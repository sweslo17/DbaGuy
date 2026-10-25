"""run_query, explain and sample_fields over MCP (spec ⑧ §3.5, §3.6, §4, D-08-13, D-08-14; A4, A8, A11, A13): results,
refusals and their History, access levels, timeouts, a client's cancel, the plan per engine, and what the log may say.
Seeds are only read; nothing is created on a server."""

import logging
import sqlite3
import time
from types import SimpleNamespace

import anyio
import psycopg
import pytest
from fastapi.testclient import TestClient
from loguru import logger

from dbaguy_backend.agent.queries import SQL_MAX, QueryTools
from dbaguy_backend.agent.reader import ReadOutcome, ReadRequest
from dbaguy_backend.agent.server import client_name
from dbaguy_backend.agent.tools import ToolFailure
from dbaguy_backend.app import create_app
from dbaguy_backend.domain.errors import StorageError
from dbaguy_backend.domain.profiles import ProfileUpsertRequest, SqliteProfile

from .agent_support import AUTH, MCP_TOKEN, call_tool, container, logged, mcp_session, serving, share
from .support import (
    PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, make_couchbase_profile, make_mssql_profile, make_mysql_profile,
    make_pg_profile, requires_couchbase, requires_mssql, requires_mysql, requires_pg, scaled, wait_until,
)

__all__ = ["container"]


@pytest.fixture
def lite(tmp_path):
    path = tmp_path / "zz_08.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, note TEXT); "
                       "INSERT INTO t (note) VALUES ('it''s 中文'), (NULL), ('" + "x" * 3000 + "');")
    conn.close()
    return path


def _lite(container, path, access="query", **changes) -> str:
    return share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000001", engine="sqlite",
                                          name="zz_08 lite", path=str(path)), access=access, **changes)


def test_run_query_over_both_protocols_and_its_history(container, lite):
    pid = _lite(container, lite)
    with serving(container) as port:
        for mode in ("legacy", "2026-07-28"):
            error, data = call_tool(port, "run_query", {"connection": pid, "sql": "SELECT id, note FROM t ORDER BY id",
                                                         "max_cell_chars": 10}, mode=mode, client=f"pytest {mode}")
            assert not error
            assert data["columns"] == [{"name": "id", "type": None}, {"name": "note", "type": None}]  # no peek
            assert data["rows"][:2] == [[1, "it's 中文"], [2, None]]
            assert data["rows"][2][1] == {"$type": "text", "preview": "x" * 10, "size": 3000, "truncated": True}
            assert (data["row_count"], data["more_rows"], data["truncated"]) == (3, False, None)
        error, more = call_tool(port, "run_query", {"connection": pid, "sql": "SELECT id FROM t", "max_rows": 2})
        assert more["more_rows"] and more["notes"] == [
            "More rows exist: raise max_rows (at most 1000) or narrow the query"]
    page = container.history.search(source="mcp")
    assert [(e.client, e.status, e.rows) for e in page.items] == [
        ("pytest", "ok", 2), ("pytest 2026-07-28", "ok", 3), ("pytest legacy", "ok", 3)]
    assert container.history.search(source="app").items == []


def test_refused_statements_are_recorded_merged_and_never_connect(container, lite, monkeypatch):
    pid = _lite(container, lite)
    opened = []
    monkeypatch.setattr(container.agent_sessions, "lease", lambda *a, **k: opened.append(a))
    with serving(container) as port:
        for _ in range(3):
            error, data = call_tool(port, "run_query", {"connection": pid, "sql": "DELETE FROM t"})
            assert error and data["error"]["code"] == "refused"
            assert data["error"]["message"] == ("DbaGuy runs exactly one read statement per call: starts with DELETE: "
                                                "only SELECT, WITH, VALUES are run")
        error, data = call_tool(port, "explain", {"connection": pid, "sql": "EXPLAIN SELECT 1"})
        assert data["error"]["code"] == "refused"
    assert opened == []
    [explained, deleted] = container.history.search(source="mcp").items
    assert (deleted.sql, deleted.status, deleted.repeats, deleted.client) == ("DELETE FROM t", "refused", 3, "pytest")
    assert deleted.error.startswith("DbaGuy runs exactly one read statement per call")
    # an explain's record is what it would have sent, refused or not: never merged with run_query's same text
    assert (explained.sql, explained.status) == ("EXPLAIN EXPLAIN SELECT 1", "refused")


