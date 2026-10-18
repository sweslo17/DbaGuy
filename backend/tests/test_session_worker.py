"""Worker / manager lifecycle and cancel semantics, driven by a fake connection so every race is forced, not hoped for."""

import sqlite3
import threading
import time

import pytest

from dbaguy_backend.adapters.base import AdapterCapabilities, RawJsonRows, RunOptions
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.domain.classification import Classification, StatementKind
from dbaguy_backend.domain.errors import ConnectionFailed, SessionBusy, SessionNotFound
from dbaguy_backend.domain.execution import (
    ExecuteCommand,
    ExportCommand,
    FetchMoreCommand,
    MessageSeverity,
    QueryContext,
    ServerMessage,
    StatementParams,
)
from dbaguy_backend.domain.formats import FormatKind
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.services.safety import GuardAction, GuardDecision
from dbaguy_backend.sessions.manager import SessionCreateRequest, SessionManager
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.fake_db import FakeConnection, FakeLease, FakeResult
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


def test_a_repeated_cancel_of_the_same_request_is_a_forced_one(started_worker):  # R-03b-T8a: the second Cancel
    worker, conn, recorder = started_worker(Recorder())
    execute(worker, "r", "hold")  # a statement a plain cancel does not stop
    assert conn.blocked.wait(5)
    worker.cancel("r")
    time.sleep(0.2)
    assert conn.interruptions == ["cancel"] and not recorder.of("r", "finished")
    worker.cancel("r")
    assert recorder.wait_for(finished("r"))
    assert conn.interruptions == ["cancel", "force"]
    conn.blocked.clear()
    execute(worker, "s", "hold")  # the count belongs to one request: a new one starts again at a plain cancel
    assert conn.blocked.wait(5)
    worker.cancel("s")
    time.sleep(0.2)
    assert conn.interruptions == ["cancel", "force", "cancel"]
    worker.cancel("s")
    assert recorder.wait_for(finished("s"))


def test_closing_the_session_aborts_the_running_statement(started_worker):  # R-03b-T8a: closing the tab forces
    worker, conn, recorder = started_worker(Recorder())
    execute(worker, "r", "hold")
    assert conn.blocked.wait(5)
    worker.close()
    assert conn.interruptions == ["abort"] and conn.closed


def test_a_timeout_never_forces(started_worker):  # a statement timeout behaves like the first Cancel only
    worker, conn, recorder = started_worker(Recorder())
    worker.submit(ExecuteCommand(request_id="t", sql="block", timeout_s=0.2))
    assert recorder.wait_for(finished("t")).timed_out
    assert conn.interruptions == ["cancel"]


def test_a_cancel_after_a_timeout_forces(started_worker):  # R-03b-T8h: the warning after a timeout says "press Cancel"
    worker, conn, recorder = started_worker(Recorder())
    worker.submit(ExecuteCommand(request_id="t", sql="hold", timeout_s=0.2))
    assert conn.blocked.wait(5)
    time.sleep(0.4)
    assert conn.interruptions == ["cancel"] and not recorder.of("t", "finished")
    worker.cancel("t")
    recorder.wait_for(finished("t"))
    assert conn.interruptions == ["cancel", "force"] and recorder.of("t", "error")[0].code == "connection_lost"


@pytest.mark.parametrize("sql, column", [("rows 3\x00", 7), ("SELECT 1;\n-- a\x00 b", 5)])
def test_text_with_a_nul_character_never_reaches_the_connection(started_worker, sql, column):  # Important 3
    worker, conn, recorder = started_worker(Recorder())
    execute(worker, "n", sql)
    done = recorder.wait_for(finished("n"))
    [error] = recorder.of("n", "error")
    assert error.code == "invalid_text" and "U+0000" in error.message
    assert error.position.column == column and not done.cancelled
    assert conn.runs == []  # drivers cut C strings at NUL: whatever follows it would be lost silently
    worker.submit(ExportCommand(request_id="e", sql=sql, path="/tmp/never-written.csv", format=FormatKind.CSV))
    recorder.wait_for(finished("e"))
    assert recorder.of("e", "error")[0].code == "invalid_text" and conn.runs == []


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


