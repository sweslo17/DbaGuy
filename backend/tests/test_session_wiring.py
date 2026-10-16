import sqlite3
import time

import pytest

from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.domain.execution import ExportCommand
from dbaguy_backend.domain.formats import FormatKind, RowFormatOptions
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.history_service import HistoryService
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.services.safety import SafetyPolicy
from dbaguy_backend.sessions.hooks import SessionHooksFactory
from dbaguy_backend.sessions.manager import SessionCreateRequest, SessionManager
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.test_sessions import Recorder
from tests.test_worker_protocol import run


class FakeCache:
    def __init__(self):
        self.invalidated = []

    def invalidate(self, profile_id, database=None):
        self.invalidated.append((profile_id, database))


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "w.db"
    sqlite3.connect(db).executescript("CREATE TABLE t (id INTEGER);")
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    body = {"profile": {"engine": "sqlite", "name": "w", "path": str(db)}}
    pid = profiles.create(ProfileUpsertRequest.model_validate(body)).profile.id
    registry = AdapterRegistry([SqliteAdapter()])
    connections = ConnectionService(registry, profiles, SshTunnelManager(write_askpass_script(tmp_path)))
    history = HistoryService(tmp_path / "history.db")
    cache = FakeCache()
    manager = SessionManager(connections, profiles, SessionHooksFactory(registry, profiles, SafetyPolicy(), history, cache))

    def open_session():
        info = manager.create(SessionCreateRequest(profile_id=pid))
        recorder = Recorder()
        manager.attach(info.session_id, recorder)
        return manager.worker(info.session_id), recorder

    def set_profile(**fields):
        profiles.update(pid, ProfileUpsertRequest.model_validate({"profile": {**body["profile"], **fields}}))

    yield open_session, set_profile, pid, history, cache, manager
    manager.close_all()
    history.close()


def test_attach_replays_state_and_ddl_is_recorded_and_invalidates(env):
    open_session, _, pid, history, cache, _ = env
    worker, rec = open_session()
    assert rec.wait_for(lambda e: e.type == "session_state").autocommit
    run(worker, rec, "a", "CREATE TABLE z (id INTEGER)")
    assert cache.invalidated == [(pid, None)]
    [entry] = history.search().items
    assert (entry.sql, entry.profile_name, entry.status) == ("CREATE TABLE z (id INTEGER)", "w", "ok")


def test_safety_settings_apply_to_open_sessions(env):
    open_session, set_profile, *_ = env
    worker, rec = open_session()
    assert not run(worker, rec, "a", "INSERT INTO t VALUES (1)").skipped
    set_profile(safety="strict")
    assert run(worker, rec, "b", "INSERT INTO t VALUES (2)").skipped
    set_profile(read_only=True)
    run(worker, rec, "c", "INSERT INTO t VALUES (3)", confirmed=True)
    assert rec.of("c", "error")[0].code == "read_only"


def test_idle_transactions_are_rolled_back(env):
    open_session, _, _, _, _, manager = env
    worker, rec = open_session()
    run(worker, rec, "a", "BEGIN")
    run(worker, rec, "b", "INSERT INTO t VALUES (9)")
    time.sleep(0.2)
    manager.check_idle_transactions(warn_after_s=0.1, rollback_after_s=0.1)
    rec.wait_for(lambda e: e.type == "message" and e.text.startswith("Rolled back"))
    run(worker, rec, "c", "SELECT count(*) FROM t")
    assert rec.of("c", "rows")[0].rows == [[0]]


def test_failures_are_recorded_with_their_message(env):
    open_session, _, _, history, _, _ = env
    worker, rec = open_session()
    run(worker, rec, "a", "SELECT * FROM missing_table")
    [entry] = history.search().items
    assert entry.status == "error" and "missing_table" in entry.error


def test_a_failing_history_write_does_not_affect_execution(env):
    open_session, _, _, history, _, _ = env
    worker, rec = open_session()

    def broken(_):
        raise RuntimeError("disk full")

    history.record = broken
    assert not run(worker, rec, "a", "SELECT 1").skipped
    assert rec.of("a", "rows")[0].rows == [[1]]


def test_export_uses_the_classification_and_dialect_of_the_profile(env, tmp_path):
    open_session, *_ = env
    worker, rec = open_session()
    target = tmp_path / "out.sql"
    worker.submit(ExportCommand(request_id="x", sql="SELECT 1 AS n", format=FormatKind.INSERT, path=str(target),
                                options=RowFormatOptions(table_name="t")))
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "x")
    assert not rec.of("x", "error") and "INSERT INTO" in target.read_text()
