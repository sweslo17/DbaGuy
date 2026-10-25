"""An MCP agent's read on the five engines (spec ⑧ D-08-1, D-08-3, D-08-4, §3.4, §3.6): the frame, paging and its
memory bound, and what each server refuses with the gate taken away (`no_gate`: the worker's backstop allows anything,
as a gate bug would). Scratch objects are zz_08_*, dropped by exact name; Couchbase is only read."""

import socket
import sqlite3
import threading
import time
import tracemalloc
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import mysql.connector
import psycopg
import pytest

from dbaguy_backend.adapters.base import RunOptions
from dbaguy_backend.agent import reader as reader_module
from dbaguy_backend.agent.reader import CallControl, ReadRequest
from dbaguy_backend.agent.sessions import MAX_AGENT_SESSIONS
from dbaguy_backend.agent.tools import ToolFailure
from dbaguy_backend.domain.classification import Classification, StatementKind
from dbaguy_backend.domain.errors import DomainError
from dbaguy_backend.domain.profiles import SqliteProfile
from dbaguy_backend.services.agent_gate import AgentVerdict
from dbaguy_backend.sessions.manager import SessionManager
from dbaguy_backend.sql.dialects import Dialect, explain_head, read_frame

from .agent_support import container, share
from .support import (
    PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, couchbase_query, make_couchbase_profile, make_mssql_profile,
    make_mysql_profile, make_pg_profile, mssql_connect, mysql_driver_kwargs, requires_couchbase, requires_mssql,
    requires_mysql, requires_pg, scaled, wait_until,
)

__all__ = ["container"]


@pytest.fixture
def no_gate(monkeypatch):
    """The worker's backstop allows anything: what reaches the server is what the server alone stops."""
    allowed = AgentVerdict(classification=Classification(kind=StatementKind.READ))
    monkeypatch.setattr("dbaguy_backend.sessions.hooks.agent_read", lambda sql, dialect, purpose="query": allowed)


def _read(container, pid: str, sql: str, dialect: Dialect, **options):
    profile = container.profiles.get(UUID(pid))
    return container.agent_reader.read(ReadRequest(profile=profile, dialect=dialect, sql=sql, **options))


def _agent_workers(container) -> list:
    return [e.worker for e in container.sessions._sessions.values() if e.agent]


def _small_first(dialect: Dialect) -> str:
    """The first row small, every other one 1 MB (review I-1: a page sized by the rows before it took 99 of them)."""
    if dialect == Dialect.SQLITE:
        return ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n LIMIT 1000) "
                "SELECT CASE WHEN i = 1 THEN 'x' ELSE printf('%.*c', 1000000, 'x') END AS big FROM n")
    return "SELECT CASE WHEN n = 1 THEN 'x' ELSE repeat('x', 1000000) END AS big FROM generate_series(1, 1000) AS n"


def _traced(read):
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        outcome = read()
        return outcome, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@contextmanager
