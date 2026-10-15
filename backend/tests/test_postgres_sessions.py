"""PostgreSQL behind a SessionWorker: paging and discarding must never change the user's transaction state."""

import pytest

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.domain.execution import ExecuteCommand
from dbaguy_backend.sessions.worker import SessionWorker
from tests.fake_db import FakeLease
from tests.support import PG_HOST, PG_PASSWORD, PG_PORT, make_pg_profile, requires_pg
from tests.test_sessions import Recorder, finished, paused

pytestmark = requires_pg

LARGE_SELECT = "SELECT g FROM generate_series(1, 5000000) g"


@pytest.fixture
def pg_worker():
    lease = FakeLease(PostgresAdapter().open(make_pg_profile(), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None))
    worker = SessionWorker("pg", lambda: lease)
    worker.start()
    recorder = Recorder()
    worker.set_emitter(recorder)
    yield worker, recorder
    worker.close()


def run(worker, recorder, rid, sql, page_size=500):
    worker.submit(ExecuteCommand(request_id=rid, sql=sql, page_size=page_size))
    done = recorder.wait_for(finished(rid), timeout=30)
    errors = recorder.of(rid, "error")
    assert not errors, errors[0].message
    return done


def test_paused_large_select_inside_a_transaction_does_not_roll_it_back(pg_worker):
    worker, rec = pg_worker
    run(worker, rec, "t", "CREATE TEMP TABLE c1_keep (id int)")
    run(worker, rec, "b", "BEGIN")
    run(worker, rec, "i", "INSERT INTO c1_keep VALUES (1)")

    worker.submit(ExecuteCommand(request_id="big", sql=LARGE_SELECT, page_size=500))
    assert rec.wait_for(paused("big", 500), timeout=30).has_more  # paused: the result holds the connection

    commit = run(worker, rec, "c", "COMMIT")  # discards the paused result first
    assert rec.of("big", "finished")[0].transaction_failed is False
    assert commit.in_transaction is False and commit.transaction_failed is False
    run(worker, rec, "n", "SELECT count(*) FROM c1_keep")
    assert rec.of("n", "rows")[0].rows == [["1"]]


def test_transaction_failed_is_reported_after_an_error_inside_a_transaction(pg_worker):
    worker, rec = pg_worker
    assert run(worker, rec, "b", "BEGIN").transaction_failed is False
    worker.submit(ExecuteCommand(request_id="e", sql="SELECT 1 / 0"))
    failed = rec.wait_for(finished("e"))
    assert rec.of("e", "error")[0].code == "22012"
    assert failed.in_transaction is True and failed.transaction_failed is True
    rolled_back = run(worker, rec, "r", "ROLLBACK")
    assert rolled_back.in_transaction is False and rolled_back.transaction_failed is False