def test_cancel_while_no_driver_call_is_in_flight_is_honoured_at_the_pause(started_worker):
    # The cancel reaches the connection while the worker is between driver calls (delivering rows): the driver has
    # nothing to interrupt, so the worker itself must stop at the pause instead of leaving the result paused.
    recorder = HoldingRecorder(lambda e: e.type == "rows" and e.request_id == "L")
    worker, conn, _ = started_worker(recorder)
    execute(worker, "L", "rows 3", page_size=1)
    assert recorder.holding.wait(5)
    worker.cancel("L")
    recorder.proceed.set()
    done = recorder.wait_for(finished("L"))
    assert done.cancelled is True
    assert [e.text for e in recorder.of("L", "message")] == ["Query cancelled"]
    assert not [e for e in recorder.of("L", "result_set_end") if e.has_more], "the cancelled request paused anyway"
    execute(worker, "next", "rows 1")  # the flag must not leak into the next request
    assert recorder.wait_for(finished("next")).cancelled is False


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


# ---- 6. messages a connection delivers while a statement runs reach that request, live ----

class MessagingConnection(FakeConnection):
    """Delivers one message from inside run(), as SQL Server delivers a PRINT before the rows."""

    def __init__(self) -> None:
        super().__init__()
        self.listener = None
        self.listeners = []

    def set_message_listener(self, listener) -> None:
        self.listener = listener
        self.listeners.append(listener)

    def run(self, sql, options=None):
        if self.listener is not None:
            self.listener(ServerMessage(severity=MessageSeverity.INFO, text=f"before {sql}"))
        return super().run(sql, options)


@pytest.fixture
def messaging_worker():
    """A started worker on a MessagingConnection, with a recorder attached: (worker, connection, recorder)."""
    lease = FakeLease(MessagingConnection())
    worker = SessionWorker("fake", lambda: lease)
    worker.start()
    recorder = Recorder()
    worker.set_emitter(recorder)
    yield worker, lease.connection, recorder
    worker.close()


def test_live_messages_reach_the_running_request_before_its_rows(messaging_worker):
    worker, conn, rec = messaging_worker
    execute(worker, "m", "rows 3")
    rec.wait_for(finished("m"))
    kinds = [e.type for e in rec.events if getattr(e, "request_id", None) == "m"]
    assert kinds.index("message") < kinds.index("result_set")
    assert [e.text for e in rec.of("m", "message")] == ["before rows 3"]
    assert conn.listener is None  # cleared when the request ended
    execute(worker, "n", "rows 1")
    rec.wait_for(finished("n"))
    assert [e.text for e in rec.of("n", "message")] == ["before rows 1"]  # each request gets its own


def test_a_paused_result_keeps_its_listener_across_fetch_more(messaging_worker):
    worker, conn, rec = messaging_worker
    execute(worker, "p", "rows 3", page_size=1)
    rec.wait_for(lambda e: e.type == "result_set_end" and e.request_id == "p" and e.has_more)
    listener = conn.listener
    assert listener is not None  # paused, still the open request's
    worker.submit(FetchMoreCommand(request_id="p", count=10))
    rec.wait_for(finished("p"))
    assert conn.listeners == [listener, None]  # set once at the start, cleared once at the end, untouched in between
    assert [e.text for e in rec.of("p", "message")] == ["before rows 3"]


@pytest.mark.parametrize("how", ["fail", "cancel"])
def test_a_failed_or_cancelled_request_clears_its_listener(messaging_worker, how):
    worker, conn, rec = messaging_worker
    execute(worker, "x", "fail boom" if how == "fail" else "block")
    if how == "cancel":
        assert conn.blocked.wait(5)
        worker.cancel("x")
    done = rec.wait_for(finished("x"))
    assert done.cancelled is (how == "cancel")
    assert [e.text for e in rec.of("x", "message") if e.text.startswith("before")] == [f"before {conn.runs[-1]}"]
    assert conn.listener is None and len(conn.listeners) == 2


