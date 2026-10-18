import re
from contextlib import contextmanager
from uuid import uuid4

import pytest

from dbaguy_backend.adapters.mssql import catalog, introspection
from dbaguy_backend.adapters.mssql import schema as mssql_schema
from dbaguy_backend.domain.errors import NotFound, ValidationFailed
from dbaguy_backend.domain.metadata import PathSegment
from tests.support import MSSQL_SA_PASSWORD, mssql_connect, requires_mssql

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
    """A connection whose answers are canned by a word of the statement: for what the dev server cannot show. An answer
    is the list of result sets the statement returns."""

    def __init__(self, answers):
        self.answers, self._sets = answers, []

    def execute_query(self, sql):
        self._sets = list(next(sets for word, sets in self.answers if word in sql))

    def get_iterator(self, _row_format):
        return iter(self._sets.pop(0))

    def nextresult(self):
        return bool(self._sets)


def test_an_unreadable_definition_is_no_ddl_and_not_an_error():
    """Without VIEW DEFINITION (or for an encrypted module) OBJECT_DEFINITION is NULL: the object is there, its text not."""
    for kind, sets in (("view", [[(12345, None)], []]), ("procedure", [[(12345, None)]]), ("function", [[(12345, None)]])):
        canned = _Canned([("OBJECT_DEFINITION", sets)])
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
                found = introspection.object_detail(guest, node.path)  # never an error, DDL or not
                assert found.ddl is None or "None" not in found.ddl
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


def test_snapshot_is_seven_queries_in_one_round_trip(recorded):
    """Seven queries whatever the number of tables, sent as one batch."""
    mssql_schema.build_snapshot(recorded)
    assert len(mssql_schema._QUERIES) == 7 and len(recorded.statements) == 1


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
    ("system schema folders", [*DB, seg("schema", "sys")]),
    ("system views", [*DB, seg("schema", "sys"), seg("folder", "views")]),
    ("system procedures", [*DB, seg("schema", "sys"), seg("folder", "procedures")]),
    ("system view folders", [*DB, seg("schema", "sys"), seg("folder", "views"), seg("view", "objects")]),
    ("system view columns", [*DB, seg("schema", "sys"), seg("folder", "views"), seg("view", "objects"), seg("folder", "columns")]),
])
def test_listing_is_one_round_trip(recorded, label, path):
    """Over an SSH tunnel every query costs a round trip: the schema/table check rides on the level's own query."""
    introspection.list_children(recorded, path)
    assert len(recorded.statements) == 1, f"{label}: {recorded.statements}"


@pytest.mark.parametrize("label, path, queries", [
    ("table", ORDERS, 1),  # columns, keys, checks, foreign keys, indexes and comment: one batch
    ("view", [*DBO, seg("folder", "views"), seg("view", "order_summary")], 1),  # definition and columns: one batch
    ("procedure", [*DBO, seg("folder", "procedures"), seg("procedure", "two_results")], 1),
    ("trigger", [*ORDERS, seg("folder", "triggers"), seg("trigger", "orders_note")], 1),
    ("sequence", [*SALES, seg("folder", "sequences"), seg("sequence", "order_seq")], 1),
    ("alias type", [*DBO, seg("folder", "types"), seg("type", "email_address")], 1),
    ("table type", [*DBO, seg("folder", "types"), seg("type", "id_list")], 1),
    ("system view", [*DB, seg("schema", "sys"), seg("folder", "views"), seg("view", "objects")], 1),
    ("index", [*ORDERS, seg("folder", "indexes"), seg("index", "orders_pk")], 1),
    ("foreign key", [*ORDERS, seg("folder", "foreign_keys"), seg("foreign_key", "orders_customer_fk")], 1),
    ("column", [*ORDERS, seg("folder", "columns"), seg("column", "id")], 1),
])
def test_detail_is_one_round_trip(recorded, label, path, queries):
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


# ---- system schemas ------------------------------------------------------------------------------------------------


def test_system_schemas_are_browsable_and_the_empty_ones_say_so(conn):
    schemas = {n.name: n for n in introspection.list_children(conn, DB)}
    assert schemas["sys"].has_children and schemas["INFORMATION_SCHEMA"].has_children and schemas["dbo"].has_children
    assert not schemas["guest"].has_children and not schemas["db_owner"].has_children  # they own nothing
    sys_schema = [*DB, seg("schema", "sys")]
    assert names(introspection.list_children(conn, sys_schema)) == ["views", "functions", "procedures"]
    views = names(introspection.list_children(conn, [*sys_schema, seg("folder", "views")]))
    assert {"objects", "tables", "all_columns", "dm_exec_sessions"} <= set(views)
    functions = introspection.list_children(conn, [*sys_schema, seg("folder", "functions")])
    assert ("function", "dm_exec_sql_text") in [(n.kind, n.name) for n in functions]
    procedures = names(introspection.list_children(conn, [*sys_schema, seg("folder", "procedures")]))
    assert {"sp_who", "sp_help", "xp_cmdshell"} <= set(procedures)
    information = [*DB, seg("schema", "INFORMATION_SCHEMA")]
    assert names(introspection.list_children(conn, information)) == ["views"]
    assert {"TABLES", "COLUMNS"} <= set(names(introspection.list_children(conn, [*information, seg("folder", "views")])))
    assert introspection.list_children(conn, [*DB, seg("schema", "guest")]) == []
    assert introspection.list_children(conn, [*DB, seg("schema", "db_owner")]) == []


