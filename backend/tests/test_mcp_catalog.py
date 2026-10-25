"""The schema tools on the agents' own read-only metadata connection (spec ⑧ D-08-11, §3.5): list_objects,
search_objects, describe; access levels; names that reach no SQL. Seeds are only read; the one scratch table is
zz_08_victim, dropped by exact name."""

import json
import os
import sqlite3
import threading
from uuid import UUID

import psycopg
import pytest

from dbaguy_backend.adapters.base import RunOptions
from dbaguy_backend.agent.tools import ToolFailure
from dbaguy_backend.domain.errors import QueryFailed, SecretStoreError, SessionBusy
from dbaguy_backend.domain.metadata import PathSegment
from dbaguy_backend.domain.profiles import SqliteProfile

from .agent_support import call_tool, container, logged, serving, share
from .support import (
    PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, make_couchbase_profile, make_mssql_profile, make_mysql_profile,
    make_pg_profile, requires_couchbase, requires_mssql, requires_mysql, requires_pg,
)

__all__ = ["container"]


def _segments(path: list[dict]) -> list[PathSegment]:
    return [PathSegment(**segment) for segment in path]


def _one_value(connection, sql: str):
    result = connection.run(sql, RunOptions(describe=False))
    try:
        return result.fetch(1)[0][0]
    finally:
        result.close()


@pytest.fixture
def sqlite_file(tmp_path):
    path = tmp_path / "zz_08.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent (id), note TEXT);
        CREATE INDEX child_parent_idx ON child (parent_id);
    """)
    conn.close()
    return path


def test_an_off_or_unknown_connection_is_not_there_and_schema_access_lists(container, sqlite_file):
    hidden = share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000001", engine="sqlite", name="hidden",
                                            path=str(sqlite_file)), access="off")
    shared = share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000002", engine="sqlite",
                                            name="zz_08 lite", path=str(sqlite_file)), access="schema")
    tools = container.agent_tools
    for name in (hidden, "hidden", "nope"):
        with pytest.raises(ToolFailure) as refused:
            tools.list_objects(name)
        assert refused.value.code == "unknown_connection"
    root = tools.list_objects("ZZ_08 LITE")  # a name, ignoring case
    assert [o["name"] for o in root["objects"]] == ["main"] and not root["truncated"]
    tables = tools.list_objects(shared, _segments(root["objects"][0]["path"]) + [PathSegment(kind="folder",
                                                                                             name="tables")])
    assert [o["name"] for o in tables["objects"]] == ["child", "parent"]
    assert tools.list_objects(shared, None, limit=1)["truncated"] is False


def test_sqlite_describe_has_keys_foreign_keys_and_indexes(container, sqlite_file):
    pid = share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000003", engine="sqlite", name="lite",
                                         path=str(sqlite_file)), access="schema")
    described = container.agent_tools.describe(pid, name="CHILD")
    assert [c["name"] for c in described["columns"]] == ["id", "parent_id", "note"]
    assert described["primary_key"] == ["id"]
    assert described["foreign_keys"] == [{"name": described["foreign_keys"][0]["name"], "columns": ["parent_id"],
                                          "references": "main.parent", "ref_columns": ["id"]}]
    assert [i["name"] for i in described["indexes"]] == ["child_parent_idx"]
    assert "ddl" not in described
    assert "CREATE TABLE child" in container.agent_tools.describe(pid, name="child", include_ddl=True)["ddl"]
    hits = container.agent_tools.search_objects(pid, "parent", include_columns=True)["objects"]
    assert {(h["name"], tuple(h.get("columns", ()))) for h in hits} == {("parent", ()), ("child", ("parent_id",))}


@pytest.fixture
def labels_file(tmp_path):
    path = tmp_path / "zz_08_labels.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE a_note (id INTEGER PRIMARY KEY, label TEXT);
        CREATE TABLE zz_label1 (id INTEGER PRIMARY KEY, label TEXT);
        CREATE TABLE zz_label2 (id INTEGER PRIMARY KEY, label TEXT);
    """)
    conn.close()
    return path


