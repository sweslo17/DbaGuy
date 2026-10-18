"""A Cancel that lands between the worker's last cancel check and the driver call is kept (Task 6b: `arm_cancel` /
`disarm_cancel`), for every engine. The window is forced the way the investigator's repro did: a hook right after the
worker's last check cancels the request, while the driver has nothing running yet."""

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from dbaguy_backend.adapters.base import DatabaseConnection, Endpoint, StatementCancelled
from dbaguy_backend.adapters.mssql.adapter import MssqlAdapter
from dbaguy_backend.adapters.mysql.adapter import MysqlAdapter
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.domain.classification import Classification, StatementKind
from dbaguy_backend.domain.execution import ExecuteCommand, ExportCommand, FetchMoreCommand, SetAutocommitCommand
from dbaguy_backend.domain.formats import FormatKind
from dbaguy_backend.domain.profiles import SqliteProfile
from dbaguy_backend.services.safety import GuardAction, GuardDecision
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from tests.fake_db import FakeConnection, FakeLease
from tests.support import (
    MSSQL_HOST,
    MSSQL_PASSWORD,
    MSSQL_PORT,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    PG_HOST,
    PG_PASSWORD,
    PG_PORT,
    make_mssql_profile,
    make_mysql_profile,
    make_pg_profile,
    requires_mssql,
    requires_mysql,
    requires_pg,
    scaled,
)
from tests.test_sessions import Recorder, finished, paused

CANCEL_WITHIN_S = 3.0  # a kept cancel sends nothing: the request ends with local work and the state read only
LOST_AFTER_S = 15.0  # a cancel that was lost is given this long before the test fails (and closes the session)


def reads(sql: str, confirmed: bool) -> GuardDecision:  # an export needs a read; the window is the same for both
    return GuardDecision(action=GuardAction.RUN, classification=Classification(kind=StatementKind.READ))


def cancel_from_another_thread(worker: SessionWorker, request_id: str, times: int = 1) -> None:
    """`worker.cancel()` as the WebSocket handler calls it, from its own thread; returns once it has been delivered."""
    for _ in range(times):
        sender = threading.Thread(target=worker.cancel, args=(request_id,))
        sender.start()
        sender.join(10)
        assert not sender.is_alive(), "the cancel never returned"


def cancel_after_last_check(worker: SessionWorker, request_id: str, times: int = 1) -> dict:
    """Once, right after the worker's last cancel check before run(), cancel `request_id` (`times` Cancels): the driver
    is idle then, so only the armed connection can keep it. Returns {"at": when the cancel was delivered}."""
    check, state = worker._raise_if_cancelled, {"at": None}

    def check_then_cancel() -> None:
        check()
        if state["at"] is None and worker._running_request == request_id:
            cancel_from_another_thread(worker, request_id, times)
            state["at"] = time.monotonic()

    worker._raise_if_cancelled = check_then_cancel
    return state


def cancel_after_run(worker: SessionWorker, request_id: str) -> dict:
    """Once, right after run() has returned for `request_id` (the connection disarmed, nothing read yet), cancel it:
    PostgreSQL in a transaction has only DECLAREd the cursor then, and its backend is idle (R-04a-6b1). Returns {"at": when
    the cancel was delivered}."""
    run_statement, state = worker._run_statement, {"at": None}

    def run_then_cancel(req, *args):
        result = run_statement(req, *args)
        if state["at"] is None and req.request_id == request_id:
            cancel_from_another_thread(worker, request_id)
            state["at"] = time.monotonic()
        return result

    worker._run_statement = run_then_cancel
    return state


def submit(worker: SessionWorker, path: str, request_id: str, sql: str, folder: Path) -> Path:
    """Execute or export `sql`; returns the export's file (never written by an execute)."""
    target = folder / f"{request_id}.csv"
    if path == "execute":
        worker.submit(ExecuteCommand(request_id=request_id, sql=sql))
    else:
        worker.submit(ExportCommand(request_id=request_id, sql=sql, format=FormatKind.CSV, path=str(target)))
    return target


def start_worker(open_connection: Callable[[], DatabaseConnection], name: str) -> tuple[SessionWorker, Recorder]:
    """A started worker whose connection is opened on its own thread (SQLite's must be), with a recorder attached."""
    worker = SessionWorker(name, lambda: FakeLease(open_connection()))
    worker.start()
    worker.configure(WorkerHooks(check=reads))
    recorder = Recorder()
    worker.set_emitter(recorder)
    return worker, recorder


