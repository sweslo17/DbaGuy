import itertools
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseAdapter, DatabaseConnection
from ..adapters.registry import AdapterRegistry
from ..domain.schema import ObjectSearchHit, RelationKind, SchemaSnapshot
from ..sql.completion import SnapshotIndex
from .metadata_service import MAX_AGENT_CONNECTIONS, MetadataService, error_code
from .profile_service import ProfileService

_Key = tuple[UUID, str]
# Snapshots built for MCP agents and not asked for by the app since, kept at once (ruling T2-2): one per catalog connection
MAX_AGENT_SNAPSHOTS = MAX_AGENT_CONNECTIONS


@dataclass
class _Entry:
    snapshot: SchemaSnapshot
    built_at: float
    index: SnapshotIndex | None = None  # built on the first index() call for this snapshot
    agents_only: bool = False  # built for an agent, never asked for by the app since: an agent's build may evict it
    used: int = 0  # when it was last read (a count): the agents' least recently used one goes first


@dataclass
class _Failure:
    error: Exception
    failed_at: float


class _Match(NamedTuple):
    database: str | None  # a relation's database; a collection's bucket
    schema_name: str  # a relation's schema; a collection's scope
    name: str
    kind: RelationKind


_SortKey = tuple[int, bool, int, int, str]  # rank, system, search-path position, name length, name