def test_text_a_driver_cannot_send_is_refused_and_still_recorded(container, lite):
    pid = _lite(container, lite)  # JSON can carry "\ud800"; the history file cannot store it
    with pytest.raises(ToolFailure) as refused:
        container.agent_queries.run_query(pid, "SELECT '\ud800'", client="pytest")
    assert refused.value.code == "refused" and "half of a surrogate pair" in refused.value.message
    [record] = container.history.search(source="mcp").items
    assert (record.sql, record.status) == ("SELECT '�'", "refused")
    named = SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace(
        client_info=SimpleNamespace(name="claude\x07 \ud800" + "x" * 100))))
    assert client_name(named) == "claude �" + "x" * 72  # D-08-7: no control characters, 80 at most


def test_a_connection_switched_off_is_gone_at_once_with_its_sessions(container, lite):
    pid = _lite(container, lite)
    with TestClient(create_app(container.settings, container), base_url="http://127.0.0.1") as client, \
            serving(container) as port:
        assert call_tool(port, "run_query", {"connection": pid, "sql": "SELECT 1"})[0] is False
        assert any(e.agent for e in container.sessions._sessions.values())
        body = {"profile": {"engine": "sqlite", "name": "zz_08 lite", "path": str(lite), "mcp_access": "off"}}
        assert client.put(f"/profiles/{pid}", json=body, headers=AUTH).status_code == 200
        assert not any(e.agent for e in container.sessions._sessions.values())
        error, data = call_tool(port, "run_query", {"connection": pid, "sql": "SELECT 1"})
        assert data["error"]["code"] == "unknown_connection"
        assert call_tool(port, "list_connections")[1]["connections"] == []


def test_schema_access_cannot_query_and_off_is_unknown(container, lite):
    pid = _lite(container, lite, access="schema")
    with serving(container) as port:
        error, data = call_tool(port, "run_query", {"connection": pid, "sql": "SELECT 1"})
        assert data["error"] == {"code": "not_allowed", "message": (
            "'zz_08 lite' allows schema only: run_query needs 'Read-only queries' in DbaGuy's connection settings")}
        error, data = call_tool(port, "sample_fields", {"connection": pid, "bucket": "b", "scope": "s",
                                                        "collection": "c"})
        assert data["error"]["code"] == "not_allowed"
        error, data = call_tool(port, "run_query", {"connection": "nope", "sql": "SELECT 1"})
        assert data["error"]["code"] == "unknown_connection"
        error, data = call_tool(port, "run_query", {"connection": pid, "sql": "x" * 20_001})
        assert error  # the SDK's argument check: at most 20,000 characters (CR-08-3)


def test_a_locked_connection_says_so_and_asks_for_nothing(container):
    draft = make_pg_profile(name="zz_08 locked", save_password=False).model_dump(exclude={"id"})
    request = ProfileUpsertRequest.model_validate({"profile": draft | {"mcp_access": "query"}})
    pid = str(container.profiles.create(request).profile.id)  # no password saved, none unlocked
    with serving(container) as port:
        error, data = call_tool(port, "run_query", {"connection": pid, "sql": "SELECT 1"})
        listed = call_tool(port, "list_connections")[1]["connections"]
    assert data["error"]["code"] == "password_required"
    assert data["error"]["message"] == ("'zz_08 locked' needs its password: connect to it in DbaGuy first (or unlock "
                                        "it). DbaGuy never asks for a password over MCP")
    assert listed[0]["needs_unlock"] is True
    assert container.history.search(source="mcp").items[0].status == "error"


def test_names_and_contexts_the_tools_cannot_take_are_invalid_arguments(container, lite):
    """Checked before anything connects: a query_context where the engine has none, one that is not `bucket` or
    `bucket.scope`, a keyspace name longer than Couchbase allows. The History text never passes SQL_MAX."""
    pid = _lite(container, lite)
    cb = share(container, make_couchbase_profile(name="zz_08 cb"))  # never connected to here
    queries = container.agent_queries
    with pytest.raises(ToolFailure) as wrong_engine:
        queries.run_query(pid, "SELECT 1", query_context="dbaguy.app")
    assert wrong_engine.value.code == "invalid_argument" and "Couchbase" in wrong_engine.value.message
    with pytest.raises(ToolFailure) as malformed:
        queries.explain(cb, "SELECT 1", query_context="`a`.`b`.`c`")  # three parts
    assert malformed.value.code == "invalid_argument"
    with pytest.raises(ToolFailure) as long_name:
        queries.sample_fields(cb, "b" * 200, "app", "users")
    assert long_name.value.code == "invalid_argument"
    long_sql = "SELECT 1 /*" + "x" * (SQL_MAX - 13) + "*/"
    assert len(long_sql) == SQL_MAX
    queries.explain(pid, long_sql, client="pytest")
    [record] = container.history.search(source="mcp").items
    assert record.sql.startswith("EXPLAIN SELECT 1") and len(record.sql) == SQL_MAX