def test_search_says_truncated_when_more_match_and_its_hits_keep_their_columns(container, labels_file):
    pid = share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000004", engine="sqlite", name="labels",
                                         path=str(labels_file)), access="schema")
    tools = container.agent_tools
    one = tools.search_objects(pid, "label", limit=1)
    assert [o["name"] for o in one["objects"]] == ["zz_label1"] and one["truncated"]
    assert tools.search_objects(pid, "label", limit=2)["truncated"] is False
    # a_note matches by a column only and finds no room; the hit returned still gets its matching column
    columns = tools.search_objects(pid, "label", include_columns=True, limit=1)
    assert [(o["name"], o.get("columns")) for o in columns["objects"]] == [("zz_label1", ["label"])]
    assert columns["truncated"]
    every = tools.search_objects(pid, "label", include_columns=True)
    assert {o["name"]: o["columns"] for o in every["objects"]} == {
        "zz_label1": ["label"], "zz_label2": ["label"], "a_note": ["label"]} and not every["truncated"]
    # SQLite's databases are the tree's schemas (main, attached files): `database` keeps one
    assert len(tools.search_objects(pid, "label", database="main", include_columns=True)["objects"]) == 3
    assert tools.search_objects(pid, "label", database="other", include_columns=True)["objects"] == []
    with pytest.raises(ToolFailure) as elsewhere:
        tools.describe(pid, database="other", name="zz_label1")
    assert elsewhere.value.code == "not_found"
    assert tools.describe(pid, database="MAIN", name="ZZ_LABEL1")["name"] == "zz_label1"


def test_the_schema_tools_never_open_the_profiles_own_connection_and_close_with_the_server(container, sqlite_file):
    pid = share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000005", engine="sqlite", name="own",
                                         path=str(sqlite_file)), access="schema")
    metadata = container.metadata
    with serving(container) as port:
        error, child = call_tool(port, "describe", {"connection": pid, "name": "child"})
        assert not error and child["primary_key"] == ["id"]
        error, found = call_tool(port, "search_objects", {"connection": pid, "pattern": "par", "include_columns": True})
        assert not error and {o["name"] for o in found["objects"]} == {"parent", "child"}
        assert [key[2] for key in metadata._workers] == [True]  # one catalog connection, the agents' own
        metadata.with_connection(UUID(pid), None, lambda c: None)  # the app's own (the tree's)
        with pytest.raises(ValueError):
            metadata.with_connection(UUID(pid), None, lambda c: None, retry=False, agent=True)
    assert [key[2] for key in metadata._workers] == [False]  # the server stopped: only the agents' were closed

    def write(connection):
        connection.run("CREATE TABLE zz_08_written (a)", RunOptions(describe=False)).close()

    with pytest.raises(QueryFailed):  # the agents' connection opens the file read-only
        metadata.with_connection(UUID(pid), None, write, agent=True)
    with sqlite3.connect(sqlite_file) as check:
        assert check.execute("SELECT count(*) FROM sqlite_master WHERE name = 'zz_08_written'").fetchone() == (0,)


@requires_pg
def test_postgres_schema_tools_run_on_a_read_only_connection_of_their_own(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"), access="schema")
    tools = container.agent_tools
    databases = {o["name"] for o in tools.list_objects(pid)["objects"]}
    assert {"dbaguy", "analytics"} <= databases
    orders = tools.describe(pid, schema="sales", name="orders")
    assert orders["primary_key"] == ["id"]
    assert orders["foreign_keys"][0]["references"] == "public.customers"
    assert "orders_customer_idx" in {i["name"] for i in orders["indexes"]}
    customers = tools.describe(pid, name="customers")
    assert customers["comment"] == "Customer master data"
    assert next(c for c in customers["columns"] if c["name"] == "email")["comment"] == "Login e-mail"
    hit = tools.search_objects(pid, "sales.ord")["objects"][0]
    assert (hit["schema"], hit["name"]) == ("sales", "orders")
    assert tools.describe(pid, _segments(hit["path"]))["name"] == "orders"
    # the agents' connection is read-only, and is not the profile's own metadata connection
    show = lambda c: _one_value(c, "SHOW transaction_read_only")  # noqa: E731
    assert container.metadata.with_connection(UUID(pid), "dbaguy", show, agent=True) == "on"
    assert container.metadata.with_connection(UUID(pid), "dbaguy", show) == "off"


@requires_pg
def test_names_the_agent_gives_reach_no_sql(container):
    owner = psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy",
                            autocommit=True)
    try:
        owner.execute("DROP TABLE IF EXISTS public.zz_08_victim")
        owner.execute("CREATE TABLE public.zz_08_victim (id int)")
        pid = share(container, make_pg_profile(name="zz_08 pg"), access="schema")
        tools = container.agent_tools
        evil = "x'; DROP TABLE public.zz_08_victim; --"
        for call in (
            lambda: tools.describe(pid, name=evil),
            lambda: tools.describe(pid, schema=evil, name="customers"),
            lambda: tools.describe(pid, [PathSegment(kind="database", name="dbaguy"),
                                         PathSegment(kind="schema", name=evil),
                                         PathSegment(kind="folder", name="tables"),
                                         PathSegment(kind="table", name=evil)]),
            lambda: tools.list_objects(pid, [PathSegment(kind="database", name="dbaguy"),
                                             PathSegment(kind="schema", name=evil)]),
        ):
            with pytest.raises(ToolFailure) as failed:
                call()
            assert failed.value.code in ("not_found", "query_failed", "invalid_argument")
        assert tools.search_objects(pid, evil)["objects"] == []
        assert owner.execute("SELECT count(*) FROM public.zz_08_victim").fetchone() == (0,)
    finally:
        owner.execute("DROP TABLE IF EXISTS public.zz_08_victim")
        owner.close()