def test_system_objects_have_columns_and_their_own_definition(conn):
    objects = [*DB, seg("schema", "sys"), seg("folder", "views"), seg("view", "objects")]
    assert names(introspection.list_children(conn, objects)) == ["columns"]
    assert {"name", "object_id", "schema_id"} <= set(names(introspection.list_children(conn, [*objects, seg("folder", "columns")])))
    detail = introspection.object_detail(conn, objects)
    assert detail.ddl.startswith("CREATE VIEW sys.objects") and not detail.ddl_is_generated
    assert {"name", "object_id"} <= {c.name for c in detail.columns}
    tables = [*DB, seg("schema", "INFORMATION_SCHEMA"), seg("folder", "views"), seg("view", "TABLES")]
    assert names(introspection.list_children(conn, [*tables, seg("folder", "columns")]))[:2] == ["TABLE_CATALOG", "TABLE_SCHEMA"]
    who = introspection.object_detail(conn, [*DB, seg("schema", "sys"), seg("folder", "procedures"), seg("procedure", "sp_who")])
    assert "sp_who" in who.ddl.lower()
    with pytest.raises(NotFound):
        introspection.list_children(conn, [*DB, seg("schema", "sys"), seg("folder", "views"), seg("view", "no_such_view")])
    with pytest.raises(NotFound):
        introspection.object_detail(conn, [*DB, seg("schema", "sys"), seg("folder", "views"), seg("view", "no_such_view")])


# ---- a database of its own: limited accounts, and the DDL run back --------------------------------------------------
# The dbaguy login may not create a database, so `sa` creates one (`zz_t7_<random>`) for the module and drops it by that
# exact name afterwards. Everything below connects to it as `sa` and, for a limited account, does EXECUTE AS USER with a
# user WITHOUT LOGIN: the catalog answers exactly as it would to that account.

SCRATCH_OBJECTS = [
    "CREATE SCHEMA sales",
    "CREATE TYPE dbo.zz_email FROM nvarchar(200) NOT NULL",
    "CREATE TABLE dbo.zz_parent (id int PRIMARY KEY, nm nvarchar(10))",
    """CREATE TABLE dbo.zz_full (
        id int IDENTITY(5, 2) PRIMARY KEY,
        email dbo.zz_email,
        a int NOT NULL, b int NOT NULL,
        total AS (a + b),
        made AS (a * b) PERSISTED NOT NULL,
        named_df int NOT NULL CONSTRAINT zz_full_named_df DEFAULT (7),
        made_up_df int NOT NULL DEFAULT 5,
        parent_id int NULL REFERENCES dbo.zz_parent (id),
        CONSTRAINT zz_full_named_chk CHECK (a < b),
        CHECK (a > 0),
        CONSTRAINT zz_full_uq UNIQUE (a, b))""",
    "CREATE INDEX zz_full_filtered ON dbo.zz_full (a) INCLUDE (b) WHERE a > 5",
    "CREATE INDEX zz_full_plain ON dbo.zz_full (b DESC)",
    "CREATE TABLE dbo.zz_plain (id int NOT NULL CONSTRAINT zz_plain_pk PRIMARY KEY, label nvarchar(20) NOT NULL)",
    "CREATE TABLE dbo.zz_child (id int PRIMARY KEY, parent_id int REFERENCES dbo.zz_parent (id))",
    "CREATE USER zz_reader WITHOUT LOGIN",
    "ALTER ROLE db_datareader ADD MEMBER zz_reader",  # SELECT on everything, VIEW DEFINITION on nothing
    "CREATE USER zz_child_only WITHOUT LOGIN",
    "GRANT SELECT ON dbo.zz_child TO zz_child_only",  # sees the child table, not the table it references
]


def drop_database(admin, name):
    admin.execute_non_query(
        f"IF DB_ID(N'{name}') IS NOT NULL BEGIN ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
        f"DROP DATABASE [{name}]; END")