# ---- 1. the worker, on the fake connection: every ordering forced ----

@pytest.fixture
def fake():
    worker, recorder = start_worker(FakeConnection, "window")
    yield worker, worker.connection, recorder
    worker.close()


def window_calls(conn: FakeConnection) -> list[str]:
    return [call for call in conn.calls if call in ("arm", "disarm")]


@pytest.mark.parametrize("path", ["execute", "export"])
@pytest.mark.parametrize("times", [1, 2])  # a repeated Cancel routes to force_cancel(): kept the same way
def test_a_cancel_between_the_last_check_and_run_ends_the_request_before_it_runs(fake, path, times, tmp_path):
    worker, conn, rec = fake
    delivered = cancel_after_last_check(worker, "w", times)
    target = submit(worker, path, "w", "block", tmp_path)  # `block` waits 10 s for a cancel that reaches it
    done = rec.wait_for(finished("w"), timeout=scaled(5))
    assert delivered["at"] is not None and done.cancelled and not rec.of("w", "error")
    assert conn.runs == [] and conn.calls == ["arm", *["keep"] * times, "run", "disarm"]
    assert conn.interruptions == ["cancel", "force"][:times]  # the worker's routing is unchanged
    assert [m.text for m in rec.of("w", "message")] == ["Query cancelled"] and not target.exists()
    nxt = submit(worker, path, "n", "rows 1", tmp_path)
    assert not rec.wait_for(finished("n"), timeout=scaled(5)).cancelled  # the kept cancel belonged to "w" only
    assert conn.runs == ["rows 1"] and (path == "execute" or nxt.read_text() == "n\n0\n")


@pytest.mark.parametrize("path", ["execute", "export"])
@pytest.mark.parametrize("sql", ["rows 1", "fail boom"])
def test_a_cancel_after_disarm_with_nothing_running_never_reaches_the_next_statement(fake, path, sql, tmp_path):
    worker, conn, rec = fake
    disarm = conn.disarm_cancel

    def disarm_then_cancel() -> None:  # run() has returned or raised: the statement is over, the worker not yet
        disarm()
        if worker._running_request == "a":
            cancel_from_another_thread(worker, "a")

    conn.disarm_cancel = disarm_then_cancel
    submit(worker, path, "a", sql, tmp_path)
    rec.wait_for(finished("a"), timeout=scaled(5))
    assert conn.interruptions == ["cancel"]  # it reached the connection, idle: ignored, like a real server
    conn.disarm_cancel = disarm
    submit(worker, path, "b", "rows 1", tmp_path)
    assert not rec.wait_for(finished("b"), timeout=scaled(5)).cancelled
    assert conn.runs == [sql, "rows 1"] and not rec.of("b", "error")


def test_a_cancel_as_load_more_starts_is_honoured_before_any_row(fake):
    worker, conn, rec = fake
    worker.submit(ExecuteCommand(request_id="p", sql="rows 5", page_size=2))
    rec.wait_for(paused("p", 2), timeout=scaled(5))
    set_running = worker._set_running

    def running_then_cancel(request_id):  # Load more made "p" the running request again; no driver call yet
        cancelled = set_running(request_id)
        if request_id == "p":
            cancel_from_another_thread(worker, "p")
        return cancelled

    worker._set_running = running_then_cancel
    worker.submit(FetchMoreCommand(request_id="p", count=2))
    assert rec.wait_for(finished("p"), timeout=scaled(5)).cancelled
    assert sum(len(e.rows) for e in rec.of("p", "rows")) == 2, "a page was sent after the cancel"
    assert [m.text for m in rec.of("p", "message")] == ["Query cancelled"]


def cancel_at_arm(worker: SessionWorker, conn: FakeConnection, request_id: str) -> None:
    """A cancel right after arm_cancel(), before the worker's last check: the worker's own flag stops the request."""
    arm = conn.arm_cancel

    def arm_then_cancel() -> None:
        arm()
        cancel_from_another_thread(worker, request_id)

    conn.arm_cancel = arm_then_cancel


