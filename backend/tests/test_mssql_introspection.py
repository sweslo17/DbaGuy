import pytest

from dbaguy_backend.adapters.mssql import catalog, introspection
from dbaguy_backend.adapters.mssql import schema as mssql_schema
from dbaguy_backend.domain.errors import NotFound, ValidationFailed
from dbaguy_backend.domain.metadata import PathSegment
from tests.support import mssql_connect, requires_mssql

pytestmark = requires_mssql


@pytest.fixture
def conn():
    connection = mssql_connect()
    yield connection
    connection.close()


class Recorder:
    """A connection that records every statement it runs (a Cython connection cannot be patched, so it is wrapped)."""

    def __init__(self, connection):
        self._connection, self.statements = connection, []

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def execute_query(self, sql, *args):
        assert not args, "internal SQL never takes driver parameters"
        self.statements.append(sql)
        return self._connection.execute_query(sql)


@pytest.fixture
def recorded(conn):
    return Recorder(conn)


def seg(kind, name):
    return PathSegment(kind=kind, name=name)


def names(nodes):
    return [n.name for n in nodes]


DB = [seg("database", "dbaguy")]
DBO = [*DB, seg("schema", "dbo")]
SALES = [*DB, seg("schema", "sales")]
ORDERS = [*SALES, seg("folder", "tables"), seg("table", "orders")]


def test_tree(conn):
    root = introspection.list_children(conn, [])
    assert names(root) == ["analytics", "dbaguy", "master", "msdb", "tempdb"]  # no model, no locked_away
    assert [n.system for n in root] == [False, False, True, True, True] and {n.kind for n in root} == {"database"}
    schemas = introspection.list_children(conn, DB)
    assert names(schemas)[:2] == ["dbo", "sales"] and not any(n.system for n in schemas[:2])
    assert {"sys", "INFORMATION_SCHEMA", "guest", "db_owner"} <= {n.name for n in schemas if n.system}
    assert names(introspection.list_children(conn, DBO)) == ["tables", "views", "functions", "procedures", "synonyms", "types"]
    assert names(introspection.list_children(conn, SALES)) == ["tables", "functions", "sequences"]
    tables = set(names(introspection.list_children(conn, [*DBO, seg("folder", "tables")])))
    assert {"big_table", "customers", "types_demo", "Order Items", "brack]et", "job_log"} <= tables
    assert set(names(introspection.list_children(conn, [*DBO, seg("folder", "procedures")]))) == {
        "two_results", "fail_after_select", "noisy"}
    def leaves(path):
        return [(n.kind, n.name, n.detail) for n in introspection.list_children(conn, path)]
    assert leaves([*DBO, seg("folder", "functions")]) == [("function", "add_numbers", "scalar")]
    assert leaves([*SALES, seg("folder", "functions")]) == [("function", "orders_of", "inline table")]
    assert leaves([*DBO, seg("folder", "synonyms")]) == [("synonym", "customer_alias", "[dbo].[customers]")]
    assert leaves([*DBO, seg("folder", "types")]) == [("type", "email_address", "nvarchar(200)"), ("type", "id_list", "table type")]
    assert leaves([*SALES, seg("folder", "sequences")]) == [("sequence", "order_seq", "bigint")]
    assert names(introspection.list_children(conn, ORDERS)) == ["columns", "indexes", "foreign_keys", "triggers"]
    assert leaves([*ORDERS, seg("folder", "indexes")]) == [("index", "orders_pk", "primary"), ("index", "orders_customer_idx", None)]
    assert leaves([*ORDERS, seg("folder", "foreign_keys")]) == [
        ("foreign_key", "orders_customer_fk", "(customer_id) → dbo.customers (id)")]
    assert leaves([*ORDERS, seg("folder", "triggers")]) == [("trigger", "orders_note", "after insert")]
    customers = [*DBO, seg("folder", "tables"), seg("table", "customers")]
    columns = leaves([*customers, seg("folder", "columns")])
    assert columns[:2] == [("column", "id", "bigint NOT NULL PK"), ("column", "email", "email_address NOT NULL")]


