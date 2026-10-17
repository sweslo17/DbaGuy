import mysql.connector
import pytest
from mysql.connector.cursor import MySQLCursorBuffered

from dbaguy_backend.adapters.mysql import catalog, introspection, schema as mysql_schema
from dbaguy_backend.domain.errors import NotFound, ValidationFailed
from dbaguy_backend.domain.metadata import PathSegment
from tests.support import mysql_driver_kwargs, requires_mysql

pytestmark = requires_mysql


@pytest.fixture
def conn():
    connection = mysql.connector.connect(**mysql_driver_kwargs())
    yield connection
    connection.close()


@pytest.fixture
def sql_log(monkeypatch):
    """Every statement any buffered cursor executes, in order."""
    seen: list[str] = []
    original = MySQLCursorBuffered.execute

    def spy(self, operation, *args, **kwargs):
        seen.append(operation if isinstance(operation, str) else operation.decode())
        return original(self, operation, *args, **kwargs)

    monkeypatch.setattr(MySQLCursorBuffered, "execute", spy)
    return seen


def seg(kind, name):
    return PathSegment(kind=kind, name=name)


def names(nodes):
    return [n.name for n in nodes]


DB = [seg("schema", "dbaguy")]
ORDERS = [*DB, seg("folder", "tables"), seg("table", "orders")]


def test_tree(conn):
    root = introspection.list_children(conn, [])
    assert {"dbaguy", "analytics"} <= set(names(root))
    system = {n.name for n in root if n.system}
    # R-03a-P2: SCHEMATA lists only what the account may access; dbaguy (no global grants) sees these two system schemas
    assert {"information_schema", "performance_schema"} <= system
    assert all(not n.system for n in root[:2]) and all(n.kind == "schema" for n in root)  # user schemas first
    assert names(introspection.list_children(conn, DB)) == ["tables", "views", "functions", "procedures", "triggers", "events"]
    assert names(introspection.list_children(conn, [seg("schema", "analytics")])) == ["tables"]
    tables = set(names(introspection.list_children(conn, [*DB, seg("folder", "tables")])))
    assert {"big_table", "customers", "orders", "Order Items", "tick`table", "types_demo"} <= tables
    assert names(introspection.list_children(conn, [*DB, seg("folder", "views")])) == ["order_summary"]
    assert set(names(introspection.list_children(conn, [*DB, seg("folder", "procedures")]))) == {"two_results", "fail_after_select"}
    [trigger] = introspection.list_children(conn, [*DB, seg("folder", "triggers")])
    assert (trigger.kind, trigger.name, trigger.detail) == ("trigger", "orders_note", "on orders")
    [event] = introspection.list_children(conn, [*DB, seg("folder", "events")])
    assert (event.kind, event.name, event.detail) == ("event", "purge_job_log", "enabled")
    assert names(introspection.list_children(conn, ORDERS)) == ["columns", "indexes", "foreign_keys", "triggers"]
    indexes = introspection.list_children(conn, [*ORDERS, seg("folder", "indexes")])
    assert [(n.name, n.detail) for n in indexes] == [("PRIMARY", "primary"), ("orders_customer_idx", None)]
    [fk] = introspection.list_children(conn, [*ORDERS, seg("folder", "foreign_keys")])
    assert (fk.kind, fk.name, fk.detail) == ("foreign_key", "orders_customer_fk", "(customer_id) → dbaguy.customers (id)")
    columns = introspection.list_children(conn, [*ORDERS, seg("folder", "columns")])
    assert columns[0].name == "id" and columns[0].detail == "bigint NOT NULL PK"


