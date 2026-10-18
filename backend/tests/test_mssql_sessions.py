import time

import pytest

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.mssql import adapter as mssql_adapter
from dbaguy_backend.adapters.mssql.adapter import MssqlAdapter
from dbaguy_backend.domain.execution import CommitCommand, ExecuteCommand, SetAutocommitCommand
from dbaguy_backend.sessions.worker import SessionWorker
from tests.fake_db import FakeLease
from tests.support import MSSQL_HOST, MSSQL_PASSWORD, MSSQL_PORT, make_mssql_profile, requires_mssql
from tests.test_sessions import Recorder, finished, paused

pytestmark = requires_mssql
LARGE_SELECT = "SELECT a.id FROM dbo.big_table a CROSS JOIN dbo.big_table b"


class TimedRecorder(Recorder):
    def __init__(self) -> None:
        super().__init__()
        self.started = time.monotonic()
        self.times = []

    def __call__(self, event) -> None:
        self.times.append((time.monotonic() - self.started, event))
        super().__call__(event)


@pytest.fixture
def mssql_worker():
    lease = FakeLease(MssqlAdapter().open(make_mssql_profile(), Endpoint(MSSQL_HOST, MSSQL_PORT), MSSQL_PASSWORD, None))
    worker = SessionWorker("mssql", lambda: lease)
    worker.start()
    recorder = TimedRecorder()
    worker.set_emitter(recorder)
    yield worker, recorder
    worker.close()


def run(worker, rec, rid, sql, page_size=500):
    worker.submit(ExecuteCommand(request_id=rid, sql=sql, page_size=page_size))
    done = rec.wait_for(finished(rid), timeout=30)
    assert not rec.of(rid, "error"), rec.of(rid, "error")[0].message
    return done


def command(worker, rec, cmd):
    worker.submit(cmd)
    done = rec.wait_for(finished(cmd.request_id), timeout=30)
    assert not rec.of(cmd.request_id, "error")
    return done


def test_statement_timeout(mssql_worker):
    worker, rec = mssql_worker
    worker.submit(ExecuteCommand(request_id="t", sql="WAITFOR DELAY '00:00:05'", timeout_s=0.3))
    done = rec.wait_for(finished("t"), timeout=30)
    assert done.timed_out and done.cancelled and done.elapsed_ms < 3000
    assert not run(worker, rec, "u", "SELECT 1").cancelled


def test_a_batch_without_semicolons_keeps_its_variables(mssql_worker):
    worker, rec = mssql_worker
    run(worker, rec, "v", "DECLARE @x int = 41\nSELECT @x + 1 AS answer")
    assert rec.of("v", "rows")[0].rows == [[42]]


def test_live_messages_come_before_the_rows(mssql_worker):  # Task 4 hook end to end
    worker, rec = mssql_worker
    run(worker, rec, "n", "EXEC dbo.noisy")
    timeline = [(t, e) for t, e in rec.times if getattr(e, "request_id", None) == "n"]
    messages = [(t, e.text) for t, e in timeline if e.type == "message"]
    [rows_at] = [t for t, e in timeline if e.type == "rows"]
    assert [text for _, text in messages] == ["one", "two", "three"]
    assert messages[1][0] < rows_at - 1.5  # "two" was on screen while the batch waited


def test_cancel_inside_a_manual_transaction_keeps_it(mssql_worker):
    worker, rec = mssql_worker
    command(worker, rec, SetAutocommitCommand(request_id="m", enabled=False))
    assert run(worker, rec, "c", "CREATE TABLE #keep (a int); INSERT #keep VALUES (1)").in_transaction
    worker.submit(ExecuteCommand(request_id="s", sql="WAITFOR DELAY '00:00:30'"))
    rec.wait_for(lambda e: e.type == "started" and e.request_id == "s", timeout=10)
    time.sleep(0.3)
    started = time.monotonic()
    worker.cancel("s")
    done = rec.wait_for(finished("s"), timeout=10)
    assert done.cancelled and done.in_transaction and time.monotonic() - started < 2.5
    assert not command(worker, rec, CommitCommand(request_id="k")).in_transaction