def test_table_ddl_is_generated_and_complete(conn):
    customers = introspection.object_detail(conn, [*DBO, seg("folder", "tables"), seg("table", "customers")])
    assert customers.ddl_is_generated and customers.comment == "Customer master data"
    assert customers.ddl.startswith("CREATE TABLE [dbo].[customers] (\n    [id] bigint IDENTITY(1, 1) NOT NULL,")
    for part in ("[email] [dbo].[email_address] NOT NULL", "CONSTRAINT [customers_created_df] DEFAULT (sysutcdatetime())",
                 "CONSTRAINT [customers_pk] PRIMARY KEY CLUSTERED ([id])",
                 "CONSTRAINT [customers_email_uq] UNIQUE NONCLUSTERED ([email])",
                 "CONSTRAINT [customers_profile_json] CHECK ([profile] IS NULL OR isjson([profile])=(1))"):
        assert part in customers.ddl, part
    assert [c.name for c in customers.columns] == ["id", "email", "name", "profile", "created_at"]
    assert customers.columns[0].is_primary_key and customers.columns[4].default == "(sysutcdatetime())"
    orders = introspection.object_detail(conn, ORDERS)
    assert ("CONSTRAINT [orders_customer_fk] FOREIGN KEY ([customer_id]) REFERENCES [dbo].[customers] ([id]) "
            "ON DELETE CASCADE ON UPDATE NO ACTION") in orders.ddl
    assert orders.ddl.endswith(
        ");\nCREATE NONCLUSTERED INDEX [orders_customer_idx] ON [sales].[orders] ([customer_id]) INCLUDE ([total]) "
        "WHERE ([note] IS NOT NULL);")
    demo = introspection.object_detail(conn, [*DBO, seg("folder", "tables"), seg("table", "types_demo")])
    assert "[legacy] varchar(50) COLLATE Chinese_Taiwan_Stroke_CI_AS NULL" in demo.ddl  # only a non-default collation
    assert "[precise] datetime2(7) NULL" in demo.ddl and "[data] varbinary(max) NULL" in demo.ddl


def test_other_details(conn):
    def ddl(path):
        return introspection.object_detail(conn, path).ddl
    assert ddl([*ORDERS, seg("folder", "indexes"), seg("index", "orders_pk")]) == (
        "ALTER TABLE [sales].[orders] ADD CONSTRAINT [orders_pk] PRIMARY KEY CLUSTERED ([id]);")
    assert ddl([*ORDERS, seg("folder", "foreign_keys"), seg("foreign_key", "orders_customer_fk")]) == (
        "ALTER TABLE [sales].[orders] ADD CONSTRAINT [orders_customer_fk] FOREIGN KEY ([customer_id]) "
        "REFERENCES [dbo].[customers] ([id]) ON DELETE CASCADE ON UPDATE NO ACTION;")
    view = introspection.object_detail(conn, [*DBO, seg("folder", "views"), seg("view", "order_summary")])
    assert view.ddl.startswith("CREATE VIEW dbo.order_summary AS") and not view.ddl_is_generated
    assert [c.name for c in view.columns] == ["name", "orders", "total"]
    assert "SELECT COUNT(*) AS total FROM dbo.big_table" in ddl([*DBO, seg("folder", "procedures"), seg("procedure", "two_results")])
    assert "CREATE TRIGGER sales.orders_note ON sales.orders AFTER INSERT" in ddl(
        [*ORDERS, seg("folder", "triggers"), seg("trigger", "orders_note")])
    assert ddl([*SALES, seg("folder", "sequences"), seg("sequence", "order_seq")]) == (
        "CREATE SEQUENCE [sales].[order_seq] AS bigint START WITH 1000 INCREMENT BY 10 "
        "MINVALUE -9223372036854775808 MAXVALUE 9223372036854775807 NO CYCLE CACHE;")
    assert ddl([*DBO, seg("folder", "synonyms"), seg("synonym", "customer_alias")]) == (
        "CREATE SYNONYM [dbo].[customer_alias] FOR [dbo].[customers];")
    assert ddl([*DBO, seg("folder", "types"), seg("type", "email_address")]) == (
        "CREATE TYPE [dbo].[email_address] FROM nvarchar(200) NOT NULL;")
    assert ddl([*DBO, seg("folder", "types"), seg("type", "id_list")]) == (
        "CREATE TYPE [dbo].[id_list] AS TABLE (\n    [id] int NOT NULL,\n    PRIMARY KEY CLUSTERED ([id])\n);")


def test_names_with_brackets_round_trip(conn):
    path = [*DBO, seg("folder", "tables"), seg("table", "brack]et")]
    detail = introspection.object_detail(conn, path)
    assert detail.ddl.startswith("CREATE TABLE [dbo].[brack]]et] (\n    [a]]b] int NOT NULL,")
    assert [c.name for c in detail.columns] == ["a]b", "label"]
    assert names(introspection.list_children(conn, [*path, seg("folder", "columns")])) == ["a]b", "label"]


# Names that need quoting, through the tree, the generated DDL and the snapshot: (table, its columns, a column's type).
AWKWARD_TABLES = [
    ("brack]et", ["a]b", "label"], "int"),
    ("Order Items", ["id", "select", "order"], "int"),
    ("客戶備註", ["編號", "內容"], "int"),
    ("group", ["id", "order"], "int"),
]