class _Reader:
    """A reader that answers with one outcome, for the mapping of outcomes to §4 errors."""

    def __init__(self, outcome: ReadOutcome) -> None:
        self.outcome = outcome
        self.requests: list[ReadRequest] = []

    def read(self, request, control=None):
        self.requests.append(request)
        return self.outcome


@pytest.mark.parametrize(("code", "message", "expected"), [
    ("read_only", "Blocked: read-only connection", ("refused", "Blocked: read-only connection")),
    ("session_changed", "The connection's sql_mode changed: the call was not run and the connection was reset",
     ("session_changed", "The connection's sql_mode changed: the call was not run and the connection was reset")),
    ("SQLITE_READONLY", "attempt to write a readonly database",
     ("query_failed", "attempt to write a readonly database (DbaGuy's MCP access is read-only) (SQLITE_READONLY)")),
    ("SQLITE_ERROR", "no such table: x", ("query_failed", "no such table: x (SQLITE_ERROR)")),
    ("connection_closed", "DbaGuy closed the connection during the call: try again",
     ("connection_failed", "DbaGuy closed the connection during the call: try again")),
    ("connection_lost", "The connection to the server was lost",
     ("connection_failed", "The connection to the server was lost")),
])
def test_what_a_read_ends_with_is_the_tools_error(container, lite, code, message, expected):
    """§4: the worker's backstop is `refused`; a server's read-only refusal says MCP is read-only; a server error ends
    with its code; a connection closed or lost under the call is connection_failed (Task 6 carry)."""
    pid = _lite(container, lite)
    outcome = ReadOutcome()
    outcome.failed(code, message)
    queries = QueryTools(container.agent_tools, _Reader(outcome), container.history, container.field_samples,
                         container.app_settings)
    with pytest.raises(ToolFailure) as failed:
        queries.run_query(pid, "SELECT 1", client="pytest")
    assert (failed.value.code, failed.value.message) == expected
    [record] = container.history.search(source="mcp").items
    assert record.status == ("refused" if expected[0] == "refused" else "error")


def test_history_that_cannot_be_written_never_fails_the_call(container, lite, monkeypatch):
    """As the user's own tabs (the worker's `_notify`): a History write that fails is logged, and the agent gets its
    rows."""
    pid = _lite(container, lite)

    def broken(_record):
        raise StorageError("Cannot open the history: disk I/O error")

    monkeypatch.setattr(container.history, "record", broken)
    with logged("WARNING") as records:
        result = container.agent_queries.run_query(pid, "SELECT id FROM t ORDER BY id", client="pytest")
    assert result["rows"] == [[1], [2], [3]]
    assert any(level == "WARNING" and "History" in text and "StorageError" in text for level, text in records)


@requires_pg
def test_timeouts_and_production_connections(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    red = share(container, make_pg_profile(name="zz_08 red", color="red"))
    with serving(container) as port:
        started = time.monotonic()
        error, data = call_tool(port, "run_query", {"connection": pid, "sql": "SELECT pg_sleep(10)", "timeout_s": 1})
        assert data["error"] == {"code": "timeout", "message": (
            "Stopped after 1 s (timeout_s; at most 60, 30 on production connections)")}
        assert time.monotonic() - started < scaled(5)
        error, data = call_tool(port, "run_query", {"connection": red, "sql": "SELECT 1", "timeout_s": 60})
        assert not error and data["notes"] == ["timeout_s was lowered to 30 s: a production (red) connection"]
    assert [e.status for e in container.history.search(source="mcp").items] == ["ok", "timeout"]


@requires_pg
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
def test_a_client_that_cancels_stops_the_statement(container, mode):
    """A4: a client gives up on a call and keeps its session, as Claude Code does on Esc. 2025-11-25 sends
    notifications/cancelled; 2026-07-28 closes the request's stream, which the SDK notices only with
    json_response=False (P18). Either way the statement stops on the server."""
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    watcher = psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy",
                              autocommit=True)

    def running() -> bool:
        # the frame's read-only transaction reads through a cursor: the backend shows its FETCH, waiting in PgSleep
        sleeping = watcher.execute("SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
                                   "AND datname = 'dbaguy' AND state = 'active' AND wait_event = 'PgSleep'")
        return sleeping.fetchone()[0] > 0

    seen: list[bool] = []

    async def give_up(port: int) -> None:
        async with mcp_session(port, mode=mode) as session:
            async def call() -> None:
                with anyio.move_on_after(1.5):
                    await session.call_tool("run_query", {"connection": pid, "sql": "SELECT pg_sleep(29)"})

            async with anyio.create_task_group() as group:
                group.start_soon(call)
                seen.append(await anyio.to_thread.run_sync(lambda: wait_until(running, 1.4, interval=0.05)))
            seen.append(await anyio.to_thread.run_sync(lambda: wait_until(lambda: not running(), scaled(10))))

    try:
        with serving(container) as port:
            anyio.run(give_up, port)
        assert seen == [True, True]  # it was running when the client gave up, and it stopped while the session stayed
    finally:
        watcher.close()
    assert wait_until(lambda: [e.status for e in container.history.search(source="mcp").items] == ["cancelled"],
                      scaled(5))