@requires_mysql
def test_mysql_describe_by_name(container):
    pid = share(container, make_mysql_profile(name="zz_08 mysql"), access="schema")
    orders = container.agent_tools.describe(pid, schema="dbaguy", name="orders")
    assert orders["primary_key"] == ["id"] and orders["foreign_keys"][0]["name"] == "orders_customer_fk"


@requires_pg
def test_postgres_by_name_uses_the_profiles_database_and_one_catalog_connection(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"), access="schema")
    tools = container.agent_tools
    assert tools.search_objects(pid, "orders")["objects"][0]["path"][0] == {"kind": "database", "name": "dbaguy"}
    assert tools.describe(pid, schema="sales", name="orders")["primary_key"] == ["id"]
    assert tools.describe(pid, database="analytics", name="page_views")["name"] == "page_views"
    assert set(container.metadata._workers) == {(UUID(pid), "dbaguy", True), (UUID(pid), "analytics", True)}


@requires_mysql
def test_mysql_database_narrows_like_a_schema(container):
    pid = share(container, make_mysql_profile(name="zz_08 mysql"), access="schema")
    tools = container.agent_tools
    assert tools.describe(pid, database="analytics", name="page_views")["name"] == "page_views"
    with pytest.raises(ToolFailure) as elsewhere:  # never dbaguy.orders
        tools.describe(pid, database="analytics", name="orders")
    assert elsewhere.value.code == "not_found"
    assert tools.search_objects(pid, "orders", database="analytics")["objects"] == []
    assert {h["schema"] for h in tools.search_objects(pid, "orders", database="dbaguy")["objects"]} == {"dbaguy"}
    read_only = container.metadata.with_connection(
        UUID(pid), None, lambda c: _one_value(c, "SELECT @@SESSION.transaction_read_only"), agent=True)
    assert str(read_only) == "1"  # the agents' catalog connection is a read-only session


@requires_mssql
def test_sql_server_describe_by_name(container):
    pid = share(container, make_mssql_profile(name="zz_08 mssql"), access="schema")
    orders = container.agent_tools.describe(pid, schema="sales", name="orders")
    assert orders["foreign_keys"][0]["name"] == "orders_customer_fk"
    assert "orders_customer_idx" in {i["name"] for i in orders["indexes"]}


@requires_couchbase
def test_couchbase_collections_by_path_and_name_over_mcp(container):
    pid = share(container, make_couchbase_profile(name="zz_08 cb"), access="schema")
    with serving(container) as port:
        error, buckets = call_tool(port, "list_objects", {"connection": pid})
        assert not error and "dbaguy" in {o["name"] for o in buckets["objects"]}
        error, users = call_tool(port, "describe", {"connection": "zz_08 cb", "database": "dbaguy", "schema": "app",
                                                    "name": "users"})
        assert not error and users["kind"] == "collection" and users["name"] == "users"
        error, missing = call_tool(port, "describe", {"connection": pid, "name": "zz_08_no_such"})
        assert error and missing["error"]["code"] == "not_found"


# ---- fix round 1 (task-2-fix1.md) ----

TOOL_LINES = ("MCP list_objects ", "MCP search_objects ", "MCP describe ", "MCP list_connections:")


def _agent_keys(container, pid: str) -> set[tuple]:
    return {key for key in container.metadata._workers if key[0] == UUID(pid) and key[2]}


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


@requires_mssql
def test_database_names_resolve_to_the_servers_own_so_variants_open_nothing(container):
    """I-1: SQL Server logs in to `ANALYTICS`, `Analytics`, … and, past a NUL, to the default database. Only the names
    the server lists reach a connection or a snapshot key."""
    pid = share(container, make_mssql_profile(name="zz_08 ms"), access="schema")
    tools = container.agent_tools
    database = lambda name: [PathSegment(kind="database", name=name)]  # noqa: E731
    assert tools.list_objects(pid, database("analytics"))["objects"]
    assert tools.describe(pid, database="analytics", name="page_views")["name"] == "page_views"
    keys, threads, fds = _agent_keys(container, pid), threading.active_count(), _open_fds()
    assert keys == {(UUID(pid), "dbaguy", True), (UUID(pid), "analytics", True)}
    casings = ["ANALYTICS", "Analytics", "aNALYTICS", "AnAlYtIcS", "analyticS", "ANALYTICs", "anaLYTICS", "analYtics",
               "ANALytics", "aNaLyTiCs", "AnalyticS", "analytICS"]
    for name in casings[:8]:
        listed = tools.list_objects(pid, database(name))
        assert listed["objects"][0]["path"][0] == {"kind": "database", "name": "analytics"}
    for name in casings[8:]:
        assert tools.describe(pid, database=name, name="page_views")["path"][0]["name"] == "analytics"
    calls = [lambda n=n: tools.list_objects(pid, database(f"\x00zz_08_{n}")) for n in range(8)]
    calls += [lambda n=n: tools.search_objects(pid, "page", database=f"\x00zz_08_{n}") for n in range(4)]
    for call in calls:
        with pytest.raises(ToolFailure) as refused:
            call()
        assert refused.value.code == "not_found"
    assert _agent_keys(container, pid) == keys
    assert {k[1] for k in container.schema_cache._entries if k[0] == UUID(pid)} == {"analytics"}
    assert threading.active_count() <= threads and _open_fds() <= fds


@requires_pg
def test_agent_text_never_reaches_the_log_above_debug(container):
    """I-2: a path with a newline (PostgreSQL lists nothing for a schema that is not there, so any text gets through)
    and a client's name stay out of INFO and WARNING: one line per call, tool, connection id, outcome, count, ms."""
    pid = share(container, make_pg_profile(name="zz_08 pg"), access="schema")
    forged = "x\nINFO | MCP run_query on 'prod' by claude-code: ok, 3 rows, 12 ms\x1b[31m"
    client = "zz_08 MARKER client"
    path = [{"kind": "database", "name": "dbaguy"}, {"kind": "schema", "name": forged}, {"kind": "folder",
                                                                                         "name": "tables"}]
    with serving(container) as port, logged() as records:
        error, empty = call_tool(port, "list_objects", {"connection": pid, "path": path}, client=client)
        assert not error and empty["objects"] == []
        assert not call_tool(port, "describe", {"connection": pid, "name": "customers"}, client=client)[0]
        assert not call_tool(port, "search_objects", {"connection": pid, "pattern": forged}, client=client)[0]
        assert call_tool(port, "describe", {"connection": pid, "name": forged}, client=client)[0]  # not_found
        assert not call_tool(port, "list_connections", {}, client=client)[0]
    tool_lines = [message for _, message in records if message.startswith(TOOL_LINES)]
    assert len(tool_lines) == 5, tool_lines
    for _, message in records:
        assert "\n" not in message and "run_query" not in message and "MARKER" not in message and "\x1b" not in message
    assert any(line.startswith(f"MCP list_objects on {pid}: ok, 0 objects, ") and line.endswith(" ms")
               for line in tool_lines)


@requires_mysql
def test_a_server_error_quoting_the_agents_name_stays_out_of_the_warning_log(container):
    """I-2: MySQL answers a table name with a NUL with 1064, quoting the name up to the NUL; the reconnect's WARNING
    carries the code only."""
    pid = share(container, make_mysql_profile(name="zz_08 mysql"), access="schema")
    path = [PathSegment(kind="schema", name="dbaguy"), PathSegment(kind="folder", name="tables"),
            PathSegment(kind="table", name="zz_08 MARKER\x00")]
    with logged() as records:
        with pytest.raises(ToolFailure) as failed:
            container.agent_tools.describe(pid, path)
    assert failed.value.code == "query_failed"
    assert any(level == "WARNING" for level, _ in records)
    assert not any("MARKER" in message for _, message in records)


def test_a_keychain_that_refuses_and_a_connection_closed_twice_are_connection_failed(container, sqlite_file,
                                                                                    monkeypatch):
    """Minor 2: fixed texts, never the store's own."""
    pid = share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000006", engine="sqlite", name="kc",
                                         path=str(sqlite_file)), access="schema")

    def refuse(profile):
        raise SecretStoreError("zz_08 the Keychain's own text")

    monkeypatch.setattr(container.profiles, "resolve_secrets", refuse)
    with pytest.raises(ToolFailure) as keychain:
        container.agent_tools.list_objects(pid)
    assert keychain.value.code == "connection_failed" and "zz_08" not in keychain.value.message
    assert "Keychain" in keychain.value.message
    monkeypatch.undo()

    def closed(*args, **kwargs):
        raise SessionBusy("zz_08 closed")

    monkeypatch.setattr(container.metadata, "with_connection", closed)
    with pytest.raises(ToolFailure) as busy:
        container.agent_tools.describe(pid, name="child")
    assert busy.value.code == "connection_failed" and "zz_08" not in busy.value.message