@pytest.mark.parametrize("table, columns, first_type", AWKWARD_TABLES)
def test_awkward_names_round_trip(conn, table, columns, first_type):
    path = [*DBO, seg("folder", "tables"), seg("table", table)]
    assert table in names(introspection.list_children(conn, [*DBO, seg("folder", "tables")]))
    assert names(introspection.list_children(conn, path)) == ["columns", "indexes"]  # each has a primary key
    leaf_nodes = introspection.list_children(conn, [*path, seg("folder", "columns")])
    assert names(leaf_nodes) == columns
    detail = introspection.object_detail(conn, path)
    assert [c.name for c in detail.columns] == columns
    for column in columns:  # the generated CREATE TABLE writes every name quoted, with `]` doubled
        assert catalog.q(column) in detail.ddl
    assert detail.ddl.startswith(f"CREATE TABLE {catalog.q('dbo')}.{catalog.q(table)} (\n")
    for node in leaf_nodes:  # each column leaf resolves to its own detail
        leaf = introspection.object_detail(conn, node.path)
        assert [c.name for c in leaf.columns] == [node.name] and leaf.ddl is None
    snapshot = mssql_schema.build_snapshot(conn)
    assert [c.name for c in rel(snapshot, "dbo", table).columns] == columns
    assert rel(snapshot, "dbo", table).columns[0].type_name == first_type


def test_awkward_names_can_be_run_back(conn):
    """The generated DDL of a table whose names need quoting is valid T-SQL: it parses (SET PARSEONLY) unchanged."""
    ddls = [
        introspection.object_detail(conn, [*DBO, seg("folder", "tables"), seg("table", table)]).ddl
        for table, _, _ in AWKWARD_TABLES
    ]
    conn.execute_non_query("SET PARSEONLY ON")  # nothing below runs: the server only checks the syntax
    try:
        for ddl in ddls:
            conn.execute_non_query(ddl)  # raises on a syntax error
    finally:
        conn.execute_non_query("SET PARSEONLY OFF")


def test_missing_objects_are_not_found(conn):
    with pytest.raises(NotFound):
        introspection.object_detail(conn, [*DBO, seg("folder", "tables"), seg("table", "nope")])
    with pytest.raises(NotFound):
        introspection.list_children(conn, [*DB, seg("schema", "nope")])
    with pytest.raises(NotFound):
        introspection.object_detail(conn, [*ORDERS, seg("folder", "indexes"), seg("index", "nope")])


HOSTILE = "x'; DROP TABLE [dbo].[customers]; --%s]]"  # a name is data: quotes, brackets and `%` change nothing


@pytest.mark.parametrize("ghost", ["ghost", HOSTILE, "鬼"])
def test_stale_nodes_at_every_level_are_not_found(conn, ghost):
    ghost_schema = [*DB, seg("schema", ghost)]
    ghost_table = [*DBO, seg("folder", "tables"), seg("table", ghost)]
    for path in (
        ghost_schema,  # a schema's folders
        [*ghost_schema, seg("folder", "tables")],  # objects of a schema that is gone
        ghost_table,  # a table's folders
        [*ghost_table, seg("folder", "columns")],  # a table's columns
        [*ghost_table, seg("folder", "indexes")],
        [*[*DBO, seg("folder", "views")], seg("view", ghost)],
    ):
        with pytest.raises(NotFound):
            introspection.list_children(conn, path)
    for path, kind in (
        ([*DBO, seg("folder", "tables"), seg("table", ghost)], "table"),
        ([*DBO, seg("folder", "views"), seg("view", ghost)], "view"),
        ([*DBO, seg("folder", "procedures"), seg("procedure", ghost)], "procedure"),
        ([*DBO, seg("folder", "functions"), seg("function", ghost)], "function"),
        ([*SALES, seg("folder", "sequences"), seg("sequence", ghost)], "sequence"),
        ([*DBO, seg("folder", "synonyms"), seg("synonym", ghost)], "synonym"),
        ([*DBO, seg("folder", "types"), seg("type", ghost)], "type"),
        ([*ORDERS, seg("folder", "indexes"), seg("index", ghost)], "index"),
        ([*ORDERS, seg("folder", "foreign_keys"), seg("foreign_key", ghost)], "foreign_key"),
        ([*ORDERS, seg("folder", "triggers"), seg("trigger", ghost)], "trigger"),
        ([*ORDERS, seg("folder", "columns"), seg("column", ghost)], "column"),
        ([*ghost_schema, seg("folder", "tables"), seg("table", "orders")], "table"),
    ):
        with pytest.raises(NotFound) as caught:
            introspection.object_detail(conn, path)
        assert str(caught.value).startswith(f"{kind} '") and str(caught.value).endswith("' not found")
    assert "customers" in names(introspection.list_children(conn, [*DBO, seg("folder", "tables")]))