def _stalled_server() -> Iterator[int]:
    """A port that accepts TCP and never answers: a server stuck before its greeting (review I-2)."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.1)
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                accepted.append(listener.accept()[0])
            except TimeoutError:
                continue
            except OSError:
                return

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        server.join()
        for conn in accepted:
            conn.close()
        listener.close()


def _pg():
    return psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy",
                           autocommit=True)


@pytest.fixture
def lite(tmp_path):
    path = tmp_path / "zz_08.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, note TEXT); "
                       "INSERT INTO t (note) VALUES ('a'), ('b'), ('c');")
    conn.close()
    return path


def _lite_profile(path, name="zz_08 lite", number=1) -> SqliteProfile:
    return SqliteProfile(id=f"00000000-0000-0000-0000-{number:012d}", engine="sqlite", name=name, path=str(path))


@requires_pg
def test_postgres_rows_more_rows_and_the_server_refuses_writes(container, no_gate):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    first = _read(container, pid, "SELECT n FROM generate_series(1, 250) AS n", Dialect.POSTGRES)
    assert (first.status, len(first.rows), first.more_rows) == ("ok", 100, True)
    assert first.columns == [{"name": "n", "type": "int4"}] and first.rows[:2] == [[1], [2]]
    whole = _read(container, pid, "SELECT n FROM generate_series(1, 250) AS n", Dialect.POSTGRES, max_rows=1000)
    assert (len(whole.rows), whole.more_rows) == (250, False)
    agent = next(e.worker for e in container.sessions._sessions.values() if e.agent)
    assert agent._tracker.idle_stretch() is None  # the read's transaction ended: no idle-transaction warning for it
    owner = _pg()
    try:
        owner.execute("DROP TABLE IF EXISTS public.zz_08_victim")
        owner.execute("CREATE TABLE public.zz_08_victim (id int)")
        written = _read(container, pid, "INSERT INTO public.zz_08_victim VALUES (1)", Dialect.POSTGRES)
        assert (written.status, written.error_code) == ("error", "25006")
        stacked = _read(container, pid, "SELECT 1; COMMIT; DROP TABLE public.zz_08_victim", Dialect.POSTGRES)
        assert stacked.error_code == "read_only_single_statement"  # G6: the adapter's own refusal
        assert owner.execute("SELECT count(*) FROM public.zz_08_victim").fetchone() == (0,)
    finally:
        owner.execute("DROP TABLE IF EXISTS public.zz_08_victim")
        owner.close()


@requires_pg
def test_postgres_stays_read_only_after_a_function_turns_it_off_and_its_locks_are_released(container, no_gate):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    owner = _pg()
    try:
        owner.execute("DROP TABLE IF EXISTS public.zz_08_victim")
        owner.execute("CREATE TABLE public.zz_08_victim (id int)")
        owner.execute("CREATE OR REPLACE FUNCTION public.zz_08_flip() RETURNS int LANGUAGE plpgsql AS $$ BEGIN "
                      "PERFORM set_config('default_transaction_read_only', 'off', false); RETURN 1; END $$")
        owner.execute("CREATE OR REPLACE FUNCTION public.zz_08_lock() RETURNS int LANGUAGE plpgsql AS $$ BEGIN "
                      "PERFORM pg_advisory_lock(80808); RETURN 1; END $$")
        assert _read(container, pid, "SELECT public.zz_08_flip()", Dialect.POSTGRES).status == "ok"  # P3
        after = _read(container, pid, "INSERT INTO public.zz_08_victim VALUES (1)", Dialect.POSTGRES)
        assert after.error_code == "25006"  # the ROLLBACK put the session default back
        assert _read(container, pid, "SHOW default_transaction_read_only", Dialect.POSTGRES).rows == [["on"]]
        assert _read(container, pid, "SELECT public.zz_08_lock()", Dialect.POSTGRES).status == "ok"
        held = owner.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 80808").fetchone()
        assert held == (0,)  # H1-e: pg_advisory_unlock_all() after the ROLLBACK
    finally:
        owner.execute("DROP FUNCTION IF EXISTS public.zz_08_flip()")
        owner.execute("DROP FUNCTION IF EXISTS public.zz_08_lock()")
        owner.execute("DROP TABLE IF EXISTS public.zz_08_victim")
        owner.close()


@requires_pg
def test_postgres_with_standard_conforming_strings_off_is_refused(container):
    """Pre-flight M1 (ruling J1): with it off, a backslash in '…' escapes, so the gate's lexer could read a denied call
    as part of a string. The session's own setting (here from the login's startup options, as ALTER ROLE … SET would
    give it) refuses every call, and no agent session is left open."""
    pid = share(container, make_pg_profile(name="zz_08 pg scs",
                                           options={"options": "-c standard_conforming_strings=off"}))
    for _ in range(2):  # refused again: the next call opens a new session and reads it again
        with pytest.raises(ToolFailure) as refused:
            _read(container, pid, "SELECT 1", Dialect.POSTGRES)
        assert refused.value.code == "refused"
        assert "standard_conforming_strings off" in refused.value.message
    assert not [e for e in container.sessions._sessions.values() if e.agent]
    plain = share(container, make_pg_profile(name="zz_08 pg"))
    assert _read(container, plain, "SELECT 1", Dialect.POSTGRES).rows == [[1]]  # on: the default


@requires_pg
def test_postgres_statement_timeout_reaches_the_cursor_and_the_deadline_stops_the_rest(container, monkeypatch):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    monkeypatch.setattr("dbaguy_backend.agent.reader.SERVER_TIMEOUT_MARGIN_S", -4)  # the server's own: 1 s (A7)
    started = time.monotonic()
    server = _read(container, pid, "SELECT pg_sleep(3)", Dialect.POSTGRES, timeout_s=5)
    assert server.status == "timeout" and time.monotonic() - started < 2.5
    assert server.notes == ["canceling statement due to statement timeout"]  # the server's words
    monkeypatch.undo()
    started = time.monotonic()
    ours = _read(container, pid, "SELECT pg_sleep(30)", Dialect.POSTGRES, timeout_s=1)
    assert ours.status == "timeout" and time.monotonic() - started < scaled(5)
    assert _read(container, pid, "SELECT 1", Dialect.POSTGRES).rows == [[1]]  # the session goes on


@requires_pg
def test_a_cancel_stops_the_statement(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    control = CallControl()
    threading.Timer(0.5, control.cancel).start()
    started = time.monotonic()
    outcome = container.agent_reader.read(ReadRequest(
        profile=container.profiles.get(UUID(pid)), dialect=Dialect.POSTGRES, sql="SELECT pg_sleep(30)"), control)
    assert outcome.status == "cancelled" and time.monotonic() - started < scaled(5)


@requires_pg
def test_huge_values_are_read_a_page_at_a_time_and_stop_at_the_response_budget(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    sql = "SELECT repeat('x', 1000000) AS big FROM generate_series(1, 1000)"
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        outcome = _read(container, pid, sql, Dialect.POSTGRES, max_rows=1000)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert outcome.status == "ok" and outcome.more_rows
    assert outcome.rows[0][0] == {"$type": "text", "preview": "x" * 1000, "size": 1_000_000, "truncated": True}
    assert len(outcome.rows) < 40 and outcome.rows_read < 60  # CR-08-7: never the 1,000 rows
    assert peak < 40 * 1024 * 1024, f"{peak / 1e6:.1f} MB"  # a page (8 Mi characters) and a peeked row, not 1 GB


@requires_pg
def test_a_second_call_waits_for_the_first_within_its_deadline(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    first = threading.Thread(target=_read, args=(container, pid, "SELECT pg_sleep(2)", Dialect.POSTGRES))
    first.start()
    time.sleep(0.5)
    with pytest.raises(ToolFailure) as busy:
        _read(container, pid, "SELECT 1", Dialect.POSTGRES, timeout_s=0.5)
    assert busy.value.code == "busy"
    first.join()
    assert _read(container, pid, "SELECT 1", Dialect.POSTGRES, timeout_s=10).rows == [[1]]


@requires_mysql
def test_mysql_refuses_writes_cleans_its_sql_mode_and_releases_locks(container, no_gate):
    pid = share(container, make_mysql_profile(name="zz_08 mysql", options={"sql_mode": "ANSI,NO_BACKSLASH_ESCAPES"}))
    mode = _read(container, pid, "SELECT @@SESSION.sql_mode", Dialect.MYSQL).rows[0][0]
    assert not {"ANSI", "ANSI_QUOTES", "NO_BACKSLASH_ESCAPES"} & set(mode.split(","))  # CR-08-5
    owner = mysql.connector.connect(**mysql_driver_kwargs(autocommit=True))
    cursor = owner.cursor()
    try:
        cursor.execute("DROP TABLE IF EXISTS zz_08_victim")
        cursor.execute("CREATE TABLE zz_08_victim (id INT)")
        cursor.execute("DROP FUNCTION IF EXISTS zz_08_lock")
        cursor.execute("CREATE FUNCTION zz_08_lock() RETURNS INT NO SQL RETURN GET_LOCK('zz_08_l', 1)")
        written = _read(container, pid, "INSERT INTO zz_08_victim VALUES (1)", Dialect.MYSQL)
        assert (written.status, written.error_code) == ("error", "1792")
        assert _read(container, pid, "SELECT zz_08_lock()", Dialect.MYSQL).rows == [[1]]
        cursor.execute("SELECT IS_FREE_LOCK('zz_08_l')")
        assert cursor.fetchall() == [(1,)]  # M5: RELEASE_ALL_LOCKS() after the ROLLBACK
        cursor.execute("SELECT COUNT(*) FROM zz_08_victim")
        assert cursor.fetchall() == [(0,)]
    finally:
        cursor.execute("DROP FUNCTION IF EXISTS zz_08_lock")
        cursor.execute("DROP TABLE IF EXISTS zz_08_victim")
        owner.close()


def test_sqlite_refuses_writes_attach_and_vacuum_into_and_leaves_no_file(container, no_gate, lite, tmp_path):
    pid = share(container, _lite_profile(lite))
    assert _read(container, pid, "SELECT note FROM t ORDER BY id", Dialect.SQLITE).rows == [["a"], ["b"], ["c"]]
    attached, copy = tmp_path / "zz_08_attached.db", tmp_path / "zz_08_copy.db"
    for sql in ("INSERT INTO t (note) VALUES ('x')", f"ATTACH '{attached}' AS x", f"VACUUM INTO '{copy}'",
                "PRAGMA query_only = 0", "BEGIN", "SELECT load_extension('x')", "SELECT fts3_tokenizer('simple')"):
        outcome = _read(container, pid, sql, Dialect.SQLITE)
        assert outcome.status == "error", sql
        # the authorizer (D-08-4): "authorization denied" while it runs, "not authorized" while it is prepared
        assert outcome.error_code == "SQLITE_AUTH" or "not authorized" in outcome.error_message, sql
    assert not attached.exists() and not copy.exists()
    plan = _read(container, pid, "SELECT * FROM t WHERE id = 1", Dialect.SQLITE, explain=True)  # + EXPLAIN QUERY PLAN
    assert plan.status == "ok" and plan.rows  # S9: EXPLAIN QUERY PLAN under the authorizer (A9)
    catalog = container.agent_tools.describe(pid, name="t")  # H6: the catalog's pragmas under the authorizer
    assert [c["name"] for c in catalog["columns"]] == ["id", "note"]
    assert sqlite3.connect(lite).execute("SELECT count(*) FROM t").fetchone() == (3,)


def test_at_most_eight_sessions_the_least_recently_used_gives_way(container, tmp_path):
    ids = []
    for number in range(MAX_AGENT_SESSIONS + 1):
        path = tmp_path / f"zz_08_{number}.db"
        sqlite3.connect(path).close()
        ids.append(share(container, _lite_profile(path, f"zz_08 {number}", number + 1)))
        assert _read(container, ids[-1], "SELECT 1", Dialect.SQLITE).rows == [[1]]
    assert len(container.agent_sessions._slots) == MAX_AGENT_SESSIONS
    assert UUID(ids[0]) not in {key[0] for key in container.agent_sessions._slots}


def test_mysql_explains_as_a_tree_from_8_0_16_only():  # D-08-10
    assert explain_head(Dialect.MYSQL, "MySQL 8.4.11") == explain_head(Dialect.MYSQL, "MySQL 8.0.16") == \
        "EXPLAIN FORMAT = TREE"
    assert explain_head(Dialect.MYSQL, "MySQL 8.0.15") == explain_head(Dialect.MYSQL, "MariaDB 11.4.2-MariaDB") == \
        "EXPLAIN"
    assert explain_head(Dialect.TSQL, "Microsoft SQL Server 2022") is None  # SHOWPLAN instead
    assert explain_head(Dialect.SQLITE, "") == "EXPLAIN QUERY PLAN"


def test_a_session_that_cannot_open_takes_no_slot(container, tmp_path):
    pid = share(container, _lite_profile(tmp_path / "zz_08_missing.db"))
    with pytest.raises(DomainError):
        _read(container, pid, "SELECT 1", Dialect.SQLITE)
    assert not container.agent_sessions._slots  # only sessions count toward MAX_AGENT_SESSIONS


def test_closing_a_profiles_sessions_ends_its_call_and_leaves_nothing_open(container, lite):
    """The profile changed (or the MCP server stopped) while a call ran: the call says so, never "timeout", and no
    session of the old profile stays open."""
    pid = share(container, _lite_profile(lite))
    slow = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r"
    outcomes = []
    call = threading.Thread(target=lambda: outcomes.append(_read(container, pid, slow, Dialect.SQLITE, timeout_s=20)))
    call.start()
    time.sleep(0.5)
    container.agent_sessions.close(UUID(pid))
    call.join(scaled(10))
    assert [(o.status, o.error_code) for o in outcomes] == [("error", "connection_closed")]
    assert not [e for e in container.sessions._sessions.values() if e.agent] and not container.agent_sessions._slots
    assert _read(container, pid, "SELECT 1", Dialect.SQLITE).rows == [[1]]


@requires_pg
def test_a_page_is_bounded_by_its_size_whatever_the_rows_before_it(container):
    """Ruling T6-I1: rows are fetched one at a time and a page ends once its raw size reaches PAGE_RAW_CHARS, so a
    small first row never lets 99 rows of 1 MB into one page (the old rule: 101 rows read, 107 MB traced)."""
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    _read(container, pid, "SELECT 1", Dialect.POSTGRES)  # the session is open before tracing
    outcome, peak = _traced(lambda: _read(container, pid, _small_first(Dialect.POSTGRES), Dialect.POSTGRES,
                                          max_rows=1000))
    assert outcome.status == "ok" and outcome.more_rows and outcome.rows[0] == ["x"]
    assert len(outcome.rows) < 40 and outcome.rows_read < 50  # about what the response holds
    assert peak < 8 * 1024 * 1024, f"{peak / 1e6:.1f} MB"  # a few 1 MB rows, never a page of 99
    assert _read(container, pid, "SELECT 1", Dialect.POSTGRES).rows == [[1]]  # the stream's cancel was rolled back


@requires_pg
def test_an_agents_postgres_read_streams_inside_its_frame(container):
    """T6-I1: in its frame's transaction the agent's read is the server's single-row stream, not a cursor (a FETCH
    per row would cost a round trip each)."""
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    found = _read(container, pid, "SELECT count(*) FROM pg_cursors WHERE name LIKE 'dbaguy%'", Dialect.POSTGRES)
    assert found.rows == [["0"]]  # DbaGuy's DECLAREd cursor would count itself (int8 is read as text)


@requires_pg
def test_postgres_standard_conforming_strings_is_checked_on_every_call(container):
    """Review M-2: a session that turns it off after it opened (a server reload) is refused on its next call."""
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    assert _read(container, pid, "SELECT 1", Dialect.POSTGRES).rows == [[1]]
    worker = _agent_workers(container)[0]
    worker.call(lambda c: c.run("SET standard_conforming_strings = off", RunOptions(describe=False)).close())
    with pytest.raises(ToolFailure) as refused:
        _read(container, pid, "SELECT 1", Dialect.POSTGRES)
    assert refused.value.code == "refused" and not _agent_workers(container)
    assert _read(container, pid, "SELECT 1", Dialect.POSTGRES).rows == [[1]]  # a new session: on again


@requires_pg
def test_an_error_in_the_read_still_ends_its_frame(container, monkeypatch):
    """Review M-1: whatever goes wrong while the statement is read, the session is closed: no transaction, lock or
    paused result is left for the next call."""
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    _read(container, pid, "SELECT 1", Dialect.POSTGRES)

    def broken(self, rows):
        raise RuntimeError("injected")

    monkeypatch.setattr(reader_module.ResultBudget, "take", broken)
    with pytest.raises(RuntimeError):
        _read(container, pid, "SELECT n FROM generate_series(1, 10) AS n", Dialect.POSTGRES)
    assert not _agent_workers(container) and not container.agent_sessions._slots
    monkeypatch.undo()
    assert _read(container, pid, "SELECT 1", Dialect.POSTGRES).rows == [[1]]


@requires_pg
def test_the_deadline_covers_opening_the_session(container):
    """Ruling T6-I2: a server that accepts and never answers ends the call at its timeout, as `timeout` (it took
    PostgreSQL's 10 s connect_timeout and came back ConnectionFailed)."""
    with _stalled_server() as port:
        pid = share(container, make_pg_profile(name="zz_08 stalled", port=port))
        started = time.monotonic()
        outcome = _read(container, pid, "SELECT 1", Dialect.POSTGRES, timeout_s=1)
        took = time.monotonic() - started
    assert outcome.status == "timeout" and took < scaled(1.6), f"{took:.2f} s"
    assert not _agent_workers(container)  # nothing registered: the open is still in flight, kept in its slot (R-3)


def _session_threads(name: str) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(f"session-mcp:{name}:")]


