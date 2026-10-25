import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal, TypeVar
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseConnection, StatementError, StatementNotSent
from ..adapters.registry import AdapterRegistry
from ..domain.errors import DomainError, QueryFailed, SessionBusy, WriteOutcomeUnknown
from ..domain.metadata import MetadataRequest, ObjectDetail, ObjectNode, path_database
from ..domain.profiles import AnyProfile
from ..sessions.worker import SessionWorker
from .connection_service import ConnectionService
from .profile_service import ProfileService

T = TypeVar("T")
_Key = tuple[UUID, str | None, bool]  # (profile, database, an MCP agent's: spec ⑧ D-08-11)

# The agents' catalog connections held at once: past it, the one used longest ago closes (task 2 fix round 1, I-1). As
# many as the agent sessions D-08-1 allows; an agent works in one to three databases at a time.
MAX_AGENT_CONNECTIONS = 8
WRITE_TIMEOUT_S = 120.0  # how long a write waits for its answer, time queued behind another call included (as a read)
_NOT_SENT = "Timed out waiting for the connection: nothing was sent"
_NOT_STARTED = "The write could not start on the connection: nothing was sent"
_CLOSED = "The connection was closed before the write started: nothing was sent"
_UNKNOWN = "{}: the write may or may not have been applied. Reload to see what the server holds"
_FAILED_AFTER_START = "The write failed after it started"  # `fn` raised: an error of its own, or a timeout of its own
_STOPPED = "The MCP server stopped while the call ran"
_CHANGED = "The connection's settings changed while the call waited"
# The MCP server's stop epoch as one tool call began (`agent_call`, T2 re-review 2): set on the call's own thread
_AGENT_CALL: ContextVar[int | None] = ContextVar("dbaguy_agent_call", default=None)


def error_code(exc: Exception) -> str:
    """An error's code (a server's, or its type): what a log line above DEBUG may say of it."""
    return str(getattr(exc, "code", None) or type(exc).__name__)


class _WriteCall[R]:
    """A write's task on the metadata worker (ruling C42). It runs only if it starts before its caller stops waiting:
    the check and the start are one step under a lock, so a write given up on while queued never runs later."""

    def __init__(self, fn: Callable[[DatabaseConnection], R]) -> None:
        self._fn = fn
        self._lock = threading.Lock()
        self._state: Literal["queued", "started", "finished", "abandoned"] = "queued"

    def __call__(self, connection: DatabaseConnection) -> R:
        with self._lock:
            if self._state == "abandoned":
                logger.debug("A write given up on while it was queued was not run")
                raise QueryFailed(_NOT_SENT)  # nobody waits for it any more
            self._state = "started"
        try:
            return self._fn(connection)
        finally:
            with self._lock:
                self._state = "finished"

    def abandon(self) -> bool:
        """Give up on the call: True when it had not started, and now never will."""
        with self._lock:
            if self._state == "queued":
                self._state = "abandoned"
                return True
            return False

    def running(self) -> bool:
        """Started and not finished: a timeout now is the wait's, not one `fn` raised."""
        with self._lock:
            return self._state == "started"


@dataclass
class _MetaEntry:
    worker: SessionWorker
    last_used: float
    calls: int = 0  # an agent's calls running or queued on it: the cap never closes one that has any (T2 re-review 1)