@pytest.fixture(scope="module")
def scratch():
    name = f"zz_t7_{uuid4().hex[:8]}"
    admin = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database="master")
    try:
        admin.execute_non_query(f"CREATE DATABASE [{name}]")
        owner = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database=name)
        try:
            owner.execute_non_query("SET QUOTED_IDENTIFIER ON")
            for statement in SCRATCH_OBJECTS:
                owner.execute_non_query(statement)
        finally:
            owner.close()
        yield name
    finally:
        drop_database(admin, name)
        admin.close()


@contextmanager
def scratch_connection(scratch_name, user=None):
    """A connection to the scratch database as its owner, or (EXECUTE AS USER) as one of its limited users."""
    connection = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database=scratch_name)
    try:
        connection.execute_non_query("SET QUOTED_IDENTIFIER ON")
        if user:
            connection.execute_non_query(f"EXECUTE AS USER = '{user}'")
        yield connection
    finally:
        connection.close()


def scratch_table(scratch_name, name, schema="dbo"):
    return [seg("database", scratch_name), seg("schema", schema), seg("folder", "tables"), seg("table", name)]


FULL_COLUMNS = ["id", "email", "a", "b", "total", "made", "named_df", "made_up_df", "parent_id"]


def test_the_owner_gets_complete_ddl_without_made_up_names(scratch):
    with scratch_connection(scratch) as owner:
        detail = introspection.object_detail(owner, scratch_table(scratch, "zz_full"))
    assert detail.ddl_is_generated and [c.name for c in detail.columns] == FULL_COLUMNS
    for part in ("[id] int IDENTITY(5, 2) NOT NULL", "[email] [dbo].[zz_email] NOT NULL", "[total] AS ([a]+[b]),",
                 "[made] AS ([a]*[b]) PERSISTED NOT NULL", "[named_df] int NOT NULL CONSTRAINT [zz_full_named_df] DEFAULT ((7))",
                 "[made_up_df] int NOT NULL DEFAULT ((5))", "FOREIGN KEY ([parent_id]) REFERENCES [dbo].[zz_parent] ([id])",
                 "PRIMARY KEY CLUSTERED ([id])", "CONSTRAINT [zz_full_uq] UNIQUE NONCLUSTERED ([a], [b])",
                 "CONSTRAINT [zz_full_named_chk] CHECK ([a]<[b])", "CHECK ([a]>(0))",
                 "CREATE NONCLUSTERED INDEX [zz_full_filtered] ON [dbo].[zz_full] ([a]) INCLUDE ([b]) WHERE ([a]>(5));",
                 "CREATE NONCLUSTERED INDEX [zz_full_plain] ON [dbo].[zz_full] ([b] DESC);"):
        assert part in detail.ddl, part
    assert not re.search(r"\b(PK|DF|CK|FK|UQ)__", detail.ddl)  # nothing the server named itself
    assert "None" not in detail.ddl


def test_a_limited_account_gets_every_column_and_no_wrong_ddl(scratch):
    """db_datareader without VIEW DEFINITION: the catalog hides definitions and the account's view of user types. No
    column may vanish, and DDL that would need a hidden definition is not written at all (R-03b-T7a)."""
    with scratch_connection(scratch, "zz_reader") as limited:
        table = scratch_table(scratch, "zz_full")
        detail = introspection.object_detail(limited, table)
        assert [c.name for c in detail.columns] == FULL_COLUMNS  # the alias-typed column is still there
        assert detail.columns[1].type_name == "nvarchar(200)"  # its base type: the alias itself is not visible
        assert detail.ddl is None and not detail.ddl_is_generated
        listed = introspection.list_children(limited, [*table, seg("folder", "columns")])
        assert names(listed) == FULL_COLUMNS and listed[1].detail == "nvarchar(200) NOT NULL"
        # what is complete is still written
        plain = introspection.object_detail(limited, scratch_table(scratch, "zz_plain"))
        assert plain.ddl_is_generated and "CONSTRAINT [zz_plain_pk] PRIMARY KEY CLUSTERED ([id])" in plain.ddl
        index = [*table, seg("folder", "indexes")]
        assert introspection.object_detail(limited, [*index, seg("index", "zz_full_filtered")]).ddl is None
        assert introspection.object_detail(limited, [*index, seg("index", "zz_full_plain")]).ddl == (
            "CREATE NONCLUSTERED INDEX [zz_full_plain] ON [dbo].[zz_full] ([b] DESC);")
        assert introspection.object_detail(limited, [*index, seg("index", "zz_full_uq")]).ddl == (
            "ALTER TABLE [dbo].[zz_full] ADD CONSTRAINT [zz_full_uq] UNIQUE NONCLUSTERED ([a], [b]);")
        assert introspection.object_detail(limited, [*table, seg("folder", "foreign_keys"), seg(
            "foreign_key", introspection.list_children(limited, [*table, seg("folder", "foreign_keys")])[0].name)]).ddl.startswith(
            "ALTER TABLE [dbo].[zz_full] ADD FOREIGN KEY ([parent_id]) REFERENCES [dbo].[zz_parent] ([id])")
        # and nothing anywhere says None: walk every table, index and foreign key the account reaches
        listed_tables = introspection.list_children(limited, [seg("database", scratch), seg("schema", "dbo"), seg("folder", "tables")])
        assert {"zz_parent", "zz_full", "zz_plain", "zz_child"} <= set(names(listed_tables))  # none has gone missing
        tables = [n.path for n in listed_tables]
        for path in tables:
            for found in (introspection.object_detail(limited, path), *(
                    introspection.object_detail(limited, leaf.path)
                    for folder in introspection.list_children(limited, path) if folder.name in ("indexes", "foreign_keys")
                    for leaf in introspection.list_children(limited, folder.path))):
                assert found.ddl is None or "None" not in found.ddl
        snapshot = mssql_schema.build_snapshot(limited)
        full = rel(snapshot, "dbo", "zz_full")
        assert [(c.name, c.type_name) for c in full.columns][:2] == [("id", "int"), ("email", "nvarchar(200)")]
        assert [c.name for c in full.columns] == FULL_COLUMNS and full.primary_key == ["id"]
        assert [f.name for f in full.foreign_keys] and len(rel(snapshot, "dbo", "zz_child").columns) == 2