@requires_pg
def test_retries_against_a_stalled_server_keep_one_open_in_flight(container):
    """Re-review R-3: an open the call's deadline left behind stays in its slot, and the next call on the key waits
    for it (within its own deadline) instead of starting another: ten retries, one connection attempt."""
    with _stalled_server() as port:
        pid = share(container, make_pg_profile(name="zz_08 stalled", port=port))
        outcomes = [_read(container, pid, "SELECT 1", Dialect.POSTGRES, timeout_s=0.2) for _ in range(10)]
        assert {o.status for o in outcomes} == {"timeout"}
        assert len(_session_threads("zz_08 stalled")) <= 1


@requires_pg
def test_opens_in_flight_count_toward_the_session_cap(container):
    """Re-review R-3: across keys too, an open the call left behind holds its slot until it ends, so a stalled server
    holds at most MAX_AGENT_SESSIONS opens: the next key is busy."""
    with _stalled_server() as port:
        pids = [share(container, make_pg_profile(name=f"zz_08 stalled {n}", port=port))
                for n in range(MAX_AGENT_SESSIONS + 1)]
        for pid in pids[:-1]:
            assert _read(container, pid, "SELECT 1", Dialect.POSTGRES, timeout_s=0.2).status == "timeout"
        with pytest.raises(ToolFailure) as busy:
            _read(container, pids[-1], "SELECT 1", Dialect.POSTGRES, timeout_s=0.2)
        assert busy.value.code == "busy"
        assert len([t for t in threading.enumerate() if t.name.startswith("session-mcp:zz_08 stalled")]) \
            <= MAX_AGENT_SESSIONS