def test_paused_large_select_inside_a_transaction_then_commit(mssql_worker):
    worker, rec = mssql_worker
    command(worker, rec, SetAutocommitCommand(request_id="m", enabled=False))
    run(worker, rec, "t", "CREATE TABLE #page (a int); INSERT #page VALUES (1)")
    worker.submit(ExecuteCommand(request_id="big", sql=LARGE_SELECT, page_size=500))
    assert rec.wait_for(paused("big", 500), timeout=30).has_more
    assert not command(worker, rec, CommitCommand(request_id="c")).in_transaction  # discards the paused result first
    run(worker, rec, "n", "SELECT COUNT(*) FROM #page")
    assert rec.of("n", "rows")[0].rows == [[1]]


def test_a_print_is_shown_exactly_once(mssql_worker):  # Task 4's hook: live, never again from take_messages()
    worker, rec = mssql_worker
    run(worker, rec, "p", "PRINT 'once'; SELECT 1 AS a")
    assert [m.text for m in rec.of("p", "message")] == ["once"]
    assert rec.of("p", "rows")[0].rows == [[1]]


def test_messages_from_internal_queries_never_reach_a_request(mssql_worker, monkeypatch):
    for name in ("_STATE_QUERY", "_COMMIT"):  # each one prints first now
        monkeypatch.setattr(mssql_adapter, name, "PRINT 'internal'; " + getattr(mssql_adapter, name))
    worker, rec = mssql_worker
    run(worker, rec, "a", "SELECT 1 AS a")
    command(worker, rec, SetAutocommitCommand(request_id="m", enabled=False))
    assert run(worker, rec, "b", "SELECT TOP 1 id FROM dbo.big_table").in_transaction
    assert not command(worker, rec, CommitCommand(request_id="k")).in_transaction
    run(worker, rec, "c", "SELECT 2 AS b")
    assert not [e for e in rec.events if e.type == "message" and "internal" in e.text]


def test_after_a_cancel_the_next_request_gets_its_own_rows(mssql_worker):  # 03a lesson: protocol desync
    worker, rec = mssql_worker
    worker.submit(ExecuteCommand(request_id="w", sql="SELECT 1 AS a; WAITFOR DELAY '00:00:30'; SELECT 2 AS b"))
    rec.wait_for(lambda e: e.type == "started" and e.request_id == "w", timeout=10)
    time.sleep(0.3)
    worker.cancel("w")
    assert rec.wait_for(finished("w"), timeout=10).cancelled
    run(worker, rec, "x", "SELECT 'mine' AS m")
    assert [e.rows for e in rec.of("x", "rows")] == [[["mine"]]]
    worker.submit(ExecuteCommand(request_id="p", sql="EXEC dbo.two_results 3", page_size=2))
    assert rec.wait_for(paused("p", 2), timeout=10).has_more
    run(worker, rec, "y", "SELECT 'mine again' AS m")  # discards the paused procedure first
    assert [e.rows for e in rec.of("y", "rows")] == [[["mine again"]]]
    assert len(rec.of("p", "result_set")) == 1  # the procedure's second set was never read


def test_an_error_that_ends_the_transaction_updates_the_badge(mssql_worker):  # 03a lesson
    worker, rec = mssql_worker
    assert run(worker, rec, "b", "SET XACT_ABORT ON; BEGIN TRAN").in_transaction
    worker.submit(ExecuteCommand(request_id="e", sql="SELECT 1/0"))
    done = rec.wait_for(finished("e"), timeout=30)
    assert rec.of("e", "error")[0].code == "8134"
    assert not done.in_transaction  # XACT_ABORT rolled it back: read from the server, not the cache
    run(worker, rec, "o", "SET XACT_ABORT OFF")