class _Errors(logging.Handler):
    """stdlib log records at ERROR and above (uvicorn and the SDK log through `logging`)."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_stopping_with_an_idle_client_connected_is_quick_and_quiet(container):
    """Task 1 review m2: a 2025-11-25 client holds its GET stream open while idle (as Claude Code does). Stopping ends
    the SDK's sessions first, so uvicorn's graceful shutdown has nothing to wait for: no 2 s wait, no ERROR."""
    errors = _Errors()
    logging.getLogger().addHandler(errors)
    took: list[float] = []

    async def stop_while_connected(port: int) -> None:
        try:
            async with mcp_session(port, mode="legacy") as session:
                await session.list_tools()
                await anyio.sleep(0.3)  # its GET stream is open
                started = time.monotonic()
                await anyio.to_thread.run_sync(container.agent_host.stop)
                took.append(time.monotonic() - started)
        except Exception:  # the client finds its server gone as it leaves: not what this test is about
            pass

    with logged("ERROR") as loguru_errors:
        try:
            with serving(container) as port:
                anyio.run(stop_while_connected, port)
        finally:
            logging.getLogger().removeHandler(errors)
    assert took and took[0] < 1.0, took
    assert errors.messages == [] and loguru_errors == []


@requires_pg
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
def test_stopping_during_a_call_ends_it_quickly_and_quietly(container, mode):
    """m2, a call still running: 2026-07-28 answers on the request's own stream (uvicorn waited GRACEFUL_S for it,
    then cut it with an ERROR); a 2025-11-25 session's call answers into a stream the stop cut (the SDK waited a second
    to write its last answer). Either way the stop is quick, logs no ERROR and the call's statement is cancelled."""
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    errors = _Errors()
    logging.getLogger().addHandler(errors)
    took: list[float] = []

    async def stop_during_a_call(port: int) -> None:
        try:
            async with mcp_session(port, mode=mode) as session:
                async def call() -> None:
                    try:
                        await session.call_tool("run_query", {"connection": pid, "sql": "SELECT pg_sleep(20)"})
                    except Exception:  # the server stopped under it
                        pass

                async with anyio.create_task_group() as group:
                    group.start_soon(call)
                    await anyio.sleep(1.0)
                    started = time.monotonic()
                    await anyio.to_thread.run_sync(container.agent_host.stop)
                    took.append(time.monotonic() - started)
                    group.cancel_scope.cancel()
        except Exception:
            pass

    with logged("ERROR") as loguru_errors:
        try:
            with serving(container) as port:
                anyio.run(stop_during_a_call, port)
        finally:
            logging.getLogger().removeHandler(errors)
    assert took and took[0] < 1.0, took
    assert errors.messages == [] and loguru_errors == []
    assert wait_until(lambda: [e.status for e in container.history.search(source="mcp").items] == ["cancelled"],
                      scaled(5))


