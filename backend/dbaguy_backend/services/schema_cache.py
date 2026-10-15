import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseAdapter
from ..adapters.registry import AdapterRegistry
from ..domain.schema import ObjectSearchHit, SchemaSnapshot
from ..sql.completion import SnapshotIndex
from .metadata_service import MetadataService
from .profile_service import ProfileService

_Key = tuple[UUID, str]


@dataclass
class _Entry:
    snapshot: SchemaSnapshot
    built_at: float
    index: SnapshotIndex | None = None  # built on the first index() call for this snapshot


@dataclass
class _Failure:
    error: Exception
    failed_at: float


def _is_subsequence(needle: str, text: str) -> bool:
    """True when every character of `needle` occurs in `text`, in order (not necessarily adjacent)."""
    remaining = iter(text)
    return all(char in remaining for char in needle)


def _match_rank(name: str, needle: str) -> int | None:
    """0 exact, 1 prefix, 2 substring, 3 subsequence (fuzzy), None when `name` does not match. Both lowercase."""
    if name == needle:
        return 0
    if name.startswith(needle):
        return 1
    if needle in name:
        return 2
    return 3 if _is_subsequence(needle, name) else None


class SchemaCache:
    """Schema snapshots per (profile, database), rebuilt after `ttl_s` or on invalidation.

    A failed build is remembered for `failure_ttl_s` and re-raised without reconnecting: completion asks for the
    snapshot on every keystroke, and an unreachable server must not cost a connect timeout each time.
    """

    def __init__(
        self, metadata: MetadataService, registry: AdapterRegistry, profiles: ProfileService,
        ttl_s: float = 600.0, failure_ttl_s: float = 30.0, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._metadata = metadata
        self._registry = registry
        self._profiles = profiles
        self._ttl = ttl_s
        self._failure_ttl = failure_ttl_s
        self._clock = clock
        self._entries: dict[_Key, _Entry] = {}
        self._failures: dict[_Key, _Failure] = {}
        self._key_locks: dict[_Key, threading.Lock] = {}
        self._epoch = 0  # bumped by every invalidation, so a build that started before one is not kept
        self._lock = threading.Lock()

    @staticmethod
    def _key(profile_id: UUID, database: str | None) -> _Key:
        return (profile_id, database or "")

    def peek(self, profile_id: UUID, database: str | None = None) -> SchemaSnapshot | None:
        with self._lock:
            entry = self._entries.get(self._key(profile_id, database))
        return entry.snapshot if entry else None

    def get(self, profile_id: UUID, database: str | None = None) -> SchemaSnapshot:
        snapshot = self._snapshot(profile_id, database, wait=True)
        assert snapshot is not None  # waiting always ends in a snapshot or an exception
        return snapshot

    def index(self, profile_id: UUID, database: str | None = None, wait: bool = True) -> SnapshotIndex | None:
        """Completion index of the current snapshot, built once per snapshot.

        With `wait=False` it returns None at once while another thread is building that snapshot, so per-keystroke
        requests do not pile up behind a slow or unreachable server.
        """
        snapshot = self._snapshot(profile_id, database, wait)
        if snapshot is None:
            return None
        key = self._key(profile_id, database)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.snapshot is snapshot and entry.index is not None:
                return entry.index
        built = SnapshotIndex(snapshot, self._adapter(profile_id).dialect)  # outside `_lock`: big schemas take a while
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.snapshot is not snapshot:
                return built  # invalidated or replaced meanwhile: answer this call, keep nothing
            if entry.index is None:  # a concurrent caller may have stored one first; keep a single index
                entry.index = built
            return entry.index

    def invalidate(self, profile_id: UUID, database: str | None = None) -> None:
        """Forget the snapshot (and any remembered failure) of one database, or of every database of the profile."""
        with self._lock:
            self._epoch += 1
            for store in (self._entries, self._failures):
                for key in [k for k in store if k[0] == profile_id and (database is None or k[1] == database)]:
                    del store[key]

    def search(self, profile_id: UUID, q: str, database: str | None = None, limit: int = 50) -> list[ObjectSearchHit]:
        """Tables and views whose name matches `q` (or `schema.name`): exact, prefix, substring, then subsequence."""
        schema_part, _, name_part = q.strip().rpartition(".")
        needle, schema_needle = name_part.strip().lower(), schema_part.strip().lower()
        if not needle and not schema_needle:
            return []
        snapshot = self.get(profile_id, database)
        adapter = self._adapter(profile_id)
        search_path = {name.lower(): position for position, name in enumerate(snapshot.search_path)}
        scored = []
        for schema in snapshot.schemas:
            if schema_needle and not schema.name.lower().startswith(schema_needle):
                continue
            path_rank = search_path.get(schema.name.lower(), len(search_path))
            for relation in schema.relations:
                name = relation.name.lower()
                rank = _match_rank(name, needle)
                if rank is not None:
                    scored.append(((rank, schema.system, path_rank, len(name), name), schema.name, relation))
        scored.sort(key=lambda item: item[0])
        logger.debug("Object search {!r} in {}: {} matches", q, profile_id, len(scored))
        return [
            ObjectSearchHit(
                database=snapshot.database, schema_name=schema_name, name=relation.name, kind=relation.kind,
                path=adapter.relation_path(snapshot.database, schema_name, relation.name, relation.kind),
            )
            for _, schema_name, relation in scored[:limit]
        ]

    def _adapter(self, profile_id: UUID) -> DatabaseAdapter:
        return self._registry.get(self._profiles.get(profile_id).engine)

    def _snapshot(self, profile_id: UUID, database: str | None, wait: bool) -> SchemaSnapshot | None:
        """The cached snapshot, else a build (one per key; other keys are not blocked). Without `wait`, None when
        another thread is building this key right now."""
        key = self._key(profile_id, database)
        with self._lock:
            cached = self._cached(key)
            if cached is not None:
                return cached
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        if not key_lock.acquire(blocking=wait):
            logger.debug("Schema snapshot of {} is still being built; not waiting", profile_id)
            return None
        try:
            with self._lock:
                cached = self._cached(key)  # whoever held the key lock before us may have just built (or failed)
                if cached is not None:
                    return cached
                epoch = self._epoch
            return self._build(key, profile_id, database, epoch)
        finally:
            key_lock.release()

    def _cached(self, key: _Key) -> SchemaSnapshot | None:
        """A fresh snapshot, or None when a build is due. Raises a failure that is still remembered. Hold `_lock`."""
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None and now - entry.built_at < self._ttl:
            return entry.snapshot
        failure = self._failures.get(key)
        if failure is not None and now - failure.failed_at < self._failure_ttl:
            logger.debug("Schema snapshot of {} failed recently; not retrying yet", key[0])
            raise failure.error.with_traceback(None)  # a fresh traceback each time, or the stored one would only grow
        return None

    def _build(self, key: _Key, profile_id: UUID, database: str | None, epoch: int) -> SchemaSnapshot:
        started = time.monotonic()
        try:
            snapshot = self._metadata.with_connection(profile_id, database, lambda c: c.schema_snapshot())
        except Exception as exc:
            logger.warning("Could not build the schema snapshot of {} ({}); retrying in {:.0f} s", profile_id, exc,
                           self._failure_ttl)
            with self._lock:
                if epoch == self._epoch:
                    self._failures[key] = _Failure(error=exc, failed_at=self._clock())
            raise
        relations = sum(len(s.relations) for s in snapshot.schemas)
        logger.info("Built schema snapshot for {} ({} relations, {:.0f} ms)", profile_id, relations,
                    (time.monotonic() - started) * 1000)
        with self._lock:
            if epoch == self._epoch:
                self._failures.pop(key, None)
                self._entries[key] = _Entry(snapshot=snapshot, built_at=self._clock())
        return snapshot