def test_tree_node_shapes(conn):
    """Kinds, leaf flags and paths of the nodes at each level."""
    tables = introspection.list_children(conn, [*DB, seg("folder", "tables")])
    assert all(n.kind == "table" and n.has_children and n.detail is None for n in tables)
    functions = introspection.list_children(conn, [*DB, seg("folder", "functions")])
    assert [(n.kind, n.name, n.has_children) for n in functions] == [("function", "add_numbers", False)]
    [view] = introspection.list_children(conn, [*DB, seg("folder", "views")])
    assert view.kind == "view" and view.has_children
    assert view.path == [*DB, seg("folder", "views"), seg("view", "order_summary")]
    assert names(introspection.list_children(conn, view.path)) == ["columns"]  # a view has no indexes, FKs or triggers
    # customers: a primary key, a unique index, no foreign keys or triggers
    customers = [*DB, seg("folder", "tables"), seg("table", "customers")]
    assert names(introspection.list_children(conn, customers)) == ["columns", "indexes"]
    indexes = introspection.list_children(conn, [*customers, seg("folder", "indexes")])
    assert [(n.name, n.detail) for n in indexes] == [("PRIMARY", "primary"), ("email", "unique")]
    [trigger] = introspection.list_children(conn, [*ORDERS, seg("folder", "triggers")])
    assert (trigger.kind, trigger.name, trigger.detail) == ("trigger", "orders_note", "before insert")
    assert not trigger.has_children


def test_object_detail(conn):
    table = introspection.object_detail(conn, ORDERS)
    assert table.ddl.startswith("CREATE TABLE `orders`") and table.ddl.endswith(";") and not table.ddl_is_generated
    assert [c.name for c in table.columns][:2] == ["id", "customer_id"] and table.columns[0].is_primary_key
    customers = introspection.object_detail(conn, [*DB, seg("folder", "tables"), seg("table", "customers")])
    assert customers.comment == "Customer master data"
    proc = introspection.object_detail(conn, [*DB, seg("folder", "procedures"), seg("procedure", "two_results")])
    assert "SELECT COUNT(*) AS total FROM big_table" in proc.ddl
    event = introspection.object_detail(conn, [*DB, seg("folder", "events"), seg("event", "purge_job_log")])
    assert "EVENT `purge_job_log`" in event.ddl
    trigger = introspection.object_detail(conn, [*ORDERS, seg("folder", "triggers"), seg("trigger", "orders_note")])
    assert "BEFORE INSERT ON `orders`" in trigger.ddl
    index = introspection.object_detail(conn, [*ORDERS, seg("folder", "indexes"), seg("index", "orders_customer_idx")])
    assert index.ddl == "CREATE INDEX `orders_customer_idx` ON `dbaguy`.`orders` (`customer_id`);" and index.ddl_is_generated
    primary = introspection.object_detail(conn, [*ORDERS, seg("folder", "indexes"), seg("index", "PRIMARY")])
    assert primary.ddl == "ALTER TABLE `dbaguy`.`orders` ADD PRIMARY KEY (`id`);"
    fk = introspection.object_detail(conn, [*ORDERS, seg("folder", "foreign_keys"), seg("foreign_key", "orders_customer_fk")])
    assert fk.ddl.startswith(
        "ALTER TABLE `dbaguy`.`orders` ADD CONSTRAINT `orders_customer_fk` FOREIGN KEY (`customer_id`) "
        "REFERENCES `dbaguy`.`customers` (`id`)"
    ) and "ON DELETE CASCADE" in fk.ddl


def test_object_detail_of_views_functions_and_columns(conn):
    view = introspection.object_detail(conn, [*DB, seg("folder", "views"), seg("view", "order_summary")])
    assert view.kind == "view" and "VIEW `order_summary`" in view.ddl and not view.ddl_is_generated
    assert [c.name for c in view.columns] == ["name", "orders", "total"] and view.comment is None
    function = introspection.object_detail(conn, [*DB, seg("folder", "functions"), seg("function", "add_numbers")])
    assert "FUNCTION `add_numbers`" in function.ddl and function.ddl.endswith(";") and function.columns is None
    column = introspection.object_detail(conn, [*ORDERS, seg("folder", "columns"), seg("column", "total")])
    assert column.ddl is None and [(c.name, c.type_name, c.nullable) for c in column.columns] == [
        ("total", "decimal(30,10)", False)
    ]
    placed_at = introspection.object_detail(conn, [*ORDERS, seg("folder", "columns"), seg("column", "placed_at")])
    assert placed_at.columns[0].default is not None  # CURRENT_TIMESTAMP


