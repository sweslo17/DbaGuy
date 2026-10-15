"""Worker / manager lifecycle and cancel semantics, driven by a fake connection so every race is forced, not hoped for."""

import sqlite3
import threading
import time

import pytest

from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.domain.errors import ConnectionFailed, SessionBusy, SessionNotFound
from dbaguy_backend.domain.execution import ExecuteCommand
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.sessions.manager import SessionCreateRequest, SessionManager
from dbaguy_backend.sessions.worker import SessionWorker
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.fake_db import FakeConnection, FakeLease
from tests.fake_ssh import run_in_thread, wait_until
from tests.test_sessions import Recorder, finished, paused


class HoldingRecorder(Recorder):
    """Blocks the worker thread inside event delivery when `hold(event)` matches, until `proceed` is set."""

    def __init__(self, hold) -> None:
        super().__init__()
        self._hold = hold
        self.holding = threading.Event()
        self.proceed = threading.Event()

    def __call__(self, event) -> None:
        super().__call__(event)
        if self._hold(event) and not self.holding.is_set():
            self.holding.set()
            assert self.proceed.wait(10)


@pytest.fixture
def started_worker():
    """A started worker on a fake connection; `make(recorder)` attaches the recorder."""
    lease = FakeLease()
    conn = lease.connection
    worker = SessionWorker("fake", lambda: lease)
    worker.start()

    def attach(recorder):
        worker.set_emitter(recorder)
        return worker, conn, recorder

    yield attach
    worker.close()


def execute(worker, rid, sql, page_size=500):
    worker.submit(ExecuteCommand(request_id=rid, sql=sql, page_size=page_size))


# ---- 1. start() timeout / failure must not leak ----

def test_start_timeout_closes_the_connection_that_opens_late():
    release_opener, lease = threading.Event(), FakeLease()

    def slow_opener():
        assert release_opener.wait(10)
        return lease

    worker = SessionWorker("slow", slow_opener)
    with pytest.raises(ConnectionFailed, match="Timed out"):
        worker.start(timeout=0.05)
    with pytest.raises(SessionBusy):
        worker.call(lambda c: 1)  # a worker that gave up rejects new work

    release_opener.set()
    worker._thread.join(5)
    assert not worker._thread.is_alive(), "the worker thread was leaked"
    assert lease.closed.is_set(), "the late connection (and its tunnel reference) was leaked"


def test_close_before_the_connection_opened_is_safe():  # close() must cope with _lease being None
    release_opener, lease = threading.Event(), FakeLease()
    worker = SessionWorker("early", lambda: (release_opener.wait(10), lease)[1])
    worker._thread.start()
    closer, outcome = run_in_thread(worker.close)
    wait_until(lambda: worker._closed)
    release_opener.set()
    closer.join(5)
    assert outcome[0][0] == "ok" and lease.closed.is_set() and not worker._thread.is_alive()


# ---- 4. cancel must never hit a statement other than the one it targets ----

def test_cancel_during_finished_delivery_does_not_reach_the_connection(started_worker):
    recorder = HoldingRecorder(lambda e: e.type == "finished" and e.request_id == "A")
    worker, conn, _ = started_worker(recorder)
    execute(worker, "A", "rows 1")
    assert recorder.holding.wait(5)  # the worker is delivering finished(A): A is over, even though it is mid-send
    worker.cancel("A")
    recorder.proceed.set()
    execute(worker, "B", "rows 1")
    recorder.wait_for(finished("B"))
    assert conn.cancel_calls == 0, "a late cancel was delivered to the connection"
    assert not [e for e in recorder.events if e.type == "finished" and e.cancelled]


def test_cancel_of_a_running_statement_reaches_the_connection_once(started_worker):
    worker, conn, recorder = started_worker(Recorder())
    execute(worker, "r", "block")
    assert conn.blocked.wait(5)
    worker.cancel("r")
    assert recorder.wait_for(finished("r")).cancelled is True
    assert conn.cancel_calls == 1


# ---- 5. queued and paused work can be cancelled; close() does not run the backlog ----

def test_cancelled_queued_statement_never_runs(started_worker):
    worker, conn, recorder = started_worker(Recorder())
    execute(worker, "A", "block")
    assert conn.blocked.wait(5)
    execute(worker, "B", "rows 1")  # queued behind A
    worker.cancel("B")
    worker.cancel("A")  # lets A end
    done = recorder.wait_for(finished("B"))
    assert done.cancelled is True
    assert conn.runs == ["block"], "the cancelled queued statement was executed"
    messages = [e.text for e in recorder.of("B", "message")]
    assert messages == ["Query cancelled before it started"]
    assert recorder.types().index("started") < recorder.types().index("finished")


def test_cancelling_a_paused_result_finishes_it_as_cancelled(started_worker):
    recorder = HoldingRecorder(lambda e: e.type == "result_set_end" and e.has_more)
    worker, conn, _ = started_worker(recorder)
    execute(worker, "P", "rows 3", page_size=1)
    assert recorder.holding.wait(5)  # paused (page full, more rows) and still delivering the pause event
    worker.cancel("P")
    recorder.proceed.set()
    done = recorder.wait_for(finished("P"))
    assert done.cancelled is True
    assert [e.text for e in recorder.of("P", "message")] == ["Query cancelled"]
    assert conn.cancel_calls == 0  # nothing was running; the result was just closed