def test_no_open_starts_once_the_deadline_has_passed(container, lite, monkeypatch):
    """Re-review nit: the lock wait took the whole deadline: no session is opened only to be closed."""
    pid = share(container, _lite_profile(lite))
    opened = []
    monkeypatch.setattr(container.sessions, "create_agent", lambda *a, **k: opened.append(a))
    with pytest.raises(TimeoutError):
        with container.agent_sessions.lease(container.profiles.get(UUID(pid)), None, time.monotonic() - 1):
            pass
    assert not opened


def test_the_login_read_never_holds_up_the_answer(container, lite, monkeypatch):
    """Re-review R-2: the login is read on a thread of its own, one per profile at a time, never after a cancel."""
    pid = share(container, _lite_profile(lite))
    reads, release = [], threading.Event()

    def slow(profile):
        reads.append(profile.id)
        release.wait(5)

    monkeypatch.setattr(container.agent_login, "ensure", slow)
    try:
        started = time.monotonic()
        assert _read(container, pid, "SELECT 1", Dialect.SQLITE, timeout_s=1).rows == [[1]]
        assert _read(container, pid, "SELECT 1", Dialect.SQLITE, timeout_s=1).rows == [[1]]  # one read in flight
        took = time.monotonic() - started
        control = CallControl()
        control.cancel()
        container.agent_reader.read(ReadRequest(profile=container.profiles.get(UUID(pid)), dialect=Dialect.SQLITE,
                                                sql="SELECT 1"), control)
        assert took < scaled(1.0), f"{took:.2f} s"
        assert wait_until(lambda: reads, 2) and reads == [UUID(pid)]
    finally:
        release.set()


