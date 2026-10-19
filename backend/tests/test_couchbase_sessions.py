"""The session worker over a real Couchbase connection: paging a `*` result, the statement timeout, raw JSON exports,
CSV exports of nested values, and what the engine refuses (transactions, INSERT exports)."""

import time

import pytest

from dbaguy_backend.adapters.base import Endpoint, ReadAheadState
from dbaguy_backend.adapters.couchbase.adapter import PAUSED_RESULT_MAX_IDLE_S, CouchbaseAdapter
from dbaguy_backend.domain.execution import ExecuteCommand, ExportCommand, FetchMoreCommand, QueryContext
from dbaguy_backend.services.safety import SafetyPolicy
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks, paused_result_closed_text
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.jsontext import one_line
from tests.fake_db import FakeLease
from tests.fake_ssh import wait_until
from tests.support import (
    COUCHBASE_PASSWORD, LONG_AGGREGATE, completed_request_state, make_couchbase_profile, requires_couchbase, scaled,
)
from tests.test_couchbase_adapter import STREAM, U_KEYORDER
from tests.test_dev_env import U_NUMBERS
from tests.test_sessions import Recorder, finished, paused

pytestmark = requires_couchbase
APP = QueryContext(bucket="dbaguy", scope="app")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def start_cb_worker(clock=time.monotonic, **profile_fields) -> tuple[SessionWorker, Recorder]:
    adapter = CouchbaseAdapter()
    profile = make_couchbase_profile(**profile_fields)
    lease = FakeLease(adapter.open(profile, Endpoint(*adapter.endpoint_target(profile)), COUCHBASE_PASSWORD, None))
    worker = SessionWorker("couchbase", lambda: lease, clock=clock)
    worker.start()
    worker.configure(WorkerHooks(
        dialect=Dialect.SQLPP, capabilities=adapter.capabilities,
        check=lambda sql, confirmed: SafetyPolicy().check(sql, Dialect.SQLPP, "standard", False, confirmed),
    ))
    recorder = Recorder()
    worker.set_emitter(recorder)
    return worker, recorder


@pytest.fixture
def cb_worker():
    worker, recorder = start_cb_worker()
    yield worker, recorder
    worker.close()


def page_through(worker, rec, rid, count=5000, timeout=60.0):
    """Load more after every pause until `rid` finishes; the number of rows that arrived."""
    deadline, asked = time.monotonic() + timeout, 0
    while not rec.of(rid, "finished"):
        assert time.monotonic() < deadline, rec.types()
        pauses = [e for e in rec.of(rid, "result_set_end") if e.has_more]
        if len(pauses) > asked:
            asked = len(pauses)
            worker.submit(FetchMoreCommand(request_id=rid, count=count))
        time.sleep(0.01)
    return sum(len(e.rows) for e in rec.of(rid, "rows"))


def test_paging_20k_events(cb_worker):
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="e", sql="SELECT META(d).id, * FROM events AS d", query_context=APP,
                                 page_size=5000))
    assert page_through(worker, rec, "e") == 20_000 and not rec.of("e", "error")
    names = [c.name for c in rec.of("e", "result_set")[0].columns]
    assert names[0] == "id" and names[-1] == "(other fields)"
    assert len([e for e in rec.of("e", "result_set_end") if e.has_more]) == 3  # paused at 5k, 10k, 15k
    assert any("20,000 results" in e.text for e in rec.of("e", "message"))  # the metrics line at the end


def test_statement_timeout(cb_worker):
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="t", sql=LONG_AGGREGATE, timeout_s=0.5))
    done = rec.wait_for(finished("t"), timeout=30)
    assert done.timed_out and done.elapsed_ms < scaled(3.0) * 1000
    worker.submit(ExecuteCommand(request_id="u", sql="SELECT RAW 1"))
    assert not rec.wait_for(finished("u")).cancelled
    assert rec.of("u", "rows")[0].rows == [[1]]


def test_a_cancel_from_the_client_stops_a_long_aggregate(cb_worker):
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="c", sql=LONG_AGGREGATE))
    rec.wait_for(lambda e: e.type == "started" and e.request_id == "c")
    time.sleep(0.5)
    cancelled_at = time.monotonic()
    worker.cancel("c")
    done = rec.wait_for(finished("c"), timeout=30)
    assert done.cancelled and not rec.of("c", "error") and time.monotonic() - cancelled_at < scaled(1.0)
    worker.submit(ExecuteCommand(request_id="d", sql="SELECT RAW 2"))
    assert not rec.wait_for(finished("d")).cancelled and rec.of("d", "rows")[0].rows == [[2]]


