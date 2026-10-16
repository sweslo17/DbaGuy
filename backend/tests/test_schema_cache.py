import sqlite3
import threading
import time

import pytest
from loguru import logger

from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.domain.errors import ConnectionFailed
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.metadata_service import MetadataService
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.services.schema_cache import SchemaCache
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "c.db"
    raw = sqlite3.connect(db)
    raw.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);"
        "CREATE TABLE user_roles (user_id INTEGER, role TEXT);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id));"
    )
    raw.close()
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    pid = profiles.create(ProfileUpsertRequest.model_validate(
        {"profile": {"engine": "sqlite", "name": "c", "path": str(db)}})).profile.id
    registry = AdapterRegistry([SqliteAdapter()])
    connections = ConnectionService(registry, profiles, SshTunnelManager(write_askpass_script(tmp_path)))
    metadata = MetadataService(registry, connections, profiles)
    clock = Clock()
    cache = SchemaCache(metadata, registry, profiles, ttl_s=60, clock=clock)
    yield cache, pid, db, clock
    metadata.close_all()


def test_get_caches_until_ttl_or_invalidate(env):
    cache, pid, db, clock = env
    first = cache.get(pid)
    assert cache.get(pid) is first  # cached
    sqlite3.connect(db).executescript("CREATE TABLE later (id INTEGER);")
    assert "later" not in [r.name for r in cache.get(pid).schemas[0].relations]
    clock.now += 61
    assert "later" in [r.name for r in cache.get(pid).schemas[0].relations]
    sqlite3.connect(db).executescript("CREATE TABLE later2 (id INTEGER);")
    cache.invalidate(pid)
    assert "later2" in [r.name for r in cache.get(pid).schemas[0].relations]


def test_peek_does_not_build(env):
    cache, pid, _, _ = env
    assert cache.peek(pid) is None
    cache.get(pid)
    assert cache.peek(pid) is not None


def test_search_ranking_and_paths(env):
    cache, pid, _, _ = env
    hits = cache.search(pid, "user")
    assert [h.name for h in hits] == ["users", "user_roles"]  # prefix matches, shorter name first
    assert [(s.kind, s.name) for s in hits[0].path] == [("schema", "main"), ("folder", "tables"), ("table", "users")]
    assert [h.name for h in cache.search(pid, "ORDERS")] == ["orders"]
    assert [h.name for h in cache.search(pid, "main.ord")] == ["orders"]
    assert cache.search(pid, "") == []
    assert [h.name for h in cache.search(pid, "role")] == ["user_roles"]  # substring


def test_search_ranks_exact_before_prefix_before_substring_and_honours_limit(env):
    cache, pid, db, _ = env
    sqlite3.connect(db).executescript("CREATE TABLE role (id INTEGER); CREATE TABLE roles_audit (id INTEGER);")
    assert [h.name for h in cache.search(pid, "role")] == ["role", "roles_audit", "user_roles"]
    assert [h.name for h in cache.search(pid, "role", limit=2)] == ["role", "roles_audit"]
    assert cache.search(pid, ".") == []  # no name and no schema: not a search
    assert cache.search(pid, "nope.users") == []  # no such schema
    assert {h.name for h in cache.search(pid, "main.")} == {"users", "user_roles", "orders", "role", "roles_audit"}


def test_search_falls_back_to_a_subsequence_match(env):
    cache, pid, db, _ = env
    sqlite3.connect(db).executescript("CREATE TABLE xusrx (id INTEGER);")
    assert [h.name for h in cache.search(pid, "usrrol")] == ["user_roles"]
    assert [h.name for h in cache.search(pid, "USRROL")] == ["user_roles"]  # case-insensitive
    assert [h.name for h in cache.search(pid, "main.usrrol")] == ["user_roles"]  # the name part is fuzzy too
    assert cache.search(pid, "nope.usrrol") == []  # the schema part still has to be a prefix
    # "usr" is a substring of xusrx but only a subsequence of users and user_roles: substring first, then
    # subsequence matches shorter name first (plain alphabetical order would put xusrx last).
    assert [h.name for h in cache.search(pid, "usr")] == ["xusrx", "users", "user_roles"]


def test_search_needs_the_characters_in_order(env):
    cache, pid, _, _ = env
    assert cache.search(pid, "sru") == []  # s, r and u all occur in "users", but not in that order
    assert cache.search(pid, "lor") == []


def test_a_failed_build_is_not_retried_on_every_call(env, monkeypatch):
    cache, pid, _, clock = env
    calls = []

    def unreachable(*args):
        calls.append(1)
        raise ConnectionFailed("server down")

    monkeypatch.setattr(cache._metadata, "with_connection", unreachable)
    for _ in range(3):
        with pytest.raises(ConnectionFailed):
            cache.get(pid)
    assert len(calls) == 1
    clock.now += 31
    with pytest.raises(ConnectionFailed):
        cache.get(pid)
    assert len(calls) == 2
    cache.invalidate(pid)
    with pytest.raises(ConnectionFailed):
        cache.get(pid)
    assert len(calls) == 3