@requires_pg
def test_a_reload_that_turns_standard_conforming_strings_off_is_seen_once_the_frame_opens(container):
    """Re-review R-1: a reload reaches a session's parameter status only with its next command's answer (BEGIN's):
    it is checked again after the opening, and the call is refused before its statement."""
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    _read(container, pid, "SELECT 1", Dialect.POSTGRES)
    connection = _agent_workers(container)[0].connection
    reported = iter(["on", "off"])
    connection.reported_setting = lambda name: next(reported, "off")
    with pytest.raises(ToolFailure) as refused:
        _read(container, pid, "SELECT 1", Dialect.POSTGRES)
    assert refused.value.code == "refused" and not _agent_workers(container)


def test_a_cancel_before_the_frame_sends_nothing(container, lite):
    pid = share(container, _lite_profile(lite))
    control = CallControl()
    control.cancel()
    outcome = container.agent_reader.read(ReadRequest(profile=container.profiles.get(UUID(pid)),
                                                      dialect=Dialect.SQLITE, sql="SELECT 1"), control)
    assert outcome.status == "cancelled" and not outcome.rows


def test_a_sqlite_page_is_bounded_by_its_size_too(container, lite):
    pid = share(container, _lite_profile(lite))
    _read(container, pid, "SELECT 1", Dialect.SQLITE)
    outcome, peak = _traced(lambda: _read(container, pid, _small_first(Dialect.SQLITE), Dialect.SQLITE,
                                          max_rows=1000))
    assert outcome.status == "ok" and outcome.more_rows and len(outcome.rows) < 40 and outcome.rows_read < 50
    assert peak < 8 * 1024 * 1024, f"{peak / 1e6:.1f} MB"