def test_an_error_after_rows_keeps_the_rows_sent(cb_worker):  # Review Focus 2, through the worker
    worker, rec = cb_worker
    sql = 'SELECT RAW CASE WHEN i < 3000 THEN i ELSE ABORT("stop at 3000") END FROM ARRAY_RANGE(0, 10000) AS i'
    worker.submit(ExecuteCommand(request_id="a", sql=sql, page_size=10_000))
    rec.wait_for(finished("a"), timeout=30)
    [error] = rec.of("a", "error")
    assert error.code == "5011" and "stop at 3000" in error.message
    rows = [row for e in rec.of("a", "rows") for row in e.rows]
    assert rows and rows == [[i] for i in range(len(rows))]


def test_jsonl_export_is_the_server_text(cb_worker, tmp_path):  # Review Focus 1
    worker, rec = cb_worker
    target = tmp_path / "users.jsonl"
    sql = 'SELECT RAW d FROM dbaguy.app.users AS d USE KEYS ["u_numbers", "u_keyorder"]'
    worker.submit(ExportCommand(request_id="x", sql=sql, format="jsonl", path=str(target)))
    rec.wait_for(finished("x"), timeout=30)
    assert not rec.of("x", "error")
    assert sorted(target.read_text().splitlines()) == sorted([U_NUMBERS, one_line(U_KEYORDER)])


def test_json_export_of_a_star_result_delivers_the_peeked_rows(cb_worker, tmp_path):  # R-04a-C14
    worker, rec = cb_worker
    target = tmp_path / "events.jsonl"
    sql = "SELECT META(d).id, * FROM events AS d WHERE META(d).id BETWEEN 'e100000' AND 'e100999' ORDER BY META(d).id"
    worker.submit(ExportCommand(request_id="s", sql=sql, format="jsonl", path=str(target), query_context=APP))
    rec.wait_for(finished("s"), timeout=30)
    assert not rec.of("s", "error")
    lines = target.read_text().splitlines()
    assert len(lines) == 1000 and lines[0].startswith('{"id":"e100000","d":{')  # the first page (500) is not lost
    assert [line.split('"', 4)[3] for line in lines] == [f"e{100000 + i}" for i in range(1000)]


def test_csv_export_writes_nested_values_as_json(cb_worker, tmp_path):
    worker, rec = cb_worker
    target = tmp_path / "u1.csv"
    worker.submit(ExportCommand(request_id="c", sql='SELECT * FROM dbaguy.app.users AS d USE KEYS "u1"', format="csv",
                                path=str(target)))
    rec.wait_for(finished("c"), timeout=30)
    assert target.read_text().splitlines() == [
        "name,age,tags,address,(other fields)", 'Ann,31,"[""a"",""b""]","{""city"":""Taipei"",""zip"":""100""}",',
    ]


def test_begin_work_and_insert_exports_are_refused(cb_worker, tmp_path):
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="b", sql="BEGIN WORK"))
    rec.wait_for(finished("b"))
    assert rec.of("b", "error")[0].code == "transactions_unsupported"
    worker.submit(ExportCommand(request_id="i", sql="SELECT RAW 1", format="insert", path=str(tmp_path / "x.sql")))
    rec.wait_for(finished("i"))
    assert rec.of("i", "error")[0].code == "unsupported_format"


def test_an_idle_paused_result_is_closed_and_frees_the_server_request():  # R-04-6, A13, R-04a-T11b
    clock = Clock()
    worker, rec = start_cb_worker(clock)
    try:
        worker.submit(ExecuteCommand(request_id="p", sql=STREAM, page_size=500))
        rec.wait_for(paused("p", 500), timeout=30)  # reading ahead stopped here: the idle clock starts
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.FULL, 30)
        ident = worker.connection.last_client_context_id
        time.sleep(1.2)  # system:completed_requests keeps requests over 1 s
        clock.now += 20  # a Load more 20 s into the pause: served from the buffers, it does not restart the clock
        worker.submit(FetchMoreCommand(request_id="p", count=500))
        rec.wait_for(paused("p", 1000), timeout=30)
        # the pause after a Load more has no stall start until its first step re-evaluates it (b47c992): wait for it
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.FULL, 30)
        clock.now += PAUSED_RESULT_MAX_IDLE_S - 20 - 1  # 1 s short of the limit, counted from the first pause
        worker.check_paused_result()  # below the limit it queues nothing
        assert not rec.of("p", "finished")
        clock.now += 1  # the limit after the FIRST pause (not after the Load more)
        worker.check_paused_result()
        rec.wait_for(finished("p"), timeout=10)
        assert rec.of("p", "message")[-1].text == paused_result_closed_text(PAUSED_RESULT_MAX_IDLE_S)
        assert sum(len(e.rows) for e in rec.of("p", "rows")) == 1000 and not rec.of("p", "error")
        assert completed_request_state(ident) == "closed"  # our close, not the server's stall kill (`fatal`)
        worker.submit(ExecuteCommand(request_id="n", sql="SELECT RAW 1"))
        rec.wait_for(finished("n"))
        assert rec.of("n", "rows")[0].rows == [[1]]
    finally:
        worker.close()
