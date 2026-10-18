import time

import pytest

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.mssql import adapter as mssql_adapter
from dbaguy_backend.adapters.mssql.adapter import MssqlAdapter
from dbaguy_backend.adapters.mssql.cancel import BUSY_WARNING, WAITING_DROPPED_MESSAGE, WAITING_WARNING, WEDGE_S
from dbaguy_backend.domain.execution import CommitCommand, ExecuteCommand, SetAutocommitCommand
from dbaguy_backend.sessions.worker import SessionWorker
from tests.fake_db import FakeLease
from tests.support import (
    MSSQL_HOST, MSSQL_PASSWORD, MSSQL_PORT, make_mssql_profile, requires_mssql, scaled, unique_name,
)
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
    done = rec.wait_for(finished(rid), timeout=scaled(30))
    assert not rec.of(rid, "error"), rec.of(rid, "error")[0].message
    return done


def command(worker, rec, cmd):
    worker.submit(cmd)
    done = rec.wait_for(finished(cmd.request_id), timeout=scaled(30))
    assert not rec.of(cmd.request_id, "error")
    return done


def test_statement_timeout(mssql_worker):
    worker, rec = mssql_worker
    worker.submit(ExecuteCommand(request_id="t", sql="WAITFOR DELAY '00:00:05'", timeout_s=0.3))
    done = rec.wait_for(finished("t"), timeout=scaled(30))
    assert done.timed_out and done.cancelled and done.elapsed_ms < scaled(3000)
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
    rec.wait_for(lambda e: e.type == "started" and e.request_id == "s", timeout=scaled(10))
    time.sleep(0.3)
    started = time.monotonic()
    worker.cancel("s")
    done = rec.wait_for(finished("s"), timeout=scaled(10))
    assert done.cancelled and done.in_transaction and time.monotonic() - started < scaled(2.5)
    assert not command(worker, rec, CommitCommand(request_id="k")).in_transaction


def test_paused_large_select_inside_a_transaction_then_commit(mssql_worker):
    worker, rec = mssql_worker
    command(worker, rec, SetAutocommitCommand(request_id="m", enabled=False))
    run(worker, rec, "t", "CREATE TABLE #page (a int); INSERT #page VALUES (1)")
    worker.submit(ExecuteCommand(request_id="big", sql=LARGE_SELECT, page_size=500))
    assert rec.wait_for(paused("big", 500), timeout=scaled(30)).has_more
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
    rec.wait_for(lambda e: e.type == "started" and e.request_id == "w", timeout=scaled(10))
    time.sleep(0.3)
    worker.cancel("w")
    assert rec.wait_for(finished("w"), timeout=scaled(10)).cancelled
    run(worker, rec, "x", "SELECT 'mine' AS m")
    assert [e.rows for e in rec.of("x", "rows")] == [[["mine"]]]
    worker.submit(ExecuteCommand(request_id="p", sql="EXEC dbo.two_results 3", page_size=2))
    assert rec.wait_for(paused("p", 2), timeout=scaled(10)).has_more
    run(worker, rec, "y", "SELECT 'mine again' AS m")  # discards the paused procedure first
    assert [e.rows for e in rec.of("y", "rows")] == [[["mine again"]]]
    assert len(rec.of("p", "result_set")) == 1  # the procedure's second set was never read


def test_an_error_that_ends_the_transaction_updates_the_badge(mssql_worker):  # 03a lesson
    worker, rec = mssql_worker
    assert run(worker, rec, "b", "SET XACT_ABORT ON; BEGIN TRAN").in_transaction
    worker.submit(ExecuteCommand(request_id="e", sql="SELECT 1/0"))
    done = rec.wait_for(finished("e"), timeout=scaled(30))
    assert rec.of("e", "error")[0].code == "8134"
    assert not done.in_transaction  # XACT_ABORT rolled it back: read from the server, not the cache
    run(worker, rec, "o", "SET XACT_ABORT OFF")


