import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeVar
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseConnection, StatementError
from ..adapters.registry import AdapterRegistry
from ..domain.errors import DomainError, QueryFailed, WriteOutcomeUnknown
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
_UNKNOWN = "{}: the write may or may not have been applied. Reload to see what the server holds"


class _WriteCall[R]:
    """A write's task on the metadata worker (ruling C42). It runs only if it starts before its caller stops waiting:
    the check and the start are one step under a lock, so a write given up on while queued never runs later."""

    def __init__(self, fn: Callable[[DatabaseConnection], R]) -> None:
        self._fn = fn
        self._lock = threading.Lock()
        self._state: Literal["queued", "started", "abandoned"] = "queued"

    def __call__(self, connection: DatabaseConnection) -> R:
        with self._lock:
            if self._state == "abandoned":
                logger.debug("A write given up on while it was queued was not run")
                raise QueryFailed(_NOT_SENT)  # nobody waits for it any more
            self._state = "started"
        return self._fn(connection)

    def abandon(self) -> bool:
        """Give up on the call: True when it had not started, and now never will."""
        with self._lock:
            if self._state == "queued":
                self._state = "abandoned"
                return True
            return False


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
            keys = [k for k in self._workers if k[0] == profile_id]
        for key in keys:
            self._drop(key)

    def reap_idle(self) -> int:
        now = time.monotonic()
        with self._lock:
            idle = [k for k, e in self._workers.items() if now - e.last_used >= self._idle_timeout]
        for key in idle:
            self._drop(key)
        return len(idle)

    def close_all(self) -> None:
        with self._lock:
            keys = list(self._workers)
        for key in keys:
            self._drop(key)

    def with_connection(
        self, profile_id: UUID, database: str | None, fn: Callable[[DatabaseConnection], T], *, retry: bool = True
    ) -> T:
        """Run `fn` on the metadata connection of (profile, database), reconnecting once if it was lost.

        `database` only selects a connection for engines that open one per database. `retry=False` is a write (D-04-9,
        ruling C42): `fn` runs at most once. Given up on before it started (`write_timeout` passed while it was queued
        behind another call), it never runs: QueryFailed, nothing was sent. A failure after it started that is not a
        DomainError (the timeout, an unexpected error) is WriteOutcomeUnknown: it may have been applied.
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
            except DomainError:
                raise  # NotFound / ValidationFailed from introspection: the connection is fine
            except Exception as exc:
                self._drop(key)
                if attempt == 2:
                    message = exc.message if isinstance(exc, StatementError) else str(exc)
                    raise QueryFailed(message) from exc
                logger.warning("Metadata query failed on {} ({}); reconnecting", profile.name, exc)
        raise AssertionError("unreachable")

    def _write_on(self, profile: AnyProfile, database: str | None, fn: Callable[[DatabaseConnection], T]) -> T:
        """`with_connection(retry=False)`. A DomainError is the call's own answer and passes through untouched."""
        key = (profile.id, database)
        worker = self._worker(key, profile, database)
        call = _WriteCall(fn)
        try:
            return worker.call(call, timeout=self._write_timeout)
        except DomainError:
            raise
        except Exception as exc:
            timed_out = isinstance(exc, TimeoutError)
            if call.abandon():
                # Nothing was sent. A timeout here means the connection is busy with another call, not broken: it stays.
                if not timed_out:
                    self._drop(key)
                logger.info("A write on {} {}: not sent", profile.name,
                            f"waited {self._write_timeout:g} s for the metadata connection" if timed_out
                            else f"could not start ({type(exc).__name__})")
                raise QueryFailed(_NOT_SENT if timed_out else _NOT_STARTED) from exc
            self._drop(key)
            # The type only: an unexpected error's text may quote what the write carried.
            logger.warning("A write on {} {} after it started ({}): its outcome is unknown", profile.name,
                           "timed out" if timed_out else "failed", type(exc).__name__)
            reason = f"No answer within {self._write_timeout:g} s" if timed_out else "The write failed after it started"
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

    def _drop(self, key: _Key) -> None:
        with self._lock:
            entry = self._workers.pop(key, None)
        if entry is None:
            return
        try:
            entry.worker.close()
        except Exception:
            logger.exception("Error while closing the metadata connection of {}", key)