@requires_pg
def test_postgres_explain_never_runs_the_statement(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    with serving(container) as port:
        error, data = call_tool(port, "explain", {"connection": pid, "sql": "SELECT * FROM public.customers"})
        assert not error and "Seq Scan on customers" in data["plan"]
        error, data = call_tool(port, "explain", {"connection": pid, "sql": "SELECT pg_sleep(30)"})
        assert not error and data["elapsed_ms"] < 5000
    assert container.history.search(source="mcp").items[0].sql == "EXPLAIN SELECT pg_sleep(30)"


@requires_pg
def test_a_connection_refused_for_its_settings_is_recorded_as_refused(container):
    """Pre-flight M1 (Task 6): a PostgreSQL login with standard_conforming_strings off is refused when its session
    opens. The call never reached its statement; History says `refused`, merged like the gate's refusals."""
    pid = share(container, make_pg_profile(name="zz_08 scs",
                                           options={"options": "-c standard_conforming_strings=off"}))
    for _ in range(2):
        with pytest.raises(ToolFailure) as refused:
            container.agent_queries.run_query(pid, "SELECT 1", client="pytest")
        assert refused.value.code == "refused" and "standard_conforming_strings" in refused.value.message
    [record] = container.history.search(source="mcp").items
    assert (record.status, record.repeats, record.sql) == ("refused", 2, "SELECT 1")


@requires_mysql
def test_mysql_explain_is_a_tree(container):
    pid = share(container, make_mysql_profile(name="zz_08 mysql"))
    with serving(container) as port:
        error, data = call_tool(port, "explain", {"connection": pid, "sql": "SELECT * FROM customers WHERE id = 1"})
    assert not error and data["plan"].startswith("->")


def test_sqlite_explain_is_an_indented_tree(container, lite):
    pid = _lite(container, lite)
    with serving(container) as port:
        error, data = call_tool(port, "explain", {"connection": pid, "sql": "SELECT * FROM t WHERE id = 1"})
    assert not error and data["plan"].startswith("SEARCH t USING INTEGER PRIMARY KEY")


@requires_mssql
def test_sql_server_explain_is_showplan_and_showplan_ends_with_it(container):
    pid = share(container, make_mssql_profile(name="zz_08 mssql"))
    with serving(container) as port:
        error, data = call_tool(port, "explain", {"connection": pid, "sql": "SELECT * FROM dbo.customers"})
        assert not error and "|--" in data["plan"]  # A8
        error, data = call_tool(port, "run_query", {"connection": pid, "sql": "SELECT 1 AS one, N'x' AS two"})
        assert not error and data["rows"] == [[1, "x"]]  # SHOWPLAN is off again: the statement ran
        assert all(column["type"] for column in data["columns"]), data["columns"]  # A13
    assert container.history.search(source="mcp").items[1].sql.startswith("-- SHOWPLAN_TEXT")


@requires_couchbase
def test_couchbase_query_explain_and_sample_fields(container):
    pid = share(container, make_couchbase_profile(name="zz_08 cb"))
    with logged("INFO") as lines, serving(container) as port:
        error, data = call_tool(port, "run_query", {"connection": pid, "query_context": "dbaguy.app",
                                                    "sql": "SELECT RAW META(u).id FROM users AS u ORDER BY META(u).id"
                                                           " LIMIT 3"})
        assert not error and len(data["rows"]) == 3
        error, data = call_tool(port, "explain", {"connection": pid, "sql": "SELECT * FROM users"})  # A11
        assert not error and '"#operator"' in data["plan"] and isinstance(data["findings"], list)
        error, data = call_tool(port, "sample_fields", {"connection": pid, "bucket": "dbaguy", "scope": "app",
                                                        "collection": "users", "size": 20})
        assert not error and data["keyspace"] == "dbaguy.app.users" and data["fields"]
    sample = container.history.search(source="mcp").items[0]
    assert sample.sql.startswith("-- MCP sample_fields dbaguy.app.users (") and sample.sql.endswith(" documents)")
    assert container.history.search(source="mcp").items[2].database == "dbaguy.app"
    assert not [text for _, text in lines if "app.users" in text]  # the agent's keyspace stays out of INFO


@requires_pg
def test_the_log_carries_no_sql_no_server_text_and_no_token(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    try:
        with serving(container) as port:
            error, data = call_tool(port, "run_query", {"connection": pid,
                                                        "sql": "SELECT 'zz_08_secret' FROM zz_08_missing"})
            assert data["error"]["code"] == "query_failed" and "zz_08_missing" in data["error"]["message"]
            error, data = call_tool(port, "run_query", {"connection": pid, "sql": "DROP TABLE zz_08_refused"},
                                    client="zz_08_client")
            assert data["error"]["code"] == "refused"
    finally:
        logger.remove(sink)
    text = "\n".join(lines)
    for secret in ("zz_08_secret", "zz_08_missing", "zz_08_refused", "does not exist", "zz_08_client", MCP_TOKEN):
        assert secret not in text, secret
    # ruling I-2 (Task 2 fix round): the connection's id, never the agent's or the client's text above DEBUG
    assert f"MCP run_query on {pid}: 42P01, 0 rows, " in text
    assert f"MCP refused run_query on {pid}: not_allowed_start" in text
