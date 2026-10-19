import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeVar
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseConnection, StatementError, StatementNotSent
from ..adapters.registry import AdapterRegistry
from ..domain.errors import DomainError, QueryFailed, SessionBusy, WriteOutcomeUnknown
from ..domain.metadata import MetadataRequest, NodeKind, ObjectDetail, ObjectNode
from ..domain.profiles import AnyProfile
from ..sessions.worker import SessionWorker
from .connection_service import ConnectionService
from .profile_service import ProfileService

T = TypeVar("T")
_Key = tuple[UUID, str | None]

WRITE_TIMEOUT_S = 120.0  # how long a write waits for its answer, time queued behind another call included (as a read)
_NOT_SENT = "Timed out waiting for the connection: nothing was sent"
_NOT_STARTED = "The write could not start on the connection: nothing was sent"
_CLOSED = "The connection was closed before the write started: nothing was sent"
_UNKNOWN = "{}: the write may or may not have been applied. Reload to see what the server holds"
_FAILED_AFTER_START = "The write failed after it started"  # `fn` raised: an error of its own, or a timeout of its own


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


class MetadataService:
    """Object-tree queries on dedicated metadata connections, one per (profile, database)."""

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
        self._lock = threading.Lock()

    def children(self, request: MetadataRequest) -> list[ObjectNode]:
        return self._call(request, lambda c: c.list_children(request.path))

    def detail(self, request: MetadataRequest) -> ObjectDetail:
        return self._call(request, lambda c: c.object_detail(request.path))

    def invalidate(self, profile_id: UUID) -> None:
        with self._lock:
            entries = [(k, e.worker) for k, e in self._workers.items() if k[0] == profile_id]
        for key, worker in entries:
            self._drop(key, worker)

    def reap_idle(self) -> int:
        now = time.monotonic()
        with self._lock:
            idle = [(k, e.worker) for k, e in self._workers.items() if now - e.last_used >= self._idle_timeout]
        for key, worker in idle:
            self._drop(key, worker)
        return len(idle)

    def close_all(self) -> None:
        with self._lock:
            entries = [(k, e.worker) for k, e in self._workers.items()]
        for key, worker in entries:
            self._drop(key, worker)

    def with_connection(
        self, profile_id: UUID, database: str | None, fn: Callable[[DatabaseConnection], T], *, retry: bool = True
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
        """
        profile = self._profiles.get(profile_id)
        adapter = self._registry.get(profile.engine)
        target = database if adapter.capabilities.per_database_connection else None
        return self._call_on(profile, target, fn) if retry else self._write_on(profile, target, fn)

    def _call(self, request: MetadataRequest, fn: Callable[[DatabaseConnection], T]) -> T:
        head = request.path[0] if request.path else None
        database = head.name if head is not None and head.kind == NodeKind.DATABASE else None
        return self.with_connection(request.profile_id, database, fn)

    def _call_on(self, profile: AnyProfile, database: str | None, fn: Callable[[DatabaseConnection], T]) -> T:
        key = (profile.id, database)
        for attempt in (1, 2):
            worker = self._worker(key, profile, database)
            try:
                return worker.call(fn)
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
                logger.warning("Metadata query failed on {} ({}); reconnecting", profile.name, exc)
        raise AssertionError("unreachable")

    def _write_on(self, profile: AnyProfile, database: str | None, fn: Callable[[DatabaseConnection], T]) -> T:
        """`with_connection(retry=False)`. A DomainError is the call's own answer and passes through untouched, except
        the worker's own refusal to run it (SessionBusy: closed meanwhile), which means nothing was sent."""
        key = (profile.id, database)
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

    def _worker(self, key: _Key, profile: AnyProfile, database: str | None) -> SessionWorker:
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
            worker = SessionWorker(
                f"meta:{profile.name}:{database or '-'}",
                lambda: self._connections.open(profile, database, secrets),
            )
            try:
                worker.start()
            except BaseException:
                worker.close()  # never leave the thread, connection or tunnel behind
                raise
            with self._lock:
                self._workers[key] = _MetaEntry(worker=worker, last_used=time.monotonic())
            return worker

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