class MetadataService:
    """Object-tree queries on dedicated metadata connections, one per (profile, database), and one more per (profile,
    database) for MCP agents (`agent=True`, spec ⑧ D-08-11): opened read-only (`ConnectionService.open(agent=True)`), so
    an agent's schema tools never run on the profile's writable connection. Both close after the same idle time; the
    agents hold at most MAX_AGENT_CONNECTIONS (more only while more have calls in flight), and none outlives the MCP
    server (`close_all(agents_only=True)`) or its profile's settings (final review M1: an agent's call that began before
    `invalidate` never opens a connection with the profile it read, and an open in flight then is not kept)."""

    def __init__(
        self, registry: AdapterRegistry, connections: ConnectionService, profiles: ProfileService,
        idle_timeout: float = 300.0, write_timeout: float = WRITE_TIMEOUT_S,
    ) -> None:
        self._registry = registry
        self._connections = connections
        self._profiles = profiles
        self._idle_timeout = idle_timeout
        self._write_timeout = write_timeout
        self._workers: dict[_Key, _MetaEntry] = {}
        self._key_locks: dict[_Key, threading.Lock] = {}
        self._agents_closed = 0  # bumped when the MCP server stops: an agent's call that began before keeps nothing open
        self._agent_epochs: dict[UUID, int] = {}  # bumped by `invalidate`, per profile, for the agents' calls (M1)
        self._lock = threading.Lock()

    def children(self, request: MetadataRequest, *, agent: bool = False) -> list[ObjectNode]:
        return self._call(request, lambda c: c.list_children(request.path), agent)

    def detail(self, request: MetadataRequest, *, agent: bool = False) -> ObjectDetail:
        return self._call(request, lambda c: c.object_detail(request.path), agent)

    def invalidate(self, profile_id: UUID) -> None:
        with self._lock:
            self._agent_epochs[profile_id] = self._agent_epochs.get(profile_id, 0) + 1
            entries = [(k, e.worker) for k, e in self._workers.items() if k[0] == profile_id]
        for key, worker in entries:
            self._drop(key, worker)

    @contextmanager
    def agent_call(self) -> Iterator[None]:
        """One MCP tool call (T2 re-review 2, final review M2): every agent connection it uses is judged by the MCP
        server's stop as it stood when the call began, so a describe still running when the server stops opens nothing
        after it, whichever database its next read names."""
        with self._lock:
            began = self._agents_closed
        token = _AGENT_CALL.set(began)
        try:
            yield
        finally:
            _AGENT_CALL.reset(token)

    def reap_idle(self) -> int:
        now = time.monotonic()
        with self._lock:
            idle = [(k, e.worker) for k, e in self._workers.items() if now - e.last_used >= self._idle_timeout]
        for key, worker in idle:
            self._drop(key, worker)
        return len(idle)

    def close_all(self, *, agents_only: bool = False) -> None:
        """Close every metadata connection, or (`agents_only`, the MCP server stopped) every agent's."""
        with self._lock:
            if agents_only:
                self._agents_closed += 1
            entries = [(k, e.worker) for k, e in self._workers.items() if not agents_only or k[2]]
        for key, worker in entries:
            self._drop(key, worker)

    def with_connection(
        self, profile_id: UUID, database: str | None, fn: Callable[[DatabaseConnection], T], *, retry: bool = True,
        agent: bool = False,
    ) -> T:
        """Run `fn` on the metadata connection of (profile, database), reconnecting once if it was lost.

        `database` only selects a connection for engines that open one per database. A read whose connection was closed
        and replaced while it waited for it (SessionBusy from a worker that is no longer the key's) uses the new one; that
        counts as its one reconnect.

        `retry=False` is a write (D-04-9, ruling C42): `fn` runs at most once, and is never resent on another connection.
        Given up on before it started (`write_timeout` passed while it was queued behind another call), or refused by a
        connection closed while it was queued, it never runs: QueryFailed, nothing was sent. StatementNotSent (the round
        trip that had to go before the write failed, ruling C40) is QueryFailed, nothing was sent, and the connection is
        dropped. Any other failure after it started that is not a DomainError (the timeout, an unexpected error) is
        WriteOutcomeUnknown: it may have been applied.

        `agent`: on the agents' read-only connection of (profile, database) (D-08-11); never a write. Its epochs are
        read before the profile: past a stop (since the tool call began, `agent_call`) or a save of the profile, it
        opens nothing (SessionBusy).
        """
        began = self._agent_began(profile_id) if agent else None  # before the profile is read (M1)
        profile = self._profiles.get(profile_id)
        adapter = self._registry.get(profile.engine)
        target = database if adapter.capabilities.per_database_connection else None
        if agent and not retry:
            raise ValueError("an agent's metadata connection never writes")
        return self._call_on(profile, target, fn, began) if retry else self._write_on(profile, target, fn)

    def _call(self, request: MetadataRequest, fn: Callable[[DatabaseConnection], T], agent: bool = False) -> T:
        return self.with_connection(request.profile_id, path_database(request.path), fn, agent=agent)

    def _agent_began(self, profile_id: UUID) -> tuple[int, int]:
        """An agent's call's epochs: the MCP server's stop (as its tool call began, else now) and its profile's
        saves."""
        call = _AGENT_CALL.get()
        with self._lock:
            return self._agents_closed if call is None else call, self._agent_epochs.get(profile_id, 0)

    def _stale_locked(self, profile_id: UUID, began: tuple[int, int]) -> str | None:
        """Why an agent's call that began at `began` may open nothing more, or None. Hold `_lock`."""
        if self._agents_closed != began[0]:
            return _STOPPED
        if self._agent_epochs.get(profile_id, 0) != began[1]:
            return _CHANGED
        return None

    def _call_on(
        self, profile: AnyProfile, database: str | None, fn: Callable[[DatabaseConnection], T],
        began: tuple[int, int] | None = None,
    ) -> T:
        """`began`: an agent's call (its epochs, `_agent_began`); None: the app's."""
        agent = began is not None
        key = (profile.id, database, agent)
        for attempt in (1, 2):
            if began is not None:
                with self._lock:
                    stale = self._stale_locked(profile.id, began)
                if stale is not None:
                    raise SessionBusy(stale)  # never reopened after the stop, nor with the profile read before a save
            worker = self._worker(key, profile, database, began)
            try:
                return self._counted(key, worker, fn) if agent else worker.call(fn)
            except SessionBusy:
                # Closed while the read waited for it (invalidate, the idle reap, another call's drop): `fn` never ran,
                # and whoever closed it removed it, so the read goes to the key's connection now. From the key's current
                # worker, it is `fn`'s own answer.
                if attempt == 2 or self._is_current(key, worker):
                    raise
                logger.info("A metadata query on {} found its connection closed; reconnecting", profile.name)
            except DomainError:
                raise  # NotFound / ValidationFailed from introspection: the connection is fine
            except Exception as exc:
                self._drop(key, worker)
                if attempt == 2:
                    message = exc.message if isinstance(exc, StatementError) else str(exc)
                    raise QueryFailed(message) from exc
                # An agent's names reach the server's error text (MySQL quotes them): its code only, the text at DEBUG
                logger.warning("Metadata query failed on {} ({}); reconnecting", profile.id if agent else profile.name,
                               error_code(exc) if agent else exc)
                logger.opt(exception=exc).debug("Metadata query failed on {}", profile.name)
            finally:
                if began is not None and self._agents_stopped_since(began[0]):
                    self._drop(key, worker)  # it ended after the MCP server stopped: nothing an agent opened stays open
        raise AssertionError("unreachable")

    def _counted(self, key: _Key, worker: SessionWorker, fn: Callable[[DatabaseConnection], T]) -> T:
        """`worker.call(fn)`, counted on the key's entry while it runs or waits (`_trim_agents` skips such a one)."""
        self._count(key, worker, 1)
        try:
            return worker.call(fn)
        finally:
            self._count(key, worker, -1)

    def _count(self, key: _Key, worker: SessionWorker, change: int) -> None:
        with self._lock:
            entry = self._workers.get(key)
            if entry is not None and entry.worker is worker:
                entry.calls += change

    def _agents_stopped_since(self, began: int) -> bool:
        with self._lock:
            return self._agents_closed != began

    def _write_on(self, profile: AnyProfile, database: str | None, fn: Callable[[DatabaseConnection], T]) -> T:
        """`with_connection(retry=False)`. A DomainError is the call's own answer and passes through untouched, except
        the worker's own refusal to run it (SessionBusy: closed meanwhile), which means nothing was sent."""
        key = (profile.id, database, False)
        worker = self._worker(key, profile, database)
        call = _WriteCall(fn)
        try:
            return worker.call(call, timeout=self._write_timeout)
        except SessionBusy as exc:
            if not call.abandon():
                raise  # `fn`'s own answer
            # Closed while the write was queued (invalidate, the idle reap, another call's drop): whoever closed it has
            # already removed it. Nothing to drop, and never resent on the connection opened meanwhile.
            logger.info("A write on {} found its metadata connection closed: not sent", profile.name)
            raise QueryFailed(_CLOSED) from exc
        except DomainError:
            raise
        except StatementNotSent as exc:  # ruling C40: the request that had to go first failed, so the write never went
            self._drop(key, worker)
            logger.info("A write on {} was not sent: {} ({})", profile.name, exc.message, exc.code)
            raise QueryFailed(f"{exc.message}: nothing was sent") from exc
        except Exception as exc:
            waited_out = isinstance(exc, TimeoutError) and call.running()
            if call.abandon():
                # Nothing was sent. A timeout here means the connection is busy with another call, not broken: it stays.
                timed_out = isinstance(exc, TimeoutError)
                if not timed_out:
                    self._drop(key, worker)
                logger.info("A write on {} {}: not sent", profile.name,
                            f"waited {self._write_timeout:g} s for the metadata connection" if timed_out
                            else f"could not start ({type(exc).__name__})")
                raise QueryFailed(_NOT_SENT if timed_out else _NOT_STARTED) from exc
            self._drop(key, worker)
            # The type only: an unexpected error's text may quote what the write carried.
            logger.warning("A write on {} {} after it started ({}): its outcome is unknown", profile.name,
                           f"got no answer within {self._write_timeout:g} s" if waited_out else "failed",
                           type(exc).__name__)
            reason = f"No answer within {self._write_timeout:g} s" if waited_out else _FAILED_AFTER_START
            raise WriteOutcomeUnknown(_UNKNOWN.format(reason)) from exc

    def _worker(self, key: _Key, profile: AnyProfile, database: str | None,
                began: tuple[int, int] | None = None) -> SessionWorker:
        """The key's connection, opened when it has none. An agent's (`began`) opened past a stop or a save of the
        profile is closed, never kept (T6 re-review 2 obs 3: `invalidate` cannot see an open in flight)."""
        with self._lock:
            entry = self._workers.get(key)
            if entry is not None:
                entry.last_used = time.monotonic()
                return entry.worker
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:  # one opener per key; other profiles are not blocked
            with self._lock:
                entry = self._workers.get(key)
                if entry is not None:
                    return entry.worker
            secrets = self._profiles.resolve_secrets(profile)
            agent = key[2]
            worker = SessionWorker(
                f"{'mcp-meta' if agent else 'meta'}:{profile.name}:{database or '-'}",
                lambda: self._connections.open(profile, database, secrets, agent=agent),
            )
            try:
                worker.start()
            except BaseException:
                worker.close()  # never leave the thread, connection or tunnel behind
                raise
            with self._lock:
                stale = self._stale_locked(profile.id, began) if began is not None else None
                if stale is None:
                    self._workers[key] = _MetaEntry(worker=worker, last_used=time.monotonic())
            if stale is not None:
                worker.close()
                raise SessionBusy(stale)
            if key[2]:
                self._trim_agents(keep=key)
            return worker

    def _trim_agents(self, keep: _Key) -> None:
        """Past MAX_AGENT_CONNECTIONS, close the agents' connections used longest ago that have no call running or
        queued (never `keep`, just opened; T2 re-review 1: closing one under its calls failed them). With more than
        the cap in flight at once (at most the 16 call workers), the next open past them trims back to the cap."""
        with self._lock:
            agents = [(e.last_used, k, e.worker, e.calls) for k, e in self._workers.items() if k[2] and k != keep]
        idle = sorted(((used, k, w) for used, k, w, calls in agents if not calls), key=lambda found: found[0])
        for _, key, worker in idle[: max(0, len(agents) + 1 - MAX_AGENT_CONNECTIONS)]:
            logger.info("The agents' catalog connections are at the limit ({}): closing the one used longest ago",
                        MAX_AGENT_CONNECTIONS)
            self._drop(key, worker)

    def _is_current(self, key: _Key, worker: SessionWorker) -> bool:
        with self._lock:
            entry = self._workers.get(key)
        return entry is not None and entry.worker is worker

    def _drop(self, key: _Key, worker: SessionWorker) -> None:
        """Close `worker` if it is still the key's: a late drop (its failure answered after another call replaced it)
        never closes the connection that replaced it."""
        with self._lock:
            entry = self._workers.get(key)
            if entry is None or entry.worker is not worker:
                return
            del self._workers[key]
        try:
            worker.close()
        except Exception:
            logger.exception("Error while closing the metadata connection of {}", key)