def test_names_with_backticks_round_trip(conn):  # Review Focus 3
    path = [*DB, seg("folder", "tables"), seg("table", "tick`table")]
    detail = introspection.object_detail(conn, path)
    assert detail.ddl.startswith("CREATE TABLE `tick``table`") and [c.name for c in detail.columns] == ["a`b", "label"]
    assert names(introspection.list_children(conn, [*path, seg("folder", "columns")])) == ["a`b", "label"]
    spaced = introspection.object_detail(conn, [*DB, seg("folder", "tables"), seg("table", "Order Items")])
    assert [c.name for c in spaced.columns] == ["id", "select", "order"]


def test_generated_ddl_quotes_odd_names(conn):  # Review Focus 3: backtick, CJK, reserved word, space
    tick = [*DB, seg("folder", "tables"), seg("table", "tick`table")]
    primary = introspection.object_detail(conn, [*tick, seg("folder", "indexes"), seg("index", "PRIMARY")])
    assert primary.ddl == "ALTER TABLE `dbaguy`.`tick``table` ADD PRIMARY KEY (`a``b`);"
    cjk = [*DB, seg("folder", "tables"), seg("table", "客戶備註")]
    primary = introspection.object_detail(conn, [*cjk, seg("folder", "indexes"), seg("index", "PRIMARY")])
    assert primary.ddl == "ALTER TABLE `dbaguy`.`客戶備註` ADD PRIMARY KEY (`編號`);" and primary.ddl_is_generated
    reserved = [*DB, seg("folder", "tables"), seg("table", "group")]
    primary = introspection.object_detail(conn, [*reserved, seg("folder", "indexes"), seg("index", "PRIMARY")])
    assert primary.ddl == "ALTER TABLE `dbaguy`.`group` ADD PRIMARY KEY (`id`);"
    spaced = [*DB, seg("folder", "tables"), seg("table", "Order Items")]
    assert names(introspection.list_children(conn, [*spaced, seg("folder", "indexes")])) == ["PRIMARY"]


def test_cjk_and_reserved_word_tables_round_trip(conn):  # Review Focus 3
    cjk = [*DB, seg("folder", "tables"), seg("table", "客戶備註")]
    assert "客戶備註" in names(introspection.list_children(conn, [*DB, seg("folder", "tables")]))
    detail = introspection.object_detail(conn, cjk)
    assert detail.ddl.startswith("CREATE TABLE `客戶備註`") and "`編號`" in detail.ddl and "`內容`" in detail.ddl
    assert [(c.name, c.type_name) for c in detail.columns] == [("編號", "int"), ("內容", "varchar(50)")]
    children = introspection.list_children(conn, [*cjk, seg("folder", "columns")])
    assert [(n.name, n.detail) for n in children] == [("編號", "int NOT NULL PK"), ("內容", "varchar(50)")]
    leaf = introspection.object_detail(conn, [*cjk, seg("folder", "columns"), seg("column", "內容")])
    assert [c.name for c in leaf.columns] == ["內容"]

    reserved = [*DB, seg("folder", "tables"), seg("table", "group")]
    detail = introspection.object_detail(conn, reserved)
    assert detail.ddl.startswith("CREATE TABLE `group`") and [c.name for c in detail.columns] == ["id", "order"]
    children = introspection.list_children(conn, [*reserved, seg("folder", "columns")])
    assert names(children) == ["id", "order"]
    assert names(introspection.list_children(conn, reserved)) == ["columns", "indexes"]


def test_missing_objects_are_not_found(conn):
    with pytest.raises(NotFound):
        introspection.object_detail(conn, [*DB, seg("folder", "tables"), seg("table", "nope")])
    with pytest.raises(NotFound):
        introspection.list_children(conn, [seg("schema", "nope")])


