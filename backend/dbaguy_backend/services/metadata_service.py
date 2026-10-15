import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseConnection, StatementError
from ..adapters.registry import AdapterRegistry
from ..domain.errors import DomainError, QueryFailed
from ..domain.metadata import MetadataRequest, NodeKind, ObjectDetail, ObjectNode
from ..domain.profiles import AnyProfile
from ..sessions.worker import SessionWorker
from .connection_service import ConnectionService
from .profile_service import ProfileService

T = TypeVar("T")
_Key = tuple[UUID, str | None]


@dataclass
class _MetaEntry:
    worker: SessionWorker
    last_used: float


class MetadataService:
    """Object-tree queries on dedicated metadata connections, one per (profile, database)."""

    def __init__(
        self, registry: AdapterRegistry, connections: ConnectionService, profiles: ProfileService,
        idle_timeout: float = 300.0,
    ) -> None:
        self._registry = registry
        self._connections = connections
        self._profiles = profiles
        self._idle_timeout = idle_timeout
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

    def _call(self, request: MetadataRequest, fn: Callable[[DatabaseConnection], T]) -> T:
        profile = self._profiles.get(request.profile_id)
        adapter = self._registry.get(profile.engine)
        database = None
        if adapter.capabilities.per_database_connection and request.path and request.path[0].kind == NodeKind.DATABASE:
            database = request.path[0].name
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
            worker.start()
            with self._lock:
                self._workers[key] = _MetaEntry(worker=worker, last_used=time.monotonic())
            return worker

    def _drop(self, key: _Key) -> None:
        with self._lock:
            entry = self._workers.pop(key, None)
        if entry is not None:
            entry.worker.close()