def test_a_foreign_key_to_a_table_the_account_cannot_see(scratch):
    """The account may see the child table only: OBJECT_NAME of the referenced table is NULL (R-03b-T7b). The tree
    shows `?`, the DDL is withheld, and the snapshot (which completion depends on) still builds, without that key."""
    with scratch_connection(scratch, "zz_child_only") as limited:
        child = scratch_table(scratch, "zz_child")
        assert names(introspection.list_children(limited, [seg("database", scratch), seg("schema", "dbo"), seg("folder", "tables")])) == ["zz_child"]
        assert names(introspection.list_children(limited, child)) == ["columns", "indexes", "foreign_keys"]
        keys = introspection.list_children(limited, [*child, seg("folder", "foreign_keys")])
        assert [(n.kind, n.detail) for n in keys] == [("foreign_key", "(parent_id) → ?.? (?)")]
        table = introspection.object_detail(limited, child)
        assert table.ddl is None and [c.name for c in table.columns] == ["id", "parent_id"]
        leaf = introspection.object_detail(limited, keys[0].path)
        assert leaf.ddl is None and not leaf.ddl_is_generated
        snapshot = mssql_schema.build_snapshot(limited)
        relation = rel(snapshot, "dbo", "zz_child")
        assert relation.foreign_keys == [] and relation.primary_key == ["id"] and [c.name for c in relation.columns] == ["id", "parent_id"]
    with scratch_connection(scratch) as owner:  # the owner sees the whole key
        keys = introspection.list_children(owner, [*child, seg("folder", "foreign_keys")])
        assert keys[0].detail == "(parent_id) → dbo.zz_parent (id)"
        assert introspection.object_detail(owner, child).ddl_is_generated


def test_the_ddl_of_the_dev_database_runs_back_to_the_same_ddl(conn, scratch):
    """Every kind of generated DDL (tables with awkward names, an alias and a table type, a sequence, a synonym) runs
    unchanged in an empty database, and the database then writes the same DDL back."""
    sources = [
        ([*DBO, seg("folder", "types"), seg("type", "email_address")]),
        ([*DBO, seg("folder", "types"), seg("type", "id_list")]),
        *[[*DBO, seg("folder", "tables"), seg("table", t)] for t in (
            "customers", "types_demo", "big_table", "Order Items", "brack]et", "客戶備註", "group", "variant_cases", "job_log")],
        ORDERS,  # after customers: its foreign key
        [*SALES, seg("folder", "sequences"), seg("sequence", "order_seq")],
        [*DBO, seg("folder", "synonyms"), seg("synonym", "customer_alias")],  # after customers: what it points at
    ]
    generated = [(path, introspection.object_detail(conn, path)) for path in sources]
    assert all(detail.ddl and detail.ddl_is_generated for _, detail in generated)
    with scratch_connection(scratch) as owner:
        for _, detail in generated:
            owner.execute_non_query(detail.ddl)  # raises on any error
        for path, detail in generated:
            again = introspection.object_detail(owner, [seg("database", scratch), *path[1:]])
            assert again.ddl == detail.ddl, path[-1].name
            if detail.columns:
                assert [(c.name, c.type_name, c.nullable) for c in again.columns] == [
                    (c.name, c.type_name, c.nullable) for c in detail.columns]
        assert mssql_schema.build_snapshot(owner).database == scratch