def _snapshot_of(connection: DatabaseConnection) -> SchemaSnapshot:
    return connection.schema_snapshot()


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
    """Schema snapshots per (profile, database), rebuilt after `ttl_s` or on invalidation. Only engines with one
    connection per database (PostgreSQL) keep one per database; for the others (MySQL, SQLite) the database a request
    names is ignored, like the metadata connection the snapshot is built on, so a request naming one never builds a
    second snapshot of the same server.

    A failed build is remembered for `failure_ttl_s` and re-raised without reconnecting: completion asks for the
    snapshot on every keystroke, and an unreachable server must not cost a connect timeout each time.

    An MCP agent's request (`agent=True`, spec ⑧ D-08-11) uses the same snapshot when there is one (data in memory, no
    connection); when one must be built, it is built on the agents' read-only metadata connection. A failed build is
    remembered for the side that failed only: an agent's never degrades the app's completion, nor the app's an agent's.
    The agents keep at most MAX_AGENT_SNAPSHOTS snapshots of their own (ruling T2-2): past it, building one drops the
    agents' least recently used. A snapshot the app built is never theirs, an agent reading it changes nothing, and one
    an agent built becomes the app's (kept, not rebuilt) once the app asks for it: no agent call costs the app's
    completion a snapshot.
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
        self._failures: dict[tuple[UUID, str, bool], _Failure] = {}  # by agent too: theirs are never the app's
        self._key_locks: dict[_Key, threading.Lock] = {}
        self._epoch = 0  # bumped by every invalidation, so a build that started before one is not kept
        self._uses = itertools.count(1)
        self._lock = threading.Lock()

    @staticmethod
    def _key(profile_id: UUID, database: str | None) -> _Key:
        return (profile_id, database or "")

    def _scope(self, profile_id: UUID, database: str | None) -> str | None:
        """The database that keys the snapshot: None for an engine whose one connection sees every database (the
        profile is looked up only when a database is named)."""
        if not database or self._adapter(profile_id).capabilities.per_database_connection:
            return database
        return None

    def peek(self, profile_id: UUID, database: str | None = None) -> SchemaSnapshot | None:
        key = self._key(profile_id, self._scope(profile_id, database))
        with self._lock:
            entry = self._entries.get(key)
        return entry.snapshot if entry else None

    def get(self, profile_id: UUID, database: str | None = None, *, agent: bool = False) -> SchemaSnapshot:
        snapshot = self._snapshot(profile_id, self._scope(profile_id, database), wait=True, agent=agent)
        assert snapshot is not None  # waiting always ends in a snapshot or an exception
        return snapshot

    def index(self, profile_id: UUID, database: str | None = None, wait: bool = True) -> SnapshotIndex | None:
        """Completion index of the current snapshot, built once per snapshot.

        With `wait=False` it does not wait while another thread is building that snapshot, so per-keystroke requests
        do not pile up behind a slow or unreachable server: it answers from the expired snapshot being replaced, or
        None when there is none.
        """
        database = self._scope(profile_id, database)
        snapshot = self._snapshot(profile_id, database, wait)
        if snapshot is None:
            return None
        key = self._key(profile_id, database)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.snapshot is snapshot and entry.index is not None:
                return entry.index
        try:  # outside `_lock`: big schemas take a while
            built = SnapshotIndex(snapshot, self._adapter(profile_id).dialect)
        except Exception:  # not a build failure, so nothing has logged it yet: a bug, which callers only degrade past
            logger.exception("Could not build the completion index of {}", profile_id)
            raise
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.snapshot is not snapshot:
                return built  # invalidated or replaced meanwhile: answer this call, keep nothing
            if entry.index is None:  # a concurrent caller may have stored one first; keep a single index
                entry.index = built
            return entry.index

    def invalidate(self, profile_id: UUID, database: str | None = None) -> None:
        """Forget the snapshot (and any remembered failure) of one database, or of every database of the profile."""
        database = self._scope(profile_id, database)
        with self._lock:
            self._epoch += 1
            for store in (self._entries, self._failures):
                for key in [k for k in store if k[0] == profile_id and (database is None or k[1] == database)]:
                    del store[key]

    def search(
        self, profile_id: UUID, q: str, database: str | None = None, limit: int = 50, *, agent: bool = False,
    ) -> list[ObjectSearchHit]:
        """Tables and views whose name matches `q` (or `schema.name`): exact, prefix, substring, then subsequence.
        Couchbase collections the same way (B20): the text before the last dot has to prefix the scope or
        `bucket.scope`, so `inventory.airl` and `travel-sample.inventory.airl` both narrow to one scope."""
        schema_part, _, name_part = q.strip().rpartition(".")
        needle, schema_needle = name_part.strip().lower(), schema_part.strip().lower()
        if not needle and not schema_needle:
            return []
        snapshot = self.get(profile_id, database, agent=agent)
        adapter = self._adapter(profile_id)
        search_path = {name.lower(): position for position, name in enumerate(snapshot.search_path)}
        scored: list[tuple[_SortKey, _Match]] = []
        for schema in snapshot.schemas:
            if schema_needle and not schema.name.lower().startswith(schema_needle):
                continue
            path_rank = search_path.get(schema.name.lower(), len(search_path))
            for relation in schema.relations:
                name = relation.name.lower()
                rank = _match_rank(name, needle)
                if rank is not None:
                    scored.append(((rank, schema.system, path_rank, len(name), name),
                                   _Match(snapshot.database, schema.name, relation.name, relation.kind)))
        for keyspace in snapshot.keyspaces:
            scope = keyspace.scope.lower()
            if schema_needle and not (scope.startswith(schema_needle)
                                      or f"{keyspace.bucket.lower()}.{scope}".startswith(schema_needle)):
                continue
            name = keyspace.collection.lower()
            rank = _match_rank(name, needle)
            if rank is not None:  # no search path: every scope ranks alike
                scored.append(((rank, keyspace.system, 0, len(name), name),
                               _Match(keyspace.bucket, keyspace.scope, keyspace.collection, "collection")))
        scored.sort(key=lambda item: item[0])
        logger.debug("Object search {!r} in {}: {} matches", q, profile_id, len(scored))
        return [
            ObjectSearchHit(
                database=match.database, schema_name=match.schema_name, name=match.name, kind=match.kind,
                path=adapter.relation_path(match.database, match.schema_name, match.name, match.kind),
            )
            for _, match in scored[:limit]
        ]

    def _adapter(self, profile_id: UUID) -> DatabaseAdapter:
        return self._registry.get(self._profiles.get(profile_id).engine)

    def _snapshot(
        self, profile_id: UUID, database: str | None, wait: bool, agent: bool = False,
    ) -> SchemaSnapshot | None:
        """The cached snapshot, else a build (one per key; other keys are not blocked). Without `wait`, while another
        thread is building this key: the expired snapshot it replaces, or None when there is none."""
        key = self._key(profile_id, database)
        with self._lock:
            cached = self._cached(key, agent)
            if cached is not None:
                return cached
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        if not key_lock.acquire(blocking=wait):
            with self._lock:
                expired = self._entries.get(key)
            if expired is not None:
                logger.debug("Schema snapshot of {} is being rebuilt; using the previous one", profile_id)
                return expired.snapshot
            logger.debug("Schema snapshot of {} is still being built; not waiting", profile_id)
            return None
        try:
            with self._lock:
                cached = self._cached(key, agent)  # whoever held the key lock before us may have just built (or failed)
                if cached is not None:
                    return cached
                epoch = self._epoch
            return self._build(key, profile_id, database, epoch, agent)
        finally:
            key_lock.release()

    def _cached(self, key: _Key, agent: bool = False) -> SchemaSnapshot | None:
        """A fresh snapshot, or None when a build is due. Raises a failure that is still remembered. Hold `_lock`."""
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None and now - entry.built_at < self._ttl:
            entry.used = next(self._uses)
            entry.agents_only = entry.agents_only and agent  # the app relies on it now: no agent evicts it
            return entry.snapshot
        failure = self._failures.get((*key, agent))
        if failure is not None and now - failure.failed_at < self._failure_ttl:
            logger.debug("Schema snapshot of {} failed recently; not retrying yet", key[0])
            raise failure.error.with_traceback(None)  # a fresh traceback each time, or the stored one would only grow
        return None

    def _build(self, key: _Key, profile_id: UUID, database: str | None, epoch: int, agent: bool) -> SchemaSnapshot:
        started = time.monotonic()
        try:
            if agent:  # the agents' read-only connection (D-08-11)
                snapshot = self._metadata.with_connection(profile_id, database, _snapshot_of, agent=True)
            else:
                snapshot = self._metadata.with_connection(profile_id, database, _snapshot_of)
        except Exception as exc:
            # An agent's names reach the server's error text: its code only above DEBUG (T2 M-7, D-08-14)
            logger.warning("Could not build the schema snapshot of {} ({}); retrying in {:.0f} s", profile_id,
                           error_code(exc) if agent else exc, self._failure_ttl)
            if agent:
                logger.debug("The agents' schema snapshot of {} failed: {!r}", profile_id, str(exc)[:1000])
            with self._lock:
                if epoch == self._epoch:
                    self._failures[(*key, agent)] = _Failure(error=exc, failed_at=self._clock())
            raise
        relations = sum(len(s.relations) for s in snapshot.schemas)
        logger.info("Built schema snapshot for {} ({} relations, {:.0f} ms)", profile_id, relations,
                    (time.monotonic() - started) * 1000)
        with self._lock:
            if epoch == self._epoch:
                for side in (False, True):
                    self._failures.pop((*key, side), None)
                replaced = self._entries.get(key)
                agents_only = agent and (replaced is None or replaced.agents_only)  # the app's stays the app's
                self._entries[key] = _Entry(snapshot=snapshot, built_at=self._clock(), agents_only=agents_only,
                                            used=next(self._uses))
                if agents_only:
                    self._trim_agents(keep=key)
        return snapshot

    def _trim_agents(self, keep: _Key) -> None:
        """Past MAX_AGENT_SNAPSHOTS, forget the agents' own snapshots read longest ago (never `keep`, just built, nor
        one the app has asked for). Hold `_lock`."""
        theirs = sorted((entry.used, key) for key, entry in self._entries.items() if entry.agents_only and key != keep)
        for _, key in theirs[: max(0, len(theirs) + 1 - MAX_AGENT_SNAPSHOTS)]:
            del self._entries[key]
            logger.debug("Schema snapshot of {} dropped: the agents keep {} of their own", key[0], MAX_AGENT_SNAPSHOTS)