def test_an_export_routes_live_messages_to_its_own_request(messaging_worker, tmp_path):
    worker, conn, rec = messaging_worker
    allow_read = GuardDecision(action=GuardAction.RUN, classification=Classification(kind=StatementKind.READ))
    worker.configure(WorkerHooks(check=lambda sql, confirmed: allow_read))
    worker.submit(ExportCommand(request_id="e", sql="rows 4", format=FormatKind.CSV, path=str(tmp_path / "out.csv")))
    rec.wait_for(finished("e"))
    assert not rec.of("e", "error")
    texts = [e.text for e in rec.of("e", "message")]
    assert texts[0] == "before rows 4" and texts[-1].startswith("Exported 4 rows")
    assert conn.listener is None and len(conn.listeners) == 2


# ---- 7. per-request options, presentation, idle close of paused results, raw JSON export (04a) ----

CONTEXT = QueryContext(bucket="dbaguy", scope="app")


def _policy(sql, confirmed):  # `rows …` reads; anything else writes
    kind = StatementKind.READ if sql.startswith("rows") else StatementKind.WRITE
    return GuardDecision(action=GuardAction.RUN, classification=Classification(kind=kind))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_worker(connection=None, clock=None, recorder=None, **capabilities):
    lease = FakeLease(connection)
    worker = SessionWorker("fake", lambda: lease, clock=clock or Clock())
    worker.start()
    worker.configure(WorkerHooks(check=_policy, capabilities=AdapterCapabilities(**capabilities)))
    recorder = recorder or Recorder()
    worker.set_emitter(recorder)
    return worker, lease.connection, recorder


def test_unsupported_options_never_reach_the_connection():
    worker, conn, rec = make_worker()
    try:
        worker.submit(ExecuteCommand(request_id="c", sql="rows 1", query_context=CONTEXT))
        rec.wait_for(finished("c"))
        assert rec.of("c", "error")[0].code == "unsupported_option" and conn.runs == []
    finally:
        worker.close()


def test_options_and_retry_safety_reach_run():
    worker, conn, rec = make_worker(query_context=True, parameters=True)
    try:
        params = StatementParams(named={"n": "1"})
        worker.submit(ExecuteCommand(request_id="r", sql="rows 1", query_context=CONTEXT, params=params))
        rec.wait_for(finished("r"))
        worker.submit(ExecuteCommand(request_id="w", sql="begin"))
        rec.wait_for(finished("w"))
        assert conn.options == [RunOptions(query_context=CONTEXT, params=params, retry_safe=True), RunOptions()]
        worker.submit(ExecuteCommand(request_id="bad", sql="rows 1", params=StatementParams(positional=["x"])))
        rec.wait_for(finished("bad"))
        assert rec.of("bad", "error")[0].code == "invalid_params" and len(conn.runs) == 2
    finally:
        worker.close()


class PlanConnection(FakeConnection):
    def run(self, sql, options=None):
        result = super().run(sql, options)
        result.presentation = lambda: "plan"
        return result


def test_presentation_reaches_the_result_set_event():
    worker, _, rec = make_worker(PlanConnection())
    try:
        execute(worker, "x", "rows 1")
        rec.wait_for(finished("x"))
        assert rec.of("x", "result_set")[0].presentation == "plan"
    finally:
        worker.close()