@pytest.mark.parametrize(("version", "refused"), [
    ("MariaDB 10.11.6-MariaDB", True), ("MariaDB 11.4.2-MariaDB-ubu2404", True), ("MySQL 5.7.7-log", True),
    ("MySQL 5.6.51", True), ("MySQL 5.7.8", False), ("MySQL 5.7.44-log", False), ("MySQL 8.4.11", False),
    ("MySQL 8.0.mysql_aurora.3.05.2", False), ("MySQL 9.1.0-commercial", False),
])
def test_mysql_reads_need_mysql_5_7_8_and_refuse_mariadb(version, refused):  # ruling T6-1
    why = read_frame(Dialect.MYSQL).requires.refusal(version)
    assert (why is not None) == refused
    if refused:
        assert why == f"MCP reads need MySQL 5.7.8 or later (this server is {version})"
    assert read_frame(Dialect.POSTGRES).requires is None


def test_an_agent_session_without_hooks_is_refused(container, lite):
    """Review M-5: without the agent gate as its check it would run anything: it is never opened."""
    bare = SessionManager(container.connections, container.profiles)
    pid = UUID(share(container, _lite_profile(lite)))
    with pytest.raises(DomainError):
        bare.create_agent(pid, None)
    assert not bare._sessions


@requires_mssql
def test_sql_server_rolls_back_what_the_gate_would_have_stopped(container, no_gate):
    pid = share(container, make_mssql_profile(name="zz_08 mssql"))
    owner = mssql_connect()
    try:
        owner.execute_non_query("IF OBJECT_ID('dbo.zz_08_victim') IS NOT NULL DROP TABLE dbo.zz_08_victim; "
                                "CREATE TABLE dbo.zz_08_victim (id int)")
        assert _read(container, pid, "INSERT INTO dbo.zz_08_victim VALUES (1)", Dialect.TSQL).status == "ok"
        stacked = _read(container, pid, "SELECT 1; DROP TABLE dbo.zz_08_victim", Dialect.TSQL)  # T1
        assert stacked.status == "ok"
        owner.execute_query("SELECT COUNT(*) FROM dbo.zz_08_victim")  # T6: both rolled back
        assert [row[0] for row in owner] == [0]
    finally:
        owner.execute_non_query("IF OBJECT_ID('dbo.zz_08_victim') IS NOT NULL DROP TABLE dbo.zz_08_victim")
        owner.close()



@requires_mssql
def test_sql_server_options_the_lexer_assumes_are_set_again_on_every_call(container, no_gate):
    """Review M-3: a session option a statement turned off (QUOTED_IDENTIFIER: `"…"` then reads as a string, not as
    the lexer reads it) does not reach the next call."""
    pid = share(container, make_mssql_profile(name="zz_08 mssql"))
    assert _read(container, pid, "SET QUOTED_IDENTIFIER OFF", Dialect.TSQL).status == "ok"
    probe = "SELECT CAST(SESSIONPROPERTY('QUOTED_IDENTIFIER') AS int), CAST(SESSIONPROPERTY('ANSI_NULLS') AS int)"
    assert _read(container, pid, probe, Dialect.TSQL).rows == [[1, 1]]


