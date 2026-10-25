"""The schema tools on the agents' own read-only metadata connection (spec ⑧ D-08-11, §3.5): list_objects,
search_objects, describe; access levels; names that reach no SQL. Seeds are only read; the one scratch table is
zz_08_victim, dropped by exact name."""

import sqlite3
from uuid import UUID

import psycopg
import pytest

from dbaguy_backend.adapters.base import RunOptions
from dbaguy_backend.agent.tools import ToolFailure
from dbaguy_backend.domain.errors import QueryFailed
from dbaguy_backend.domain.metadata import PathSegment
from dbaguy_backend.domain.profiles import SqliteProfile

from .agent_support import call_tool, container, serving, share
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