def test_an_empty_table_folder_of_an_existing_table_is_just_empty(conn):
    """The existence check must not turn a legitimately empty answer into NotFound."""
    path = [*DBO, seg("folder", "tables"), seg("table", "job_log")]
    assert introspection.list_children(conn, [*path, seg("folder", "foreign_keys")]) == []
    assert introspection.list_children(conn, [*path, seg("folder", "triggers")]) == []


def test_bad_paths_are_validation_failures(conn):
    with pytest.raises(ValidationFailed):
        introspection.list_children(conn, [seg("schema", "dbo")])  # a path starts at a database
    with pytest.raises(ValidationFailed):
        introspection.list_children(conn, [*DB, seg("folder", "tables")])  # then a schema
    with pytest.raises(ValidationFailed):
        introspection.list_children(conn, [*DBO, seg("folder", "no_such_folder")])
    with pytest.raises(ValidationFailed):
        introspection.list_children(conn, [*DBO, seg("folder", "columns")])  # a folder of tables, not of a schema
    with pytest.raises(ValidationFailed):
        introspection.list_children(conn, [*ORDERS, seg("folder", "tables")])
    with pytest.raises(ValidationFailed, match="No details available for database"):
        introspection.object_detail(conn, DB)
    with pytest.raises(ValidationFailed, match="No details available for folder"):
        introspection.object_detail(conn, [*DBO, seg("folder", "tables")])
    with pytest.raises(ValidationFailed):
        introspection.object_detail(conn, [])


def test_a_view_has_columns_only_and_leaves_have_no_children(conn):
    view = [*DBO, seg("folder", "views"), seg("view", "order_summary")]
    assert names(introspection.list_children(conn, view)) == ["columns"]
    assert introspection.list_children(conn, [*DBO, seg("folder", "procedures"), seg("procedure", "noisy")]) == []


def test_a_table_without_indexes_or_triggers_lists_only_what_it_has(conn):
    path = [*DBO, seg("folder", "tables"), seg("table", "types_demo")]
    assert names(introspection.list_children(conn, path)) == ["columns", "indexes"]  # the primary key; no FK, no trigger


class _Canned:
    """A connection whose answers are canned by a word of the statement: for what the dev server cannot show."""

    def __init__(self, answers):
        self.answers, self.statements, self._rows = answers, [], []

    def execute_query(self, sql):
        self.statements.append(sql)
        self._rows = next(rows for word, rows in self.answers if word in sql)

    def get_iterator(self, _row_format):
        return iter(self._rows)

    def nextresult(self):
        return None


def test_an_unreadable_definition_is_no_ddl_and_not_an_error():
    """Without VIEW DEFINITION (or for an encrypted module) OBJECT_DEFINITION is NULL: the object is there, its text not."""
    canned = _Canned([("OBJECT_DEFINITION", [(12345, None)]), ("sys.columns", [])])
    for kind in ("view", "procedure", "function"):
        detail = introspection.object_detail(canned, [*DBO, seg("folder", kind + "s"), seg(kind, "hidden")])
        assert detail.ddl is None and not detail.ddl_is_generated and detail.name == "hidden"


def test_a_limited_login_still_browses_a_database_it_only_reaches_as_guest():
    """dbaguy is `guest` in msdb, so the catalog hides most of it and OBJECT_DEFINITION is NULL for what it does show:
    that is an object without DDL, never an error, and its columns stay."""
    guest = mssql_connect(database="msdb")
    try:
        msdb = [seg("database", "msdb")]
        msdb_dbo = [*msdb, seg("schema", "dbo")]
        assert "dbo" in names(introspection.list_children(guest, msdb))
        for folder in introspection.list_children(guest, msdb_dbo):
            for node in introspection.list_children(guest, folder.path)[:5]:
                introspection.object_detail(guest, node.path)  # never an error, DDL or not
                if node.has_children:
                    introspection.list_children(guest, node.path)
        hidden = introspection.object_detail(guest, [*msdb_dbo, seg("folder", "views"), seg("view", "syspolicy_conditions")])
        assert hidden.ddl is None and not hidden.ddl_is_generated and hidden.columns
        snapshot = mssql_schema.build_snapshot(guest)  # a default schema other than dbo: both are searched, in that order
        assert (snapshot.database, snapshot.default_schema, snapshot.search_path) == ("msdb", "guest", ["guest", "dbo"])
    finally:
        guest.close()