def test_every_missing_kind_is_not_found(conn):
    """Unknown table, routine, trigger, event, index, foreign key, column and schema all become NotFound."""
    missing = [
        [*DB, seg("folder", "views"), seg("view", "nope")],
        [*DB, seg("folder", "procedures"), seg("procedure", "nope")],
        [*DB, seg("folder", "functions"), seg("function", "nope")],
        [*DB, seg("folder", "triggers"), seg("trigger", "nope")],
        [*DB, seg("folder", "events"), seg("event", "nope")],
        [*ORDERS, seg("folder", "indexes"), seg("index", "nope")],
        [*ORDERS, seg("folder", "foreign_keys"), seg("foreign_key", "nope")],
        [*ORDERS, seg("folder", "columns"), seg("column", "nope")],
        [seg("schema", "nope"), seg("folder", "tables"), seg("table", "orders")],
    ]
    for path in missing:
        with pytest.raises(NotFound):
            introspection.object_detail(conn, path)
    with pytest.raises(NotFound, match="table 'nope' not found"):
        introspection.object_detail(conn, [*DB, seg("folder", "tables"), seg("table", "nope")])
    with pytest.raises(NotFound, match="Schema 'nope' not found"):
        introspection.object_detail(conn, [seg("schema", "nope"), seg("folder", "tables"), seg("table", "orders")])
    # a name with a "%" must not be taken for a format placeholder
    with pytest.raises(NotFound):
        introspection.object_detail(conn, [*DB, seg("folder", "tables"), seg("table", "100%s%")])


def test_invalid_paths_are_rejected(conn):
    with pytest.raises(ValidationFailed):
        introspection.list_children(conn, [*DB, seg("folder", "nonsense")])
    with pytest.raises(ValidationFailed):
        introspection.list_children(conn, [*ORDERS, seg("folder", "nonsense")])
    with pytest.raises(ValidationFailed):
        introspection.list_children(conn, [*DB, seg("folder", "columns")])  # not a schema-level folder
    with pytest.raises(ValidationFailed):
        introspection.list_children(conn, [seg("table", "orders")])  # must start with a schema
    with pytest.raises(ValidationFailed):
        introspection.object_detail(conn, [])
    with pytest.raises(ValidationFailed):
        introspection.object_detail(conn, [seg("table", "orders")])
    with pytest.raises(ValidationFailed, match="No details available for folder"):
        introspection.object_detail(conn, [*DB, seg("folder", "tables")])
    with pytest.raises(ValidationFailed, match="No details available for schema"):
        introspection.object_detail(conn, DB)


def rel(snapshot, schema_name, name):
    schema = next(s for s in snapshot.schemas if s.name == schema_name)
    return next(r for r in schema.relations if r.name == name)


def test_snapshot(conn):
    snap = mysql_schema.build_snapshot(conn)
    assert (snap.database, snap.default_schema, snap.search_path) == (None, "dbaguy", ["dbaguy"])
    big = rel(snap, "dbaguy", "big_table")
    assert [(c.name, c.type_name) for c in big.columns] == [("id", "int"), ("label", "varchar(50)"), ("value", "double")]
    orders = rel(snap, "dbaguy", "orders")
    assert orders.primary_key == ["id"]
    assert [(f.name, f.columns, f.ref_schema, f.ref_table, f.ref_columns) for f in orders.foreign_keys] == [
        ("orders_customer_fk", ["customer_id"], "dbaguy", "customers", ["id"])
    ]
    assert rel(snap, "dbaguy", "order_summary").kind == "view"
    assert rel(snap, "analytics", "page_views").kind == "table"
    dbaguy = next(s for s in snap.schemas if s.name == "dbaguy")
    assert dbaguy.functions == ["add_numbers"] and not dbaguy.system
    performance = next(s for s in snap.schemas if s.name == "performance_schema")  # visible to dbaguy (R-03a-P2)
    assert performance.system and performance.relations == []


def test_snapshot_keeps_odd_names(conn):  # Review Focus 3
    snap = mysql_schema.build_snapshot(conn)
    tick = rel(snap, "dbaguy", "tick`table")
    assert [c.name for c in tick.columns] == ["a`b", "label"] and tick.primary_key == ["a`b"]
    cjk = rel(snap, "dbaguy", "客戶備註")
    assert [c.name for c in cjk.columns] == ["編號", "內容"] and cjk.primary_key == ["編號"]
    reserved = rel(snap, "dbaguy", "group")
    assert [c.name for c in reserved.columns] == ["id", "order"] and reserved.primary_key == ["id"]
    assert [c.name for c in rel(snap, "dbaguy", "Order Items").columns] == ["id", "select", "order"]
    assert rel(snap, "dbaguy", "customers").foreign_keys == []


