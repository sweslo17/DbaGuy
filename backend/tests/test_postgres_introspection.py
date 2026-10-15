import pytest

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.domain.metadata import PathSegment
from tests.support import PG_HOST, PG_PASSWORD, PG_PORT, make_pg_profile, requires_pg

pytestmark = requires_pg


def seg(kind, name):
    return PathSegment(kind=kind, name=name)


DB = [seg("database", "dbaguy")]


def schema_path(schema, folder=None, *rest):
    path = [*DB, seg("schema", schema)]
    if folder:
        path.append(seg("folder", folder))
    return [*path, *rest]


@pytest.fixture(scope="module")
def conn():
    connection = PostgresAdapter().open(make_pg_profile(), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)
    yield connection
    connection.close()


def names(nodes):
    return [n.name for n in nodes]


def test_databases_and_schemas(conn):
    assert {"dbaguy", "analytics"} <= set(names(conn.list_children([])))
    schemas = conn.list_children(DB)
    assert names(schemas)[:2] == ["public", "sales"]
    system = {n.name for n in schemas if n.system}
    assert "pg_catalog" in system and "public" not in system


def test_folders_hide_empty(conn):
    assert names(conn.list_children(schema_path("public"))) == ["tables", "functions", "sequences", "types"]
    assert names(conn.list_children(schema_path("sales"))) == [
        "tables", "views", "materialized_views", "functions", "procedures", "sequences",
    ]


def test_tables_partitions_and_quoting(conn):
    tables = conn.list_children(schema_path("sales", "tables"))
    assert names(tables) == ["events", "orders"]
    assert tables[0].detail == "partitioned"
    assert "partitions" in names(conn.list_children(tables[0].path))
    [part] = conn.list_children([*tables[0].path, seg("folder", "partitions")])
    assert part.name == "events_2026" and "FOR VALUES" in part.detail
    assert "Order Items" in names(conn.list_children(schema_path("public", "tables")))
    detail = conn.object_detail(schema_path("public", "tables", seg("table", "Order Items")))
    assert 'CREATE TABLE public."Order Items" (' in detail.ddl
    assert '"Select" text' in detail.ddl and '"order" integer' in detail.ddl


def test_columns(conn):
    table = schema_path("public", "tables", seg("table", "customers"))
    cols = conn.list_children([*table, seg("folder", "columns")])
    assert cols[0].name == "id" and "PK" in cols[0].detail
    assert next(c for c in cols if c.name == "email").detail.startswith("email NOT NULL")
    detail = conn.object_detail(table)
    assert detail.comment == "Customer master data"
    assert next(c for c in detail.columns if c.name == "email").comment == "Login e-mail"


def test_table_ddl(conn):
    detail = conn.object_detail(schema_path("sales", "tables", seg("table", "orders")))
    assert detail.ddl_is_generated
    ddl = detail.ddl
    assert "CREATE TABLE sales.orders (" in ddl
    assert "id bigint GENERATED ALWAYS AS IDENTITY" in ddl
    assert "FOREIGN KEY (customer_id) REFERENCES" in ddl
    assert "CREATE INDEX orders_customer_idx ON sales.orders" in ddl
    events = conn.object_detail(schema_path("sales", "tables", seg("table", "events"))).ddl
    assert "PARTITION BY RANGE (occurred_at)" in events


def test_function_overloads(conn):
    fns = conn.list_children(schema_path("public", "functions"))
    assert names(fns) == ["add_numbers(integer, integer)", "add_numbers(numeric, numeric)"]
    ddl = conn.object_detail(fns[1].path).ddl
    assert "numeric" in ddl and ddl.rstrip().endswith(";")
    [proc] = conn.list_children(schema_path("sales", "procedures"))
    assert "CREATE OR REPLACE PROCEDURE sales.archive_orders" in conn.object_detail(proc.path).ddl


def test_other_object_ddl(conn):
    def ddl(*path):
        return conn.object_detail(list(path)).ddl

    assert "AS ENUM ('pending', 'paid', 'shipped', 'cancelled')" in ddl(*schema_path("public", "types", seg("type", "order_status")))
    domain = ddl(*schema_path("public", "types", seg("type", "email")))
    assert domain.startswith("CREATE DOMAIN public.email AS text") and "CHECK" in domain
    composite = ddl(*schema_path("public", "types", seg("type", "address")))
    assert composite.startswith("CREATE TYPE public.address AS (") and "street text" in composite
    seq = ddl(*schema_path("public", "sequences", seg("sequence", "invoice_seq")))
    assert "INCREMENT BY 10" in seq and "START WITH 1000" in seq
    assert "CREATE OR REPLACE VIEW sales.order_summary AS" in ddl(*schema_path("sales", "views", seg("view", "order_summary")))
    assert "CREATE MATERIALIZED VIEW sales.daily_totals AS" in ddl(
        *schema_path("sales", "materialized_views", seg("materialized_view", "daily_totals"))
    )
    trigger = ddl(*schema_path("sales", "tables", seg("table", "orders"), seg("folder", "triggers"), seg("trigger", "orders_noop")))
    assert "EXECUTE FUNCTION sales.orders_noop()" in trigger
    index = ddl(*schema_path("sales", "tables", seg("table", "orders"), seg("folder", "indexes"), seg("index", "orders_customer_idx")))
    assert index.startswith("CREATE INDEX orders_customer_idx")