@requires_mssql
def test_sql_server_without_a_profile_database_uses_one_snapshot_and_one_connection(container):
    """M-3: the login's default database is the profile's own: paths through it name it, and reach the same key."""
    pid = share(container, make_mssql_profile(name="zz_08 ms nodb", database=None), access="schema")
    _one_snapshot_and_one_connection(container, pid, "dbaguy", "sales", "orders")


@requires_pg
def test_postgres_without_a_profile_database_uses_one_snapshot_and_one_connection(container):
    """PostgreSQL's login default here is `postgres`, which has no table of the seed: one scratch table, by exact name."""
    owner = psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="postgres",
                            autocommit=True)
    try:
        owner.execute("DROP TABLE IF EXISTS public.zz_08_nodb")
        owner.execute("CREATE TABLE public.zz_08_nodb (id int PRIMARY KEY)")
        pid = share(container, make_pg_profile(name="zz_08 pg nodb", database=None), access="schema")
        _one_snapshot_and_one_connection(container, pid, "postgres", "public", "zz_08_nodb")
    finally:
        owner.execute("DROP TABLE IF EXISTS public.zz_08_nodb")
        owner.close()


def _one_snapshot_and_one_connection(container, pid: str, database: str, schema: str, name: str) -> None:
    tools = container.agent_tools
    hit = next(h for h in tools.search_objects(pid, f"{schema}.{name}")["objects"] if h["name"] == name)
    assert hit["path"][0] == {"kind": "database", "name": database}
    assert tools.describe(pid, _segments(hit["path"]))["name"] == name
    assert tools.describe(pid, schema=schema, name=name)["name"] == name
    assert tools.describe(pid, database=database, schema=schema, name=name)["name"] == name
    assert tools.list_objects(pid)["objects"]  # the root: the same connection
    assert tools.list_objects(pid, _segments(hit["path"][:1]))["objects"]
    assert _agent_keys(container, pid) == {(UUID(pid), None, True)}
    assert [k for k in container.schema_cache._entries if k[0] == UUID(pid)] == [(UUID(pid), "")]