def test_close_does_not_run_queued_statements_and_fails_pending_calls(started_worker):
    worker, conn, recorder = started_worker(Recorder())
    execute(worker, "A", "block")
    assert conn.blocked.wait(5)
    execute(worker, "B", "rows 1")
    execute(worker, "C", "rows 1")
    caller, outcome = run_in_thread(lambda: worker.call(lambda c: "ran"))
    wait_until(lambda: worker._queue.qsize() == 3)  # B, C and the call are all waiting behind A

    worker.close()  # cancels A, then must not run the backlog
    caller.join(5)

    assert conn.runs == ["block"]
    assert conn.cancel_calls == 1
    for rid in ("B", "C"):
        assert recorder.wait_for(finished(rid)).cancelled is True
        assert recorder.of(rid, "message")[0].text == "Session closed before the query started"
    assert outcome[0][0] == "error" and isinstance(outcome[0][1], SessionBusy)
    assert not worker._thread.is_alive()


# ---- manager: cleanup on failed create, bulk close ----

@pytest.fixture
def manager_env(tmp_path):
    db = tmp_path / "m.db"
    sqlite3.connect(db).close()
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    entry = profiles.create(ProfileUpsertRequest.model_validate({"profile": {"engine": "sqlite", "name": "m", "path": str(db)}}))
    real = ConnectionService(AdapterRegistry([SqliteAdapter()]), profiles, SshTunnelManager(write_askpass_script(tmp_path)))
    manager = SessionManager(real, profiles, idle_timeout=600)
    yield manager, profiles, entry.profile.id
    manager.close_all()


class GatedConnections:
    """Stands in for ConnectionService: open() blocks until released, then hands out a FakeLease."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.lease = FakeLease()

    def open(self, profile, database, secrets):
        assert self.release.wait(10)
        return self.lease


def test_create_that_times_out_opening_does_not_leak(monkeypatch, manager_env):
    _, profiles, profile_id = manager_env
    connections = GatedConnections()
    manager = SessionManager(connections, profiles)
    original = SessionWorker.start
    monkeypatch.setattr(SessionWorker, "start", lambda self, timeout=0.05: original(self, timeout))
    with pytest.raises(ConnectionFailed):
        manager.create(SessionCreateRequest(profile_id=profile_id))
    connections.release.set()
    assert connections.lease.closed.wait(5), "the connection opened after the timeout was never closed"
    assert manager._sessions == {}


def test_create_closes_the_worker_when_start_fails_for_any_reason(monkeypatch, manager_env):
    _, profiles, profile_id = manager_env
    connections = GatedConnections()
    connections.release.set()
    manager = SessionManager(connections, profiles)
    original = SessionWorker.start

    def start_then_blow_up(self, timeout=30.0):
        original(self, timeout)
        raise RuntimeError("boom")

    monkeypatch.setattr(SessionWorker, "start", start_then_blow_up)
    with pytest.raises(RuntimeError, match="boom"):
        manager.create(SessionCreateRequest(profile_id=profile_id))
    assert connections.lease.closed.wait(5)
    assert manager._sessions == {}


def make_first_close_fail(manager, profile_id, count):
    """Create `count` sessions; the first worker's close() stops its thread and then raises."""
    infos = [manager.create(SessionCreateRequest(profile_id=profile_id)) for _ in range(count)]
    workers = [manager.worker(info.session_id) for info in infos]
    real_close = workers[0].close

    def failing_close():
        real_close()
        raise OSError("boom")

    workers[0].close = failing_close
    return workers


def test_close_all_survives_a_failing_worker_close(manager_env):
    manager, _, profile_id = manager_env
    workers = make_first_close_fail(manager, profile_id, 3)
    manager.close_all()  # must not raise, and must reach every session
    assert manager._sessions == {}
    assert all(not w._thread.is_alive() for w in workers)


def test_close_for_profile_survives_a_failing_worker_close(manager_env):
    manager, _, profile_id = manager_env
    workers = make_first_close_fail(manager, profile_id, 2)
    manager.close_for_profile(profile_id)
    assert manager._sessions == {}
    assert all(not w._thread.is_alive() for w in workers)


def test_reap_idle_skips_sessions_that_stopped_being_idle_or_are_gone(manager_env, monkeypatch):
    manager, _, profile_id = manager_env
    stale, busy, gone = (manager.create(SessionCreateRequest(profile_id=profile_id)).session_id for _ in range(3))
    manager._idle_timeout = 0
    manager._sessions[busy].attached = 1  # attached after the scan took its snapshot
    manager.close(gone)  # closed explicitly after the scan
    monkeypatch.setattr(manager, "_idle_ids", lambda: [stale, busy, gone])  # the (now outdated) scan result
    assert manager.reap_idle() == 1  # neither the vanished nor the re-attached session aborts or gets closed
    assert list(manager._sessions) == [busy]
    with pytest.raises(SessionNotFound):
        manager.close(stale)