def test_the_history_record_carries_the_requests_query_context():  # P37: `_complete` fills ExecutionRecord.query_context
    worker, _, rec = make_worker(query_context=True)
    records = []
    worker.configure(WorkerHooks(check=_policy, capabilities=AdapterCapabilities(query_context=True),
                                 on_record=records.append))
    try:
        worker.submit(ExecuteCommand(request_id="h", sql="rows 1", query_context=CONTEXT))
        rec.wait_for(finished("h"))
        worker.submit(ExecuteCommand(request_id="n", sql="rows 1"))  # no context: the record says so
        rec.wait_for(finished("n"))
        assert [r.query_context for r in records] == [CONTEXT, None]
    finally:
        worker.close()


CLOSED = ("Closed this result after 2 min without loading more rows, to free the query node — run the statement again "
          "to load more")


def test_an_idle_paused_result_is_closed_after_the_limit():
    clock = Clock()
    worker, _, rec = make_worker(clock=clock, paused_result_max_idle_s=120.0)
    try:
        execute(worker, "p", "rows 1200")
        rec.wait_for(paused("p", 500))
        clock.now += 119
        worker.check_paused_result()  # below the limit it queues nothing, so this is already settled
        assert not rec.of("p", "finished")
        clock.now += 2
        worker.check_paused_result()
        rec.wait_for(finished("p"))
        assert rec.of("p", "message")[-1].text == CLOSED  # loaded rows stay; the client marks the set partial
        worker.submit(FetchMoreCommand(request_id="p"))
        rec.wait_for(lambda e: e.type == "error" and e.code == "no_open_result")
    finally:
        worker.close()


def test_engines_without_a_limit_keep_paused_results():
    clock = Clock()
    worker, _, rec = make_worker(clock=clock)
    try:
        execute(worker, "p", "rows 1200")
        rec.wait_for(paused("p", 500))
        clock.now += 10_000
        worker.check_paused_result()
        worker.submit(FetchMoreCommand(request_id="p"))
        rec.wait_for(paused("p", 1000))
    finally:
        worker.close()


@pytest.mark.parametrize("fetch_first", [True, False])
def test_idle_close_and_fetch_more_race_either_way(fetch_first):  # Review Focus 3
    clock = Clock()
    rec = HoldingRecorder(lambda e: e.type == "result_set_end" and e.has_more)
    worker, _, _ = make_worker(clock=clock, recorder=rec, paused_result_max_idle_s=120.0)
    try:
        execute(worker, "p", "rows 900")
        assert rec.holding.wait(5)  # the worker thread is inside the pause; its mark is set
        clock.now += 121
        if fetch_first:
            worker.submit(FetchMoreCommand(request_id="p"))
            worker.check_paused_result()
        else:
            worker.check_paused_result()
            worker.submit(FetchMoreCommand(request_id="p"))
        rec.proceed.set()
        worker.call(lambda c: None)  # a barrier: both queued tasks have run
        texts = [e.text for e in rec.of("p", "message")]
        if fetch_first:  # the Load more won: every row arrived and the close found nothing to do
            assert sum(len(e.rows) for e in rec.of("p", "rows")) == 900 and CLOSED not in texts
            assert len(rec.of("p", "finished")) == 1
        else:  # the close won: the Load more finds no open result
            assert CLOSED in texts and rec.of("p", "error")[0].code == "no_open_result"
    finally:
        worker.close()


def _barrier(worker):
    """Wait until every task queued so far has run, without discarding the open result (as `call()` would)."""
    done = threading.Event()
    worker._put(done.set)
    assert done.wait(5)


def test_a_close_queued_for_an_earlier_pause_spares_the_next_one():  # the token: same request, a later pause
    clock = Clock()
    rec = HoldingRecorder(lambda e: e.type == "result_set_end" and e.has_more)
    worker, _, _ = make_worker(clock=clock, recorder=rec, paused_result_max_idle_s=120.0)
    try:
        execute(worker, "p", "rows 1200")
        assert rec.holding.wait(5)  # paused at 500
        clock.now += 121
        worker.submit(FetchMoreCommand(request_id="p"))
        worker.check_paused_result()  # queued behind the Load more, for the pause at 500
        rec.proceed.set()
        rec.wait_for(paused("p", 1000))  # the Load more paused again, a new idle stretch from now
        _barrier(worker)
        assert not rec.of("p", "finished") and CLOSED not in [e.text for e in rec.of("p", "message")]
        clock.now += 121
        worker.check_paused_result()
        rec.wait_for(finished("p"))
        assert rec.of("p", "message")[-1].text == CLOSED
    finally:
        worker.close()