@pytest.fixture
def wide_file(tmp_path):
    path = tmp_path / "zz_08_wide.db"
    conn = sqlite3.connect(path)
    columns = ", ".join(f"a_rather_long_column_name_{n:04} TEXT DEFAULT 'a default'" for n in range(1200))
    conn.execute(f"CREATE TABLE wide ({columns})")
    for n in range(500):
        conn.execute(f"CREATE TABLE table_with_a_rather_long_name_{n:04} (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE commented (id INTEGER PRIMARY KEY)")
    conn.close()
    return path


def _size(result: dict) -> int:
    return len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))


def test_the_schema_tools_stay_inside_the_response_budget(container, wide_file):
    """M-1: about 40,000 characters, as the query tools: past it, items are left out and `truncated` says so."""
    pid = share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000007", engine="sqlite", name="wide",
                                         path=str(wide_file)), access="schema")
    tools = container.agent_tools
    tables = [PathSegment(kind="schema", name="main"), PathSegment(kind="folder", name="tables")]
    listed = tools.list_objects(pid, tables, limit=1000)
    assert _size(listed) <= 40_000 and listed["truncated"] and listed["notes"]
    assert 100 < len(listed["objects"]) < 502
    assert tools.list_objects(pid, tables, limit=10) == {"objects": listed["objects"][:10], "truncated": True}
    found = tools.search_objects(pid, "a_rather_long", include_columns=True, limit=1000)
    assert _size(found) <= 40_000 and found["truncated"] and found["notes"]
    wide = tools.describe(pid, name="wide", include_ddl=True)
    assert _size(wide) <= 40_000 and wide["truncated"] and wide["notes"]
    assert wide["ddl"].startswith("CREATE TABLE wide") and len(wide["ddl"]) < 40_000
    assert wide["columns"][0]["name"] == "a_rather_long_column_name_0000"
    small = tools.describe(pid, name="commented")
    assert small["truncated"] is False and "notes" not in small


