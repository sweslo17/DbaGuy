import time

import pytest

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.mysql.adapter import MysqlAdapter
from dbaguy_backend.domain.execution import (
    CommitCommand, ExecuteCommand, FetchMoreCommand, RollbackCommand, SetAutocommitCommand,
)
from dbaguy_backend.sessions.worker import SessionWorker
from tests.fake_db import FakeLease
from tests.support import MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, make_mysql_profile, requires_mysql
from tests.test_sessions import Recorder, finished, paused

pytestmark = requires_mysql
LARGE_SELECT = "SELECT a.id FROM big_table a CROSS JOIN big_table b"


@pytest.fixture
def mysql_worker():
    lease = FakeLease(MysqlAdapter().open(make_mysql_profile(), Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None))
    worker = SessionWorker("mysql", lambda: lease)
    worker.start()
    recorder = Recorder()
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


def test_statement_timeout_on_mysql(mysql_worker):
    worker, rec = mysql_worker
    worker.submit(ExecuteCommand(request_id="t", sql="SELECT SLEEP(5)", timeout_s=0.3))
    done = rec.wait_for(finished("t"), timeout=30)
    assert done.timed_out and done.cancelled and done.elapsed_ms < 3000
    assert rec.of("t", "message")[-1].text == "Timed out after 0.3 s"
    assert not run(worker, rec, "u", "SELECT 1").cancelled


def test_cancel_inside_a_manual_transaction_keeps_the_transaction(mysql_worker):
    worker, rec = mysql_worker
    command(worker, rec, SetAutocommitCommand(request_id="m", enabled=False))
    run(worker, rec, "c", "CREATE TEMPORARY TABLE keep_rows (id INT)")
    assert run(worker, rec, "i", "INSERT INTO keep_rows VALUES (1)").in_transaction
    worker.submit(ExecuteCommand(request_id="s", sql="SELECT SLEEP(30)"))
    rec.wait_for(lambda e: e.type == "started" and e.request_id == "s", timeout=10)
    time.sleep(0.3)
    started = time.monotonic()
    worker.cancel("s")
    done = rec.wait_for(finished("s"), timeout=10)
    assert done.cancelled and time.monotonic() - started < 2
    assert done.in_transaction  # KILL QUERY ends the statement, not the transaction
    assert not command(worker, rec, CommitCommand(request_id="k")).in_transaction
    run(worker, rec, "n", "SELECT COUNT(*) FROM keep_rows")
    assert rec.of("n", "rows")[0].rows == [[1]]


def test_paused_large_select_inside_a_transaction_does_not_end_it(mysql_worker):
    worker, rec = mysql_worker
    command(worker, rec, SetAutocommitCommand(request_id="m", enabled=False))
    run(worker, rec, "t", "CREATE TEMPORARY TABLE keep_page (id INT)")
    run(worker, rec, "i", "INSERT INTO keep_page VALUES (1)")
    worker.submit(ExecuteCommand(request_id="big", sql=LARGE_SELECT, page_size=500))
    assert rec.wait_for(paused("big", 500), timeout=30).has_more
    assert not command(worker, rec, CommitCommand(request_id="c")).in_transaction  # discards the paused result first
    run(worker, rec, "n", "SELECT COUNT(*) FROM keep_page")
    assert rec.of("n", "rows")[0].rows == [[1]]


def test_manual_mode_reports_the_transaction_and_rolls_back(mysql_worker):
    worker, rec = mysql_worker
    command(worker, rec, SetAutocommitCommand(request_id="m", enabled=False))
    run(worker, rec, "t", "CREATE TEMPORARY TABLE badge (id INT)")
    assert run(worker, rec, "u", "INSERT INTO badge VALUES (1)").in_transaction
    assert not command(worker, rec, RollbackCommand(request_id="r")).in_transaction
    run(worker, rec, "n", "SELECT COUNT(*) FROM badge")
    assert rec.of("n", "rows")[0].rows == [[0]]


def test_a_call_paused_between_its_result_sets_keeps_the_protocol_in_step(mysql_worker):  # Review Focus 4
    worker, rec = mysql_worker
    # 501 rows: the page of 500 plus the peeked row reads the first set's end, while the second set is still coming.
    # A CALL marks auto-commit for re-reading; the pause must not send that query in between (Commands out of sync).
    worker.submit(ExecuteCommand(request_id="p", sql="CALL two_results(501)", page_size=500))
    assert rec.wait_for(paused("p", 500), timeout=30).has_more
    worker.submit(FetchMoreCommand(request_id="p", count=500))
    done = rec.wait_for(finished("p"), timeout=30)
    assert not rec.of("p", "error") and not done.cancelled
    assert [e.rows for e in rec.of("p", "rows")][-2:] == [[[501, "row 501"]], [[100000]]]
    assert not run(worker, rec, "n", "SELECT 1").cancelled
    assert rec.of("n", "rows")[0].rows == [[1]]