@pytest.mark.parametrize("path", ["execute", "export"])
@pytest.mark.parametrize("way_out", ["returns", "fails", "crashes", "cancelled at the check", "kept cancel"])
def test_every_way_out_disarms(fake, path, way_out, tmp_path):
    worker, conn, rec = fake
    sql = {"returns": "rows 2", "fails": "fail boom", "crashes": "crash"}.get(way_out, "block")
    if way_out == "cancelled at the check":
        cancel_at_arm(worker, conn, "x")
    elif way_out == "kept cancel":
        cancel_after_last_check(worker, "x")
    submit(worker, path, "x", sql, tmp_path)
    done = rec.wait_for(finished("x"), timeout=scaled(5))
    assert done.cancelled is way_out.startswith(("cancelled", "kept"))
    assert window_calls(conn) == ["arm", "disarm"] and not conn.armed
    assert conn.runs == ([] if done.cancelled else [sql])


@pytest.mark.parametrize("kind, cancelled", [(StatementKind.READ, True), (StatementKind.WRITE, False)])
def test_a_cancel_just_after_run_returns_stops_a_read_never_a_completed_write(fake, kind, cancelled):
    worker, conn, rec = fake
    decision = GuardDecision(action=GuardAction.RUN, classification=Classification(kind=kind))
    worker.configure(WorkerHooks(check=lambda sql, confirmed: decision))
    delivered = cancel_after_run(worker, "r")
    worker.submit(ExecuteCommand(request_id="r", sql="rows 3"))
    done = rec.wait_for(finished("r"), timeout=scaled(5))
    assert delivered["at"] is not None and done.cancelled is cancelled and not rec.of("r", "error")
    assert sum(len(e.rows) for e in rec.of("r", "rows")) == (0 if cancelled else 3)  # a read delivers nothing
    worker.submit(ExecuteCommand(request_id="n", sql="rows 1"))
    assert not rec.wait_for(finished("n"), timeout=scaled(5)).cancelled


def test_a_cancel_just_after_an_exports_run_returns_reads_nothing(fake, tmp_path):
    worker, conn, rec = fake
    statement, fetches = conn.statement, []

    def counting(sql, options):  # the first fetch is what runs the query on PostgreSQL in a transaction
        result = statement(sql, options)
        fetch = result.fetch
        result.fetch = lambda n: fetches.append(n) or fetch(n)
        return result

    conn.statement = counting
    delivered = cancel_after_run(worker, "x")
    target = submit(worker, "export", "x", "rows 3", tmp_path)
    done = rec.wait_for(finished("x"), timeout=scaled(5))
    assert delivered["at"] is not None and done.cancelled and not target.exists() and fetches == []


# ---- 2. every real adapter (dev databases): the forced window, and no cancel ever reaching a later statement ----

PROBE = "SELECT 'next' AS v"  # the statement after: it must return its own row


@dataclass(frozen=True)
class Target:
    open: Callable[[Path], DatabaseConnection]
    endless: str  # runs until cancelled (or for 30 s); behind a worker, whose close stops it if the cancel was lost
    long: str  # about 10 s: run directly, where nothing else would stop it


def _open_sqlite(folder: Path) -> DatabaseConnection:
    path = folder / f"window_{uuid4().hex[:8]}.db"
    sqlite3.connect(path).close()
    profile = SqliteProfile(id=uuid4(), name="window", engine="sqlite", path=str(path))
    return SqliteAdapter().open(profile, None, None, None)


TARGETS = {
    "sqlite": Target(
        _open_sqlite,
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c",
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 80000000) SELECT count(*) FROM c",
    ),
    "postgres": Target(
        lambda _: PostgresAdapter().open(make_pg_profile(), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None),
        "SELECT pg_sleep(30)", "SELECT pg_sleep(10)",
    ),
    "mysql": Target(
        lambda _: MysqlAdapter().open(make_mysql_profile(), Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None),
        "SELECT SLEEP(30)", "SELECT SLEEP(10)",
    ),
    "mssql": Target(
        lambda _: MssqlAdapter().open(make_mssql_profile(), Endpoint(MSSQL_HOST, MSSQL_PORT), MSSQL_PASSWORD, None),
        "WAITFOR DELAY '00:00:30'", "WAITFOR DELAY '00:00:10'",
    ),
}
ENGINES = [
    pytest.param("sqlite"),
    pytest.param("postgres", marks=requires_pg),
    pytest.param("mysql", marks=requires_mysql),
    pytest.param("mssql", marks=requires_mssql),
]


@pytest.fixture(params=ENGINES)
def engine(request) -> str:
    return request.param


@pytest.fixture
def connection(engine, tmp_path):
    conn = TARGETS[engine].open(tmp_path)
    yield conn
    conn.close()


