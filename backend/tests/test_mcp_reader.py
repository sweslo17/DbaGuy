"""An MCP agent's read on the five engines (spec ⑧ D-08-1, D-08-3, D-08-4, §3.4, §3.6): the frame, paging and its
memory bound, and what each server refuses with the gate taken away (`no_gate`: the worker's backstop allows anything,
as a gate bug would). Scratch objects are zz_08_*, dropped by exact name; Couchbase is only read."""

import sqlite3
import threading
import time
import tracemalloc
from uuid import UUID

import mysql.connector
import psycopg
import pytest

from dbaguy_backend.agent.reader import CallControl, ReadRequest
from dbaguy_backend.agent.sessions import MAX_AGENT_SESSIONS
from dbaguy_backend.agent.tools import ToolFailure
from dbaguy_backend.domain.classification import Classification, StatementKind
from dbaguy_backend.domain.errors import DomainError
from dbaguy_backend.domain.profiles import SqliteProfile
from dbaguy_backend.services.agent_gate import AgentVerdict
from dbaguy_backend.sql.dialects import Dialect, explain_head

from .agent_support import container, share
from .support import (
    PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, couchbase_query, make_couchbase_profile, make_mssql_profile,
    make_mysql_profile, make_pg_profile, mssql_connect, mysql_driver_kwargs, requires_couchbase, requires_mssql,
    requires_mysql, requires_pg, scaled,
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