def test_snapshot_over_the_limit_loads_only_the_current_database(conn, monkeypatch):
    monkeypatch.setattr(mysql_schema, "RELATION_LIMIT", 5)
    snap = mysql_schema.build_snapshot(conn)
    assert rel(snap, "dbaguy", "big_table")
    assert next(s for s in snap.schemas if s.name == "analytics").relations == []


def test_snapshot_at_the_limit_loads_everything(conn, monkeypatch):
    (total,) = catalog.first_row(
        conn, "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA NOT IN ('information_schema', "
              "'mysql', 'performance_schema', 'sys')"
    )
    monkeypatch.setattr(mysql_schema, "RELATION_LIMIT", total)  # over the limit means strictly more
    snap = mysql_schema.build_snapshot(conn)
    assert rel(snap, "analytics", "page_views")


def test_snapshot_without_a_current_database(monkeypatch):
    connection = mysql.connector.connect(**mysql_driver_kwargs(database=None))
    try:
        snap = mysql_schema.build_snapshot(connection)
        assert (snap.default_schema, snap.search_path) == (None, [])
        assert rel(snap, "analytics", "page_views") and rel(snap, "dbaguy", "orders")
        monkeypatch.setattr(mysql_schema, "RELATION_LIMIT", 5)  # over the limit and nothing current: nothing loaded
        limited = mysql_schema.build_snapshot(connection)
        assert {s.name for s in limited.schemas} >= {"dbaguy", "analytics"}
        assert all(s.relations == [] and s.functions == [] for s in limited.schemas)
    finally:
        connection.close()


def test_snapshot_runs_seven_queries(conn, sql_log):
    mysql_schema.build_snapshot(conn)
    assert len(sql_log) == 7


def test_catalog_sql_never_says_delimiter(conn, sql_log):
    """Task 9 splits statements client-side and treats the word as a client command, so no internal SQL may use it."""
    walk = [
        [], DB, [*DB, seg("folder", "tables")], [*DB, seg("folder", "triggers")], [*DB, seg("folder", "events")],
        ORDERS, [*ORDERS, seg("folder", "columns")], [*ORDERS, seg("folder", "indexes")],
        [*ORDERS, seg("folder", "foreign_keys")], [*ORDERS, seg("folder", "triggers")],
    ]
    for path in walk:
        introspection.list_children(conn, path)
    for path in (
        ORDERS, [*DB, seg("folder", "procedures"), seg("procedure", "two_results")],
        [*ORDERS, seg("folder", "indexes"), seg("index", "PRIMARY")],
        [*ORDERS, seg("folder", "foreign_keys"), seg("foreign_key", "orders_customer_fk")],
    ):
        introspection.object_detail(conn, path)
    mysql_schema.build_snapshot(conn)
    assert sql_log and not any("delimiter" in sql.lower() for sql in sql_log)


def test_catalog_query_decodes_and_never_pings(conn, monkeypatch):
    _ = conn.sql_mode  # the driver reads it once, through conn.cursor() (a PING), before it substitutes the first parameters

    def no_ping(*args, **kwargs):
        raise AssertionError("conn.cursor() sends a PING; catalog queries must build the cursor themselves")

    monkeypatch.setattr(conn, "cmd_ping", no_ping)
    rows = catalog.query(conn, "SELECT %s, CAST(%s AS BINARY), %s", ("a`b", "客戶 😀", 7))
    assert rows == [("a`b", "客戶 😀", 7)]  # bytes cells are decoded as UTF-8
    assert catalog.first_row(conn, "SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", ["nope"]) is None
    assert catalog.first_row(conn, "SELECT %s", ["x"]) == ("x",)
    assert catalog.query(conn, "SELECT 1 FROM information_schema.SCHEMATA WHERE 1 = 0") == []


def test_catalog_constants():
    assert catalog.SYSTEM_SCHEMAS == frozenset({"information_schema", "mysql", "performance_schema", "sys"})
    assert catalog.MISSING_OBJECT_ERRNOS == frozenset({1049, 1146, 1305, 1360, 1539})