@requires_mssql
def test_sql_server_rowcount_a_statement_set_does_not_cut_the_next_call(container, no_gate):  # observation 2
    pid = share(container, make_mssql_profile(name="zz_08 mssql"))
    assert _read(container, pid, "SET ROWCOUNT 1", Dialect.TSQL).status == "ok"
    five = "SELECT TOP 5 ROW_NUMBER() OVER (ORDER BY (SELECT 1)) AS n FROM sys.all_objects"
    assert len(_read(container, pid, five, Dialect.TSQL).rows) == 5


@requires_mysql
def test_mysql_session_variables_a_function_changes_are_set_back_for_the_next_call(container):
    """Observation 1: a stored function's SET SESSION outlives the ROLLBACK and would cut or change what the next call
    reads (sql_select_limit: 1 row, more_rows false). The frame's opening sets them back: to the profile's own
    time_zone, the rest to DEFAULT. The function is made by root, as a database's own function would be."""
    pid = share(container, make_mysql_profile(name="zz_08 mysql", options={"time_zone": "+02:00"}))
    state = ("SELECT @@SESSION.sql_select_limit, @@SESSION.group_concat_max_len, @@SESSION.div_precision_increment, "
             "@@SESSION.time_zone, @@SESSION.lc_time_names, @@SESSION.default_week_format, "
             "@@SESSION.transaction_isolation, @@SESSION.max_sort_length, @@SESSION.block_encryption_mode, "
             "@@SESSION.windowing_use_high_precision, @@SESSION.character_set_results, YEAR(NOW()) > 2001")
    five = "SELECT n FROM (SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5) t"
    root = mysql.connector.connect(**mysql_driver_kwargs(user="root", autocommit=True))
    cursor = root.cursor()
    try:
        cursor.execute("DROP FUNCTION IF EXISTS dbaguy.zz_08_lim")
        cursor.execute(
            "CREATE FUNCTION dbaguy.zz_08_lim() RETURNS INT NO SQL BEGIN SET SESSION sql_select_limit = 1, "
            "group_concat_max_len = 4, div_precision_increment = 0, time_zone = '+05:00', lc_time_names = 'de_DE', "
            "default_week_format = 3, transaction_isolation = 'READ-UNCOMMITTED', max_sort_length = 4, "
            "block_encryption_mode = 'aes-256-cbc', windowing_use_high_precision = 0, "
            "character_set_results = 'latin1', timestamp = 1000000000; RETURN 1; END")
        before = _read(container, pid, state, Dialect.MYSQL).rows
        assert before[0][3] == "+02:00"  # the profile's own time_zone
        assert _read(container, pid, "SELECT dbaguy.zz_08_lim()", Dialect.MYSQL).status == "ok"
        after = _read(container, pid, five, Dialect.MYSQL)
        assert (len(after.rows), after.more_rows) == (5, False)
        assert _read(container, pid, state, Dialect.MYSQL).rows == before
    finally:
        cursor.execute("DROP FUNCTION IF EXISTS dbaguy.zz_08_lim")
        root.close()


@requires_couchbase
def test_couchbase_refuses_writes_with_readonly(container, no_gate):
    pid = share(container, make_couchbase_profile(name="zz_08 cb"))
    users = _read(container, pid, "SELECT RAW META(u).id FROM users AS u ORDER BY META(u).id LIMIT 2",
                  Dialect.SQLPP)
    assert users.status == "ok" and len(users.rows) == 2
    written = _read(container, pid, 'UPSERT INTO orders (KEY, VALUE) VALUES ("zz_08_x", {"a": 1})', Dialect.SQLPP)
    assert (written.status, written.error_code) == ("error", "1000")
    found = couchbase_query('SELECT RAW COUNT(*) FROM dbaguy.app.orders USE KEYS "zz_08_x"')["results"]
    assert found == [0]


# ---- the final review's fix wave: M2, T6 re-review 2 (N-1, N-3, observations 1 and 2) ----

def test_a_cancel_that_came_before_the_lock_opens_nothing(container, lite, monkeypatch):
    """Final review M2: `cancelled` is looked at again once the slot's lock is taken, before anything opens."""
    pid = share(container, _lite_profile(lite))
    opened = []
    monkeypatch.setattr(container.sessions, "create_agent", lambda *a, **k: opened.append(a))
    control = CallControl()
    control.cancel()
    outcome = container.agent_reader.read(ReadRequest(profile=container.profiles.get(UUID(pid)),
                                                      dialect=Dialect.SQLITE, sql="SELECT 1"), control)
    assert outcome.status == "cancelled" and not opened