def test_a_new_statement_that_came_first_ends_the_paused_result_normally():
    clock = Clock()
    rec = HoldingRecorder(lambda e: e.type == "result_set_end" and e.has_more)
    worker, _, _ = make_worker(clock=clock, recorder=rec, paused_result_max_idle_s=120.0)
    try:
        execute(worker, "p", "rows 900")
        assert rec.holding.wait(5)
        clock.now += 121
        execute(worker, "q", "rows 1")
        worker.check_paused_result()
        rec.proceed.set()
        rec.wait_for(finished("q"))
        _barrier(worker)
        assert len(rec.of("p", "finished")) == 1 and CLOSED not in [e.text for e in rec.of("p", "message")]
        assert not rec.of("q", "message")  # the close does not land on the next request either
    finally:
        worker.close()


def test_the_pause_mark_lives_only_while_the_result_waits():  # resumed or ended: nothing for the idle check to see
    rec = HoldingRecorder(lambda e: e.type == "result_set_end" and not e.has_more)
    worker, _, _ = make_worker(recorder=rec, paused_result_max_idle_s=120.0)
    try:
        execute(worker, "p", "rows 700")
        rec.wait_for(paused("p", 500))
        assert worker._paused is not None and worker._paused.request_id == "p"
        worker.submit(FetchMoreCommand(request_id="p"))
        assert rec.holding.wait(5)  # loading the rest: not idle
        assert worker._paused is None
        rec.proceed.set()
        rec.wait_for(finished("p"))
        execute(worker, "c", "rows 700")
        rec.wait_for(paused("c", 500))
        worker.cancel("c")  # a discarded result
        rec.wait_for(finished("c"))
        assert worker._paused is None
    finally:
        worker.close()


class RawResult(FakeResult, RawJsonRows):
    def fetch_raw(self, n):
        taken, self._rows = self._rows[:n], self._rows[n:]
        return [f'{{"n": {value},\n "s": "a b"}}' for value in taken]


class RawConnection(FakeConnection):
    def run(self, sql, options=None):
        self.runs.append(sql)
        self.options.append(options)
        return RawResult(int(sql.split()[1]))


def test_a_raw_json_export_writes_the_server_text(tmp_path):
    worker, conn, rec = make_worker(RawConnection())
    try:
        target = tmp_path / "out.jsonl"
        worker.submit(ExportCommand(request_id="j", sql="rows 3", format="jsonl", path=str(target)))
        rec.wait_for(finished("j"))
        assert target.read_text().splitlines() == ['{"n":0,"s":"a b"}', '{"n":1,"s":"a b"}', '{"n":2,"s":"a b"}']
        assert conn.options[-1].retry_safe  # an export is a read
        csv = tmp_path / "out.csv"
        worker.submit(ExportCommand(request_id="c", sql="rows 2", format="csv", path=str(csv)))
        rec.wait_for(finished("c"))
        assert csv.read_text() == "n\n0\n1\n"  # other formats go by the columns
    finally:
        worker.close()


def test_an_export_format_the_dialect_cannot_write_is_refused(tmp_path):
    worker, conn, rec = make_worker()
    worker.configure(WorkerHooks(dialect=Dialect.SQLPP, check=_policy))
    try:
        worker.submit(ExportCommand(request_id="i", sql="rows 1", format="insert", path=str(tmp_path / "x.sql")))
        rec.wait_for(finished("i"))
        assert rec.of("i", "error")[0].code == "unsupported_format" and conn.runs == []
    finally:
        worker.close()