def test_a_remembered_failure_does_not_pile_up_tracebacks(env, monkeypatch):
    cache, pid, _, _ = env

    def unreachable(*args):
        raise ConnectionFailed("server down")

    monkeypatch.setattr(cache._metadata, "with_connection", unreachable)
    depths = []
    for _ in range(5):
        with pytest.raises(ConnectionFailed) as caught:
            cache.get(pid)
        depth, tb = 0, caught.value.__traceback__
        while tb is not None:
            depth, tb = depth + 1, tb.tb_next
        depths.append(depth)
    assert depths[1:] == [depths[1]] * 4  # raising the stored exception again must not extend its traceback


def test_concurrent_gets_share_one_build(env, monkeypatch):
    cache, pid, _, _ = env
    real = cache._metadata.with_connection
    builds = []

    def slow(*args):
        builds.append(1)
        time.sleep(0.2)
        return real(*args)

    monkeypatch.setattr(cache._metadata, "with_connection", slow)
    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.get(pid))) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(builds) == 1 and len(results) == 4 and all(r is results[0] for r in results)


def test_invalidate_during_a_build_discards_that_snapshot(env, monkeypatch):
    cache, pid, _, _ = env
    real = cache._metadata.with_connection

    def invalidating(*args):
        snapshot = real(*args)
        cache.invalidate(pid)  # e.g. the user hit refresh while this build was still running
        return snapshot

    monkeypatch.setattr(cache._metadata, "with_connection", invalidating)
    assert cache.get(pid) is not None  # the caller still gets an answer...
    assert cache.peek(pid) is None  # ...but it was built before the invalidation, so it is not kept


def test_index_is_built_once_per_snapshot(env, monkeypatch):
    cache, pid, _, _ = env
    first = cache.index(pid)
    assert first.dialect == Dialect.SQLITE and first.snapshot is cache.peek(pid)
    assert first.resolve(None, "USERS")[1].name == "users"
    assert cache.index(pid) is first
    cache.invalidate(pid)
    assert cache.index(pid) is not first  # a new snapshot gets a new index
    real = cache._metadata.with_connection

    def invalidating(*args):
        snapshot = real(*args)
        cache.invalidate(pid)
        return snapshot

    cache.invalidate(pid)
    monkeypatch.setattr(cache._metadata, "with_connection", invalidating)
    assert cache.index(pid) is not None  # a snapshot discarded by an invalidation still answers this call
    assert cache.peek(pid) is None


def test_index_without_waiting_skips_a_build_in_progress(env, monkeypatch):
    cache, pid, _, _ = env
    real = cache._metadata.with_connection
    started, release = threading.Event(), threading.Event()

    def blocked(*args):
        started.set()
        release.wait(5)
        return real(*args)

    monkeypatch.setattr(cache._metadata, "with_connection", blocked)
    builder = threading.Thread(target=cache.get, args=(pid,))
    builder.start()
    try:
        assert started.wait(5)
        began = time.monotonic()
        assert cache.index(pid, wait=False) is None
        assert time.monotonic() - began < 0.5
    finally:
        release.set()
        builder.join(5)
    index = cache.index(pid, wait=False)
    assert index is not None and index.snapshot is cache.peek(pid)
    assert cache.index(pid) is index


def test_index_without_waiting_serves_the_expired_snapshot_while_it_is_rebuilt(env, monkeypatch):
    cache, pid, db, clock = env
    previous = cache.index(pid)
    sqlite3.connect(db).executescript("CREATE TABLE later (id INTEGER);")
    clock.now += 61  # expired: the next get() rebuilds it
    real = cache._metadata.with_connection
    started, release = threading.Event(), threading.Event()

    def blocked(*args):
        started.set()
        release.wait(5)
        return real(*args)

    monkeypatch.setattr(cache._metadata, "with_connection", blocked)
    builder = threading.Thread(target=cache.get, args=(pid,))
    builder.start()
    try:
        assert started.wait(5)
        began = time.monotonic()
        assert cache.index(pid, wait=False) is previous  # completion keeps its tables during the rebuild
        assert time.monotonic() - began < 0.5
    finally:
        release.set()
        builder.join(5)
    current = cache.index(pid, wait=False)
    assert current is not previous and current.snapshot is cache.peek(pid)
    assert "later" in [r.name for r in current.snapshot.schemas[0].relations]


def test_a_failing_index_construction_is_logged_with_its_traceback_and_re_raised(env, monkeypatch):
    cache, pid, _, _ = env

    def broken(*args):
        raise RuntimeError("index bug")

    monkeypatch.setattr("dbaguy_backend.services.schema_cache.SnapshotIndex", broken)
    records = []
    sink_id = logger.add(lambda message: records.append(message.record), level="ERROR")
    try:
        with pytest.raises(RuntimeError, match="index bug"):
            cache.index(pid)
    finally:
        logger.remove(sink_id)
    assert len(records) == 1 and records[0]["exception"] is not None
    assert cache.peek(pid) is not None  # the snapshot itself is fine and stays cached