def probe(conn: DatabaseConnection) -> list[list]:
    result = conn.run(PROBE)
    try:
        return result.fetch(10)
    finally:
        result.close()


@pytest.mark.parametrize("path", ["execute", "export"])
def test_a_cancel_in_the_window_stops_the_statement_and_the_session_goes_on(engine, path, tmp_path):
    worker, rec = start_worker(lambda: TARGETS[engine].open(tmp_path), f"window-{engine}")
    try:
        delivered = cancel_after_last_check(worker, "w")
        target = submit(worker, path, "w", TARGETS[engine].endless, tmp_path)
        done = rec.wait_for(finished("w"), timeout=scaled(LOST_AFTER_S))  # lost: closing the session stops it
        took = time.monotonic() - delivered["at"]
        assert done.cancelled and not rec.of("w", "error"), rec.of("w", "error")
        assert took < scaled(CANCEL_WITHIN_S), f"the cancel took {took:.1f} s"
        assert not target.exists()
        worker.submit(ExecuteCommand(request_id="n", sql=PROBE))
        nxt = rec.wait_for(finished("n"), timeout=scaled(10))
        assert not nxt.cancelled and not rec.of("n", "error"), rec.of("n", "error")
        assert rec.of("n", "rows")[0].rows == [["next"]]
    finally:
        worker.close()


@pytest.mark.parametrize("how", ["cancel", "force_cancel", "abort"])
def test_an_armed_connection_keeps_the_cancel_and_sends_nothing(engine, connection, how):
    connection.arm_cancel()
    getattr(connection, how)()  # what the worker calls for a Cancel, a repeated one, closing the session
    started = time.monotonic()
    with pytest.raises(StatementCancelled):
        connection.run(TARGETS[engine].long).close()
    assert time.monotonic() - started < scaled(CANCEL_WITHIN_S)
    connection.disarm_cancel()
    connection.disarm_cancel()  # idempotent
    assert probe(connection) == [["next"]]


def test_no_cancel_reaches_a_later_statement(connection):
    connection.arm_cancel()  # armed and disarmed without running: nothing is left behind
    connection.disarm_cancel()
    assert probe(connection) == [["next"]]
    connection.arm_cancel()  # a cancel kept while armed belongs to the statement that never ran
    connection.cancel()
    connection.disarm_cancel()
    assert probe(connection) == [["next"]]
    connection.cancel()  # nothing running, not armed: ignored
    assert probe(connection) == [["next"]]


@requires_pg
@pytest.mark.parametrize("path", ["execute", "export"])
@pytest.mark.parametrize("transaction", ["BEGIN", "manual commit"])
def test_postgres_a_cancel_between_declare_and_the_first_fetch_stops_the_read(path, transaction, tmp_path):
    """R-04a-6b1: in a transaction run() only DECLAREs the cursor and the first FETCH runs the query, so a cancel
    landing in between finds the backend idle. The worker's check after run() stops the read: the cursor is closed with
    CLOSE, and the transaction stays open and healthy."""
    worker, rec = start_worker(lambda: TARGETS["postgres"].open(tmp_path), "window-pg-tx")
    try:
        if transaction == "BEGIN":
            worker.submit(ExecuteCommand(request_id="b", sql="BEGIN"))
        else:
            worker.submit(SetAutocommitCommand(request_id="b", enabled=False))
        assert not rec.wait_for(finished("b"), timeout=scaled(10)).cancelled
        delivered = cancel_after_run(worker, "w")
        target = submit(worker, path, "w", "SELECT pg_sleep(30)", tmp_path)
        done = rec.wait_for(finished("w"), timeout=scaled(LOST_AFTER_S))  # lost: the sleep runs its 30 s
        took = time.monotonic() - delivered["at"]
        assert done.cancelled and not rec.of("w", "error"), rec.of("w", "error")
        assert took < scaled(CANCEL_WITHIN_S), f"the cancel took {took:.1f} s"
        assert done.in_transaction and not done.transaction_failed and not target.exists()
        worker.submit(ExecuteCommand(request_id="n", sql=PROBE))  # inside the same transaction
        nxt = rec.wait_for(finished("n"), timeout=scaled(10))
        assert not nxt.cancelled and not rec.of("n", "error"), rec.of("n", "error")
        assert rec.of("n", "rows")[0].rows == [["next"]] and nxt.in_transaction and not nxt.transaction_failed
    finally:
        worker.close()