def test_a_call_queued_when_the_server_stopped_opens_no_session(container, lite):
    """Final review M2: a call that waited for a slot whose session the stop closed takes the key's new slot, and the
    profile is still shared; it opens nothing (the agents' closed epoch, read as the call began)."""
    pid = share(container, _lite_profile(lite))
    profile = container.profiles.get(UUID(pid))
    outcomes: list = []
    with container.agent_sessions.lease(profile, None, time.monotonic() + 5):  # the first call holds the slot
        waiter = threading.Thread(target=lambda: outcomes.append(_read(container, pid, "SELECT 1", Dialect.SQLITE,
                                                                       timeout_s=5)))
        waiter.start()
        time.sleep(0.3)  # it waits for the slot's lock
        container.agent_sessions.close()  # the MCP server stopped
    waiter.join(10)
    assert [o.status for o in outcomes] == ["cancelled"] and not outcomes[0].rows
    assert not _agent_workers(container) and not container.agent_sessions._slots


def test_an_open_that_ends_as_its_wait_times_out_is_taken_not_orphaned(container, lite, monkeypatch):
    """T6 re-review 2 N-1: the open lands between the wait's timeout and `done()`: the session is the call's (or the
    slot's), never a session no slot knows of."""
    from concurrent.futures import Future

    pid = share(container, _lite_profile(lite))
    sessions = container.agent_sessions
    real_open = sessions._open

    class Racy(Future):
        def __init__(self, inner):
            super().__init__()
            self._inner = inner

        def result(self, timeout=None):
            if timeout is not None and not self.done():
                self.set_result(self._inner.result(5))  # the open lands just as this wait times out
                raise TimeoutError
            return super().result(timeout)

    monkeypatch.setattr(sessions, "_open", lambda slot, profile_id, database: Racy(real_open(slot, profile_id,
                                                                                               database)))
    outcome = _read(container, pid, "SELECT 1", Dialect.SQLITE, timeout_s=2)
    held = {slot.session_id for slot in sessions._slots.values()} | {
        slot.opening.result().session_id for slot in sessions._slots.values() if slot.opening is not None}
    registered = {sid for sid, entry in container.sessions._sessions.items() if entry.agent}
    assert outcome.status in ("ok", "timeout") and registered <= held


@requires_pg
def test_waiting_for_an_open_hears_the_clients_cancel(container):
    """T6 re-review 2 observation 2: a cancel while the session opens ends the call within TICK_S, not at the
    deadline (it came back `timeout` at the deadline)."""
    with _stalled_server() as port:
        pid = share(container, make_pg_profile(name="zz_08 stalled", port=port))
        control = CallControl()
        threading.Timer(0.3, control.cancel).start()
        started = time.monotonic()
        outcome = container.agent_reader.read(ReadRequest(profile=container.profiles.get(UUID(pid)),
                                                          dialect=Dialect.POSTGRES, sql="SELECT 1", timeout_s=3),
                                              control)
        took = time.monotonic() - started
    assert outcome.status == "cancelled" and took < scaled(0.9), f"{took:.2f} s"


@requires_pg
def test_an_opening_that_takes_the_rest_of_the_deadline_sends_no_statement(container, monkeypatch):
    """T6 re-review 2 observation 1: the frame's opening answered after the deadline: the statement is not sent (it
    was, then cancelled at once, which on a slow server cost seconds and a connection); the frame's end rolls back."""
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    _read(container, pid, "SELECT 1", Dialect.POSTGRES)  # the session is open
    begin, sent = reader_module._begin, []

    def slow_begin(assumed, statements, connection):
        found = begin(assumed, statements, connection)
        time.sleep(0.08)  # the opening answers within its least time (_LEAST_S), past the call's deadline
        return found

    monkeypatch.setattr(reader_module, "_begin", slow_begin)
    submit = reader_module.AgentReader._submit
    monkeypatch.setattr(reader_module.AgentReader, "_submit", staticmethod(
        lambda lease, command, outcome: (sent.append(type(command).__name__), submit(lease, command, outcome))[1]))
    # the session's lock and opening took all but a few milliseconds of the call
    outcome = _read(container, pid, "SELECT 1", Dialect.POSTGRES, timeout_s=0.5, admit=lambda: time.sleep(0.47))
    assert outcome.status == "timeout" and not outcome.rows and not sent and not outcome.reset
    assert len(_agent_workers(container)) == 1 and not _agent_workers(container)[0].connection.in_transaction()


def test_a_failed_login_query_starts_no_thread_per_call(container, lite, monkeypatch):
    """T6 re-review 2 N-3: after a login query failed, no call starts a login thread until it is due again."""
    from dbaguy_backend.agent import sessions as sessions_module
    from dbaguy_backend.domain.errors import QueryFailed

    pid = share(container, _lite_profile(lite))
    profile = container.profiles.get(UUID(pid))

    def failing():
        raise QueryFailed("permission denied")

    container.agent_login.remember(profile, failing)  # failed: not tried again for a minute
    started = []
    real = sessions_module.threading.Thread

    def counting(*args, **kwargs):
        if str(kwargs.get("name", "")).startswith("mcp-login-"):
            started.append(kwargs["name"])
        return real(*args, **kwargs)

    monkeypatch.setattr(sessions_module.threading, "Thread", counting)
    for _ in range(3):
        container.agent_sessions.read_login(profile)
    assert started == []