def test_a_nul_character_is_refused_before_the_safety_check_and_the_server(mssql_worker):  # Important 3
    worker, rec = mssql_worker
    run(worker, rec, "c", "CREATE TABLE #n (id int); INSERT #n VALUES (1), (2)")
    worker.submit(ExecuteCommand(request_id="d", sql="DELETE FROM #n\x00 WHERE id = 1"))
    rec.wait_for(finished("d"), timeout=scaled(30))
    [error] = rec.of("d", "error")
    assert error.code == "invalid_text" and (error.position.line, error.position.column) == (1, 15)
    run(worker, rec, "k", "SELECT COUNT(*) FROM #n")
    assert rec.of("k", "rows")[0].rows == [[2]]  # the server never saw a DELETE without its WHERE


def test_a_timeout_stops_a_select_blocked_on_a_row_lock(mssql_worker):  # Important 1; R-03b-F6: by escalation
    worker, rec = mssql_worker
    table = unique_name("##zz_locktimeout")  # global temp tables are shared by every run on the server
    timeout_s = scaled(1)  # the SELECT must be blocked inside its row read when the timeout comes: on a slow machine it
    # needs longer to get there, and the timeout is the first Cancel (a flag the worker sees between rows otherwise)
    locker = MssqlAdapter().open(make_mssql_profile(), Endpoint(MSSQL_HOST, MSSQL_PORT), MSSQL_PASSWORD, None)
    try:
        for sql in (f"CREATE TABLE {table} (id int PRIMARY KEY, v int)",
                    f"INSERT {table} SELECT TOP 5000 id, id AS v FROM dbo.big_table ORDER BY id"):
            locker.run(sql).close()
        locker.set_autocommit(False)
        # A real change: `SET v = v` writes nothing, and once the page has been flushed a reader skips its lock
        locker.run(f"UPDATE {table} SET v = v + 1 WHERE id = 3000").close()
        worker.submit(ExecuteCommand(request_id="t", sql=f"SELECT id, v FROM {table} ORDER BY id",
                                     page_size=5000, timeout_s=timeout_s))
        done = rec.wait_for(finished("t"), timeout=timeout_s + WEDGE_S + scaled(10))
        # The timeout is a first Cancel; blocked inside a row read the flag cannot reach the worker, so a second later
        # the auto-commit connection is dropped with the honest text (no cross-thread cancel any more, R-03b-F6).
        [error] = rec.of("t", "error")
        assert error.code == "connection_lost" and error.message == WAITING_DROPPED_MESSAGE
        assert done.elapsed_ms < (timeout_s + WEDGE_S + scaled(1.5)) * 1000 and not done.in_transaction
    finally:
        locker.rollback()
        locker.set_autocommit(True)
        locker.run(f"DROP TABLE {table}").close()
        locker.close()


def test_a_timeout_on_a_batch_that_may_hold_a_transaction_warns_and_the_next_cancel_drops(mssql_worker):  # T8h
    worker, rec = mssql_worker
    command(worker, rec, SetAutocommitCommand(request_id="m", enabled=False))
    run(worker, rec, "p", "CREATE PROCEDURE #zz_busy AS BEGIN DECLARE @t TABLE (a int); DECLARE @i int = 0; "
                          "WHILE @i < 3000000 BEGIN INSERT @t VALUES (@i); SET @i += 1; END; SELECT 'done' AS d; END")
    for attempt in range(3):  # a rare quiet moment lets the timeout cancel cleanly: try again
        rid = f"b{attempt}"
        worker.submit(ExecuteCommand(request_id=rid, sql="EXEC #zz_busy", timeout_s=scaled(0.3)))
        # The warning says "row counts" while the server keeps sending them, "waiting" when a loaded server went quiet
        event = rec.wait_for(lambda e: getattr(e, "request_id", None) == rid and (
            e.type == "finished" or (e.type == "message" and e.text in (BUSY_WARNING, WAITING_WARNING))),
            timeout=scaled(10))
        if event.type == "message":
            break
        assert event.cancelled and event.timed_out
    else:
        pytest.skip("the loop found a quiet moment three times")
    assert event.severity == "warning"
    time.sleep(0.5)
    assert not rec.of(rid, "finished")  # the timeout only warned: the transaction's work is still there
    worker.cancel(rid)  # the user's Cancel after the timeout drops the connection
    done = rec.wait_for(finished(rid), timeout=scaled(10))
    assert rec.of(rid, "error")[0].code == "connection_lost" and not done.in_transaction
