import sqlite3
import threading
import time
from uuid import uuid4

import pytest

from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.domain.errors import SessionNotFound
from dbaguy_backend.domain.execution import DiscardCommand, ExecuteCommand, FetchMoreCommand
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.sessions.manager import SessionCreateRequest, SessionManager
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore

LONG_SQL = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 2000000000) SELECT count(*) FROM c"
)


class Recorder:
    def __init__(self) -> None:
        self.events = []
        self._cv = threading.Condition()

    def __call__(self, event) -> None:
        with self._cv:
            self.events.append(event)
            self._cv.notify_all()

    def wait_for(self, predicate, timeout: float = 10.0):
        with self._cv:
            assert self._cv.wait_for(lambda: any(predicate(e) for e in self.events), timeout), self.types()
            return next(e for e in self.events if predicate(e))

    def of(self, request_id, type_):
        return [e for e in self.events if e.type == type_ and getattr(e, "request_id", None) == request_id]

    def types(self):
        return [e.type for e in self.events]


@pytest.fixture
def setup(tmp_path):
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"v{i}",) for i in range(1200)])
    conn.commit()
    conn.close()
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    entry = profiles.create(ProfileUpsertRequest.model_validate({"profile": {"engine": "sqlite", "name": "s", "path": str(db)}}))
    connections = ConnectionService(AdapterRegistry([SqliteAdapter()]), profiles, SshTunnelManager(write_askpass_script(tmp_path)))
    manager = SessionManager(connections, profiles, idle_timeout=600)
    info = manager.create(SessionCreateRequest(profile_id=entry.profile.id))
    recorder = Recorder()
    worker = manager.attach(info.session_id, recorder)
    yield manager, info, worker, recorder
    manager.close_all()


def finished(rid):
    return lambda e: e.type == "finished" and e.request_id == rid  # type first: session_state has no request_id


def paused(rid, count):
    return lambda e: e.type == "result_set_end" and e.request_id == rid and e.row_count == count


def request_events(recorder: Recorder, rid: str, timeout: float = 10.0) -> list:
    """Once `rid` has finished: its events, from its `started` to its `finished`, with the session_state among them."""
    recorder.wait_for(finished(rid), timeout)
    events = list(recorder.events)
    start = next(i for i, e in enumerate(events) if e.type == "started" and e.request_id == rid)
    end = next(i for i, e in enumerate(events) if e.type == "finished" and e.request_id == rid)
    return events[start:end + 1]


def test_paging_fetch_more_and_finish(setup):
    _, _, worker, rec = setup
    worker.submit(ExecuteCommand(request_id="r1", sql="SELECT id, v FROM t ORDER BY id", page_size=500))
    assert rec.wait_for(paused("r1", 500)).has_more
    worker.submit(FetchMoreCommand(request_id="r1", count=500))
    assert rec.wait_for(paused("r1", 1000)).has_more
    worker.submit(FetchMoreCommand(request_id="r1", count=500))
    rec.wait_for(finished("r1"))
    ends = [(e.row_count, e.has_more) for e in rec.of("r1", "result_set_end")]
    assert ends == [(500, True), (1000, True), (1200, False)]
    assert sum(len(e.rows) for e in rec.of("r1", "rows")) == 1200
    assert len(rec.of("r1", "result_set")) == 1


def test_worker_encodes_cells_for_the_client(setup):
    _, _, worker, rec = setup
    sql = "SELECT x'00ff' AS b, 1152921504606846976 AS big, 1.5 AS f, NULL AS n"
    worker.submit(ExecuteCommand(request_id="enc", sql=sql))
    rec.wait_for(finished("enc"))
    assert rec.of("enc", "rows")[0].rows == [[{"$type": "bytes", "size": 2, "preview": "00ff"}, "1152921504606846976", 1.5, None]]


def test_exact_page_has_no_more(setup):
    _, _, worker, rec = setup
    worker.submit(ExecuteCommand(request_id="r2", sql="SELECT id FROM t LIMIT 500", page_size=500))
    rec.wait_for(finished("r2"))
    assert [(e.row_count, e.has_more) for e in rec.of("r2", "result_set_end")] == [(500, False)]


def test_dml_and_error(setup):
    _, _, worker, rec = setup
    worker.submit(ExecuteCommand(request_id="d1", sql="INSERT INTO t (v) VALUES ('x')"))
    rec.wait_for(finished("d1"))
    assert rec.of("d1", "affected")[0].rows == 1
    worker.submit(ExecuteCommand(request_id="e1", sql="SELECT * FROM nope"))
    rec.wait_for(finished("e1"))
    assert "no such table" in rec.of("e1", "error")[0].message


def test_cancel_running_query(setup):
    _, _, worker, rec = setup
    worker.submit(ExecuteCommand(request_id="c1", sql=LONG_SQL))
    rec.wait_for(lambda e: e.type == "started" and e.request_id == "c1")
    time.sleep(0.3)
    worker.cancel("c1")
    done = rec.wait_for(finished("c1"), timeout=5)
    assert done.cancelled is True


def test_discard_paused_then_reuse(setup):
    _, _, worker, rec = setup
    worker.submit(ExecuteCommand(request_id="p1", sql="SELECT id FROM t", page_size=10))
    rec.wait_for(paused("p1", 10))
    worker.submit(DiscardCommand(request_id="p1"))
    rec.wait_for(finished("p1"))
    worker.submit(ExecuteCommand(request_id="p2", sql="SELECT 1"))
    rec.wait_for(finished("p2"))
    assert rec.of("p2", "rows")[0].rows == [[1]]


def test_new_execute_discards_paused_result(setup):
    _, _, worker, rec = setup
    worker.submit(ExecuteCommand(request_id="a", sql="SELECT id FROM t", page_size=10))
    rec.wait_for(paused("a", 10))
    worker.submit(ExecuteCommand(request_id="b", sql="SELECT 2"))
    rec.wait_for(finished("b"))
    assert rec.of("a", "finished")  # the paused request was finished first


def test_fetch_more_without_an_open_result_still_finishes(setup):
    _, _, worker, rec = setup
    worker.submit(FetchMoreCommand(request_id="ghost", count=10))
    done = rec.wait_for(finished("ghost"))
    assert [e.code for e in rec.of("ghost", "error")] == ["no_open_result"]
    # attach replays the state; then the error, then the one `finished` right after the state it reports
    assert rec.types() == ["session_state", "error", "session_state", "finished"]
    assert done.cancelled is False and done.in_transaction is False and done.elapsed_ms == 0


def test_call_runs_on_worker_thread(setup):
    _, _, worker, _ = setup
    assert worker.call(lambda c: threading.current_thread().name).startswith("session-")


def test_reap_idle_closes_detached_sessions(setup):
    manager, info, _, rec = setup
    manager.detach(info.session_id, rec)
    manager._idle_timeout = 0
    assert manager.reap_idle() == 1
    with pytest.raises(SessionNotFound):
        manager.worker(info.session_id)