@requires_pg
def test_long_comments_are_cut_like_query_cells(container):
    """M-1: a comment or default is the schema's own free text: past 1,000 characters (run_query's default
    max_cell_chars) it is cut, and `truncated` says so."""
    owner = psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy",
                            autocommit=True)
    try:
        owner.execute("DROP TABLE IF EXISTS public.zz_08_commented")
        owner.execute("CREATE TABLE public.zz_08_commented (id int)")
        owner.execute("COMMENT ON TABLE public.zz_08_commented IS '" + "c" * 200_000 + "'")
        owner.execute("COMMENT ON COLUMN public.zz_08_commented.id IS '" + "d" * 200_000 + "'")
        pid = share(container, make_pg_profile(name="zz_08 pg"), access="schema")
        described = container.agent_tools.describe(pid, name="zz_08_commented")
        assert _size(described) <= 40_000 and described["truncated"]
        assert described["comment"] == "c" * 1_000 and described["columns"][0]["comment"] == "d" * 1_000
    finally:
        owner.execute("DROP TABLE IF EXISTS public.zz_08_commented")
        owner.close()


def test_a_path_segment_is_bounded_like_a_name(container):
    """M-1: a path segment's name is at most 1,000 characters (as `name`), so no error echoes a huge one back."""
    with serving(container) as port:
        from .agent_support import list_tools

        tools = {tool.name: tool for tool in list_tools(port)}
    schema = json.dumps(tools["describe"].input_schema)
    assert '"maxLength": 1000' in schema and '"maxLength": 40' in schema


def test_a_column_path_never_borrows_the_keys_of_a_table_with_its_name(container, tmp_path):
    """M-5: the column `parent` of child is not the table parent: no primary key or foreign key comes from it."""
    path = tmp_path / "zz_08_names.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE grand (id INTEGER PRIMARY KEY);
        CREATE TABLE parent (id INTEGER PRIMARY KEY, grand_id INTEGER REFERENCES grand (id));
        CREATE TABLE child (id INTEGER PRIMARY KEY, parent INTEGER REFERENCES parent (id));
    """)
    conn.close()
    pid = share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000008", engine="sqlite", name="names",
                                         path=str(path)), access="schema")
    tools = container.agent_tools
    column = [PathSegment(kind="schema", name="main"), PathSegment(kind="folder", name="tables"),
              PathSegment(kind="table", name="child"), PathSegment(kind="folder", name="columns"),
              PathSegment(kind="column", name="parent")]
    described = tools.describe(pid, column)
    assert described["kind"] == "column" and described["primary_key"] == [] and described["foreign_keys"] == []
    assert tools.describe(pid, name="parent")["foreign_keys"][0]["references"] == "main.grand"  # the table's own