def rel(snapshot, schema_name, name):
    schema = next(s for s in snapshot.schemas if s.name == schema_name)
    return next(r for r in schema.relations if r.name == name)


def test_snapshot(conn):
    snap = mssql_schema.build_snapshot(conn)
    assert (snap.database, snap.default_schema, snap.search_path) == ("dbaguy", "dbo", ["dbo"])
    big = rel(snap, "dbo", "big_table")
    assert [(c.name, c.type_name) for c in big.columns] == [("id", "int"), ("label", "nvarchar(50)"), ("value", "float")]
    orders = rel(snap, "sales", "orders")
    assert orders.primary_key == ["id"]
    assert [(f.name, f.columns, f.ref_schema, f.ref_table, f.ref_columns) for f in orders.foreign_keys] == [
        ("orders_customer_fk", ["customer_id"], "dbo", "customers", ["id"])]
    assert rel(snap, "dbo", "order_summary").kind == "view"
    by_name = {s.name: s for s in snap.schemas}
    assert by_name["dbo"].functions == ["add_numbers"] and by_name["sales"].functions == ["orders_of"]
    assert by_name["sys"].system and by_name["sys"].relations == [] and not by_name["dbo"].system


def test_snapshot_uses_a_fixed_number_of_queries(recorded):
    """Seven queries, however many tables there are; the dev database has dozens of relations."""
    mssql_schema.build_snapshot(recorded)
    assert len(recorded.statements) == 7


@pytest.mark.parametrize("label, path", [
    ("databases", []),
    ("schemas", DB),
    ("schema folders", DBO),
    ("tables", [*DBO, seg("folder", "tables")]),
    ("views", [*DBO, seg("folder", "views")]),
    ("functions", [*DBO, seg("folder", "functions")]),
    ("sequences", [*SALES, seg("folder", "sequences")]),
    ("synonyms", [*DBO, seg("folder", "synonyms")]),
    ("types", [*DBO, seg("folder", "types")]),
    ("table folders", ORDERS),
    ("view folders", [*DBO, seg("folder", "views"), seg("view", "order_summary")]),
    ("columns", [*ORDERS, seg("folder", "columns")]),
    ("indexes", [*ORDERS, seg("folder", "indexes")]),
    ("foreign keys", [*ORDERS, seg("folder", "foreign_keys")]),
    ("table triggers", [*ORDERS, seg("folder", "triggers")]),
])
def test_listing_is_one_round_trip(recorded, label, path):
    """Over an SSH tunnel every query costs a round trip: the schema/table check rides on the level's own query."""
    introspection.list_children(recorded, path)
    assert len(recorded.statements) == 1, f"{label}: {recorded.statements}"


@pytest.mark.parametrize("label, path, queries", [
    ("table", ORDERS, 6),  # columns, keys, checks, foreign keys, indexes, comment
    ("view", [*DBO, seg("folder", "views"), seg("view", "order_summary")], 2),  # definition, columns
    ("procedure", [*DBO, seg("folder", "procedures"), seg("procedure", "two_results")], 1),
    ("trigger", [*ORDERS, seg("folder", "triggers"), seg("trigger", "orders_note")], 1),
    ("sequence", [*SALES, seg("folder", "sequences"), seg("sequence", "order_seq")], 1),
    ("alias type", [*DBO, seg("folder", "types"), seg("type", "email_address")], 1),
    ("index", [*ORDERS, seg("folder", "indexes"), seg("index", "orders_pk")], 1),
    ("foreign key", [*ORDERS, seg("folder", "foreign_keys"), seg("foreign_key", "orders_customer_fk")], 1),
    ("column", [*ORDERS, seg("folder", "columns"), seg("column", "id")], 1),
])
def test_detail_has_no_query_for_the_existence_check(recorded, label, path, queries):
    introspection.object_detail(recorded, path)
    assert len(recorded.statements) == queries, f"{label}: {recorded.statements}"


def test_internal_sql_is_never_a_batch_separator(recorded):
    """`GO` alone on a line splits a script client-side; no catalog statement may contain such a line."""
    for path in ([], DB, DBO, ORDERS, [*ORDERS, seg("folder", "columns")], [*DBO, seg("folder", "types")]):
        introspection.list_children(recorded, path)
    introspection.object_detail(recorded, ORDERS)
    mssql_schema.build_snapshot(recorded)
    assert recorded.statements
    assert not any(line.strip().lower() == "go" for sql in recorded.statements for line in sql.splitlines())
