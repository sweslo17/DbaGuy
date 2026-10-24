"""What a SQL Server dump writes and in which order (spec ⑥ §3.3.3): modules by their dependencies with their own
QUOTED_IDENTIFIER, a function a table needs before it, sequences at their next value (A S11), identity counters (S8,
pre-flight M8), left-out tables (S9) and what else is not written (H6); SNAPSHOT where the database allows it (A4) and
the honest per-table read where it does not (A S9). A `drop_first` script built from each catalog in the order Task 8's
DumpScript writes it runs twice into a scratch database through Run SQL file and reads back the same (H7), compared
through an independent sys.* inventory and a hash of every row (tests/mssql_inventory.py). Works in scratch databases
`zz_06_dumpcat_*` and `zz_06_restore_*` (sa makes them; the dev login owns them); the seed is only read."""

import pytest
from pymssql import _mssql

from dbaguy_backend.adapters.base import StatementError
from dbaguy_backend.adapters.mssql.dump import (
    PER_TABLE, PER_TABLE_WARNING, SNAPSHOT, _module_statements, _reseed, as_named, next_id, unwrapped,
)
from dbaguy_backend.adapters.mssql.introspection import _next_value
from dbaguy_backend.domain.data_jobs import DumpObjectKinds, DumpScope
from dbaguy_backend.sql.dump_script import drop_warnings
from tests.data_support import SCRATCH_06B, DataApp, scratch_database
from tests.edit_support import scratch_db
from tests.mssql_inventory import catalog_in, copy_rows, data, differences, inventory, restore, sa_rows, script
from tests.support import requires_mssql

# ---- pure ------------------------------------------------------------------------------------------------------------


def test_a_reseed_gives_the_source_s_next_id_and_never_moves_a_counter_back():  # S8, M8
    assert _reseed("[app].[t]", "4", "1") == (
        "IF (SELECT last_value FROM sys.identity_columns WHERE object_id = OBJECT_ID(N'[app].[t]')) IS NULL\n"
        "    DBCC CHECKIDENT (N'[app].[t]', RESEED, 4) WITH NO_INFOMSGS\n"
        "ELSE IF IDENT_CURRENT(N'[app].[t]') < 3\n"
        "    DBCC CHECKIDENT (N'[app].[t]', RESEED, 3) WITH NO_INFOMSGS")
    going_down = _reseed("[a]].b].[t]", "7", "-1")
    assert "RESEED, 7)" in going_down and "IDENT_CURRENT(N'[a]].b].[t]') > 8" in going_down
    assert "RESEED, 99999999999999999999999999999999999998)" in _reseed(  # decimal(38, 0): exact
        "[t]", "99999999999999999999999999999999999999", "1")


@pytest.mark.parametrize("last, increment, seed, current, expected", [
    ("3", "1", "1", "3", "4"),  # after its last value (the rows after it deleted: M8)
    ("8", "-1", "10", "8", "7"),
    (None, "1", "1", "1", None),  # still at its seed: nothing to write
    (None, "1", "1", "40", "40"),  # reseeded, no row since: IDENT_CURRENT is the next id
    ("99999999999999999999999999999999999998", "1", "1", "99999999999999999999999999999999999998",
     "99999999999999999999999999999999999999"),
])
def test_a_table_s_next_id(last, increment, seed, current, expected):
    assert next_id(last, increment, seed, current) == expected


@pytest.mark.parametrize("fields, expected", [
    (("10", "5", "-2147483648", "2147483647", False, "10", False), ("10", False)),  # never given: its start
    (("10", "5", "-2147483648", "2147483647", False, "25", True), ("30", False)),
    (("1", "1", "1", "3", True, "3", True), ("1", False)),  # CYCLE: starts over at MINVALUE
    (("-1", "-1", "-3", "-1", True, "-3", True), ("-1", False)),  # going down: at MAXVALUE
    (("250", "5", "0", "255", False, "255", True), ("255", True)),  # NO CYCLE, none left
    (("1", "1", "1", "99999999999999999999999999999999999999", False, "99999999999999999999999999999999999998",
      True), ("99999999999999999999999999999999999999", False)),  # decimal(38, 0): exact
])
def test_a_sequence_starts_at_the_value_it_gives_next(fields, expected):  # A S11
    assert _next_value(*fields) == expected


@pytest.mark.parametrize("definition, parent, expected", [
    ("CREATE VIEW app.v AS SELECT 1 AS a", None, ("CREATE VIEW app.v AS SELECT 1 AS a", False)),
    ("CREATE VIEW [APP].[V] AS SELECT 1 AS a", None, ("CREATE VIEW [APP].[V] AS SELECT 1 AS a", False)),
    ("CREATE VIEW v AS SELECT 1 AS a", None, ("CREATE VIEW [app].[v] AS SELECT 1 AS a", False)),  # qualified
    ("-- made by hand\n/* old */ CREATE   PROC dbo.old_name AS SELECT 1", None,
     ("-- made by hand\n/* old */ CREATE   PROC [app].[v] AS SELECT 1", True)),  # sp_rename: L13
    ("CREATE OR ALTER FUNCTION app . v () RETURNS int AS BEGIN RETURN 1 END", None,
     ("CREATE OR ALTER FUNCTION app . v () RETURNS int AS BEGIN RETURN 1 END", False)),
    ("CREATE TRIGGER app.v ON app.was AFTER INSERT AS SET NOCOUNT ON", ("app", "now"),
     ("CREATE TRIGGER app.v ON [app].[now] AFTER INSERT AS SET NOCOUNT ON", True)),  # the table renamed
    ("CREATE TRIGGER v ON t INSTEAD OF INSERT AS SELECT 1", ("app", "t"),
     ("CREATE TRIGGER [app].[v] ON [app].[t] INSTEAD OF INSERT AS SELECT 1", False)),
    ("SELECT 1", None, ("SELECT 1", False)),  # not a module's head: as it is
])
def test_a_module_is_written_under_its_name_now(definition, parent, expected):
    assert as_named(definition, "app", "v", parent) == expected


@pytest.mark.parametrize("definition, expected", [
    ("([s].[f]([id]))", "[s].[f]([id])"),
    ("(  [s].[f]([a], (1)) )", "[s].[f]([a], (1))"),
    ("([s].[f]([a]))+([s].[g]([b]))", "([s].[f]([a]))+([s].[g]([b]))"),  # not one pair: as it is
    ("[s].[f]([id])", "[s].[f]([id])"),
    ("([s].[f](')'))", "[s].[f](')')"),  # a parenthesis in a string is no parenthesis
])
def test_a_policy_predicate_loses_the_parentheses_the_catalog_adds(definition, expected):
    assert unwrapped(definition) == expected


def test_a_module_keeps_the_settings_it_was_created_with():
    assert _module_statements(" CREATE VIEW v AS SELECT 1 \n", True, True) == (" CREATE VIEW v AS SELECT 1 \n",)
    assert _module_statements("CREATE PROCEDURE p AS SELECT 1", False, False) == (
        "SET QUOTED_IDENTIFIER OFF", "SET ANSI_NULLS OFF", "CREATE PROCEDURE p AS SELECT 1", "SET QUOTED_IDENTIFIER ON",
        "SET ANSI_NULLS ON")


# ---- live ------------------------------------------------------------------------------------------------------------


@pytest.fixture
def database(tmp_path):
    db = scratch_db("mssql", tmp_path)
    with scratch_database("mssql", "dumpcat") as (name, admin):
        admin.run_in(name, f"ALTER DATABASE [{name}] SET ALLOW_SNAPSHOT_ISOLATION ON")
        for batch in (
            "CREATE SCHEMA app",
            "CREATE TYPE app.code FROM varchar(10) NOT NULL",
            "CREATE TYPE app.ids AS TABLE (id int PRIMARY KEY)",
            "CREATE SEQUENCE app.tickets AS int START WITH 10 INCREMENT BY 5",
            "CREATE FUNCTION app.double_it (@v int) RETURNS int AS BEGIN RETURN @v * 2 END",
            "CREATE TABLE app.parent (id int IDENTITY(1, 1) PRIMARY KEY, code app.code, "
            "ticket int DEFAULT NEXT VALUE FOR app.tickets, twice AS app.double_it(id))",
            "CREATE TABLE app.child (id int PRIMARY KEY, parent_id int REFERENCES app.parent (id), note nvarchar(50))",
            "CREATE INDEX child_note ON app.child (note)",
            "INSERT INTO app.parent (code) VALUES ('a'), ('b'), ('c')",
            "DELETE FROM app.parent WHERE id = 3",
            "SELECT NEXT VALUE FOR app.tickets",
            "CREATE FUNCTION app.parents_of (@id int) RETURNS TABLE AS RETURN SELECT id FROM app.parent WHERE id = @id",
            "CREATE VIEW app.v_base AS SELECT id FROM app.parent",
            "CREATE VIEW app.a_top AS SELECT id FROM app.v_base",
            "CREATE PROCEDURE app.later AS SELECT * FROM app.a_top",
            "CREATE SYNONYM app.p FOR app.parent",
            "CREATE TRIGGER app.child_ai ON app.child AFTER INSERT AS SET NOCOUNT ON",
            "DISABLE TRIGGER app.child_ai ON app.child",
            "CREATE TRIGGER app.v_base_io ON app.v_base INSTEAD OF INSERT AS SET NOCOUNT ON",  # H6: a view's trigger
            "CREATE TABLE app.versioned (id int PRIMARY KEY, a datetime2 GENERATED ALWAYS AS ROW START, "
            "b datetime2 GENERATED ALWAYS AS ROW END, PERIOD FOR SYSTEM_TIME (a, b)) "
            "WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = app.versioned_history))",
        ):
            admin.run_in(name, batch)
        admin.run_in(name, "SET QUOTED_IDENTIFIER OFF")
        admin.run_in(name, 'SET QUOTED_IDENTIFIER OFF; EXEC(\'CREATE VIEW app.quoted AS SELECT "text" AS t\')')
        yield db, name, admin
    db.close()


def kinds(objects) -> list[tuple[str, str]]:
    return [(item.kind, item.name) for item in objects]


@requires_mssql
def test_a_sql_server_dump_orders_its_objects(database):
    db, name, _ = database
    conn = db.open(database=name)
    try:
        session = conn.dump_session(DumpScope(kind="database"))
        found = conn.dump_catalog(DumpScope(kind="database"), DumpObjectKinds())
    finally:
        conn.close()
    assert (session.setup, session.consistency) == ((
        "SET IMPLICIT_TRANSACTIONS OFF", "SET TRANSACTION ISOLATION LEVEL SNAPSHOT", "BEGIN TRANSACTION"), "snapshot")
    assert session == SNAPSHOT
    assert kinds(found.pre) == [("schema", "app"), ("type", "app.code"), ("type", "app.ids"), ("sequence", "app.tickets"),
                                ("function", "app.double_it"), ("table", "app.parent"), ("table", "app.child")]
    assert kinds(found.post) == [
        ("identity value", "app.parent"), ("index", "app.child"), ("foreign key", "app.child"),
        ("function", "app.parents_of"), ("view", "app.v_base"), ("view", "app.a_top"), ("synonym", "app.p"),
        ("view", "app.quoted"), ("procedure", "app.later"), ("trigger", "app.child_ai"), ("trigger", "app.v_base_io")]
    statements = {(item.kind, item.name): item.statements for item in (*found.pre, *found.post)}
    drops = {(item.kind, item.name): item.drop for item in (*found.pre, *found.post)}
    assert "START WITH 30 " in statements[("sequence", "app.tickets")][0]  # A S11: 10, 15, 20 (the rows), 25 used
    assert statements[("identity value", "app.parent")] == (  # past the deleted row 3 (S8, M8)
        "IF (SELECT last_value FROM sys.identity_columns WHERE object_id = OBJECT_ID(N'[app].[parent]')) IS NULL\n"
        "    DBCC CHECKIDENT (N'[app].[parent]', RESEED, 4) WITH NO_INFOMSGS\n"
        "ELSE IF IDENT_CURRENT(N'[app].[parent]') < 3\n"
        "    DBCC CHECKIDENT (N'[app].[parent]', RESEED, 3) WITH NO_INFOMSGS",)
    assert "REFERENCES" not in statements[("table", "app.child")][0]
    [key] = statements[("foreign key", "app.child")]
    assert key.startswith("ALTER TABLE [app].[child] WITH CHECK ADD CONSTRAINT [FK__child__parent") and "FOREIGN KEY" in key
    fk_name = key.split("[")[3].split("]")[0]  # the server's own name, kept so that `drop_first` drops it (H7)
    assert drops[("foreign key", "app.child")] == (
        f"IF OBJECT_ID(N'[app].[{fk_name}]', N'F') IS NOT NULL ALTER TABLE [app].[child] DROP CONSTRAINT [{fk_name}]")
    assert statements[("view", "app.quoted")] == (
        "SET QUOTED_IDENTIFIER OFF", 'CREATE VIEW app.quoted AS SELECT "text" AS t', "SET QUOTED_IDENTIFIER ON")
    assert statements[("trigger", "app.child_ai")][-1] == "DISABLE TRIGGER [app].[child_ai] ON [app].[child]"
    assert statements[("trigger", "app.v_base_io")] == (
        "CREATE TRIGGER app.v_base_io ON app.v_base INSTEAD OF INSERT AS SET NOCOUNT ON",)
    assert [table.name for table in found.tables] == ["app.parent", "app.child"]
    assert found.tables[1].references == ("app.parent",)  # M9
    assert "2 system-versioned, graph or ledger tables" in found.not_included  # and its history table
    assert found.warnings == tuple(f"app.{table} is left out: system-versioned, graph and ledger tables are not dumped"
                                   for table in ("versioned", "versioned_history"))
    assert [column.name for column in found.tables[0].columns if column.copied] == ["id", "code", "ticket"]
    assert (found.consistency, found.snapshot_text) == ("snapshot", "one consistent snapshot (SNAPSHOT isolation)")


@requires_mssql
def test_a_snapshot_dump_does_not_see_later_rows(database):  # A4
    db, name, admin = database
    conn = db.open(database=name)
    try:
        for statement in conn.dump_session(DumpScope(kind="schema", schema_name="app")).setup:
            conn.run(statement).close()
        admin.run_in(name, "INSERT INTO app.parent (code) VALUES ('before')")  # after BEGIN: still seen (S13)
        found = conn.dump_catalog(DumpScope(kind="schema", schema_name="app"), DumpObjectKinds())  # the first read
        admin.run_in(name, "INSERT INTO app.parent (code) VALUES ('later')")  # SNAPSHOT starts at the first read
        result = conn.run("SELECT COUNT(*) FROM app.parent")
        assert result.fetch(10) == [[3]]
        result.close()
        conn.rollback()
    finally:
        conn.close()
    assert [table.name for table in found.tables] == ["app.parent", "app.child"]


@requires_mssql
def test_without_snapshot_isolation_each_table_is_read_in_its_turn(database):
    db, name, admin = database
    admin.run_in("master", f"ALTER DATABASE [{name}] SET ALLOW_SNAPSHOT_ISOLATION OFF")
    conn = db.open(database=name)
    try:
        session = conn.dump_session(DumpScope(kind="schema", schema_name="app"))
        found = conn.dump_catalog(DumpScope(kind="tables", schema_name="app", names=["child"]), DumpObjectKinds())
    finally:
        conn.close()
    assert (session.setup, session.consistency, found.consistency) == (
        ("SET IMPLICIT_TRANSACTIONS OFF",), "per_table", "per_table")  # L8
    assert session == PER_TABLE and found.warnings[0] == PER_TABLE_WARNING
    assert found.snapshot_text == "not one consistent snapshot: each table was read in its turn (READ COMMITTED)"
    assert "A foreign key of app.child is left out: it references app.parent, which is not in this dump" in found.warnings
    assert kinds(found.post) == [("index", "app.child"), ("trigger", "app.child_ai")]
    assert kinds(found.pre) == [("schema", "app"), ("table", "app.child")]  # no type, sequence or function it does not use


@requires_mssql
def test_a_plan_lists_what_the_dump_writes(database):
    _, name, _ = database
    for scope in (DumpScope(kind="database"), DumpScope(kind="tables", schema_name="app", names=["parent", "a_top"])):
        dumped = catalog_in(name, scope)
        planned = catalog_in(name, scope, ddl=False, setup=False)
        assert kinds(planned.pre) == kinds(dumped.pre) and kinds(planned.post) == kinds(dumped.post)
        assert [table.name for table in planned.tables] == [table.name for table in dumped.tables]
        assert all(not item.statements for item in (*planned.pre, *planned.post))
        assert planned.not_included == dumped.not_included
    # a tables scope brings what its tables and views need: the type, sequence and function the table uses
    assert kinds(dumped.pre) == [("schema", "app"), ("type", "app.code"), ("sequence", "app.tickets"),
                                 ("function", "app.double_it"), ("table", "app.parent")]
    assert kinds(dumped.post) == [("identity value", "app.parent"), ("view", "app.a_top")]
    assert "Not in this dump, and needed where it is run: app.a_top needs app.v_base" in dumped.warnings


@requires_mssql
def test_what_a_dump_does_not_choose_is_counted_and_what_it_writes_brings_what_it_calls(database):  # H6
    _, name, admin = database
    admin.run_in(name, "CREATE FUNCTION app.stamp () RETURNS int AS BEGIN RETURN 1 END",
                 "CREATE TRIGGER app.parent_ai ON app.parent AFTER INSERT AS DECLARE @x int = app.stamp()",
                 "CREATE TRIGGER app.versioned_ai ON app.versioned AFTER INSERT AS SET NOCOUNT ON")
    found = catalog_in(name, DumpScope(kind="tables", schema_name="app", names=["parent"]))
    assert kinds(found.post) == [("identity value", "app.parent"), ("function", "app.stamp"),
                                 ("trigger", "app.parent_ai")]  # the function its trigger calls comes with it
    everything = DumpScope(kind="database")
    counted = catalog_in(name, everything).not_included
    assert "1 triggers of tables and views not in the dump" in counted  # the left-out table's
    counted = catalog_in(name, everything, DumpObjectKinds(triggers=False)).not_included
    assert {"3 triggers (not chosen)", "1 triggers of tables and views not in the dump"} <= set(counted)
    no_views = catalog_in(name, everything, DumpObjectKinds(views=False))
    assert {"3 views (not chosen)", "1 synonyms (not chosen)", "2 triggers of tables and views not in the dump"} <= set(
        no_views.not_included)  # the view's INSTEAD OF trigger goes with it
    assert ("procedure", "app.later") in kinds(no_views.post)  # deferred name resolution: it needs no view to exist
    no_routines = catalog_in(name, everything, DumpObjectKinds(routines=False, types=False))
    assert {"2 functions and procedures (not chosen)", "1 types (not chosen)"} <= set(no_routines.not_included)
    assert [kind for kind in kinds(no_routines.pre + no_routines.post) if kind[0] in ("function", "type")] == [
        ("type", "app.code"), ("function", "app.double_it"), ("function", "app.stamp")]  # what the dump needs


@requires_mssql
def test_what_the_login_cannot_read_is_left_out_and_counted(tmp_path):
    """Without VIEW DEFINITION a computed column's expression and a view's text are hidden, and an encrypted module's
    from everyone: no CREATE that quietly differs, the table is left out with its triggers, and the header says so."""
    db = scratch_db("mssql", tmp_path)
    with scratch_database("mssql", "dumpcat") as (name, admin):
        admin.run_in(name, "CREATE TABLE dbo.plain (id int PRIMARY KEY)",
                     "CREATE TABLE dbo.made (id int PRIMARY KEY, twice AS id * 2)",
                     "CREATE TRIGGER dbo.made_ai ON dbo.made AFTER INSERT AS SET NOCOUNT ON",
                     "CREATE VIEW dbo.v AS SELECT id FROM dbo.plain",
                     "CREATE PROCEDURE dbo.secret WITH ENCRYPTION AS SELECT 1 AS one",
                     "CREATE USER zz_06_reader WITHOUT LOGIN", "ALTER ROLE db_datareader ADD MEMBER zz_06_reader")
        owner = catalog_in(name, DumpScope(kind="database"))
        assert "1 modules whose definition cannot be read (encrypted, or no VIEW DEFINITION)" in owner.not_included
        assert ("procedure", "dbo.secret") not in kinds(owner.post)
        conn = db.open(database=name)
        try:
            conn.run("EXECUTE AS USER = N'zz_06_reader'").close()
            limited = conn.dump_catalog(DumpScope(kind="database"), DumpObjectKinds())
            conn.run("REVERT").close()
        finally:
            conn.close()
    db.close()
    assert [table.name for table in limited.tables] == ["dbo.plain"]
    assert kinds(limited.pre) == [("table", "dbo.plain")] and ("trigger", "dbo.made_ai") not in kinds(limited.post)
    assert ("The definition of dbo.made cannot be read (hidden from this login, or a default bound with "
            "sp_bindefault): the table is left out") in limited.warnings
    assert "1 tables whose definition cannot be read" in limited.not_included
    # the view's and the trigger's text; the procedure it may not execute it does not see at all (what a login cannot
    # see is neither written nor counted: a known limit)
    assert "2 modules whose definition cannot be read (encrypted, or no VIEW DEFINITION)" in limited.not_included
    assert "Not written, their definition cannot be read (encrypted, or no VIEW DEFINITION): dbo.made_ai, dbo.v" in (
        limited.warnings)


# ---- every shape, restored twice ----------------------------------------------------------------------------------------

_SHAPES = (
    "CREATE SCHEMA s",
    "CREATE TYPE s.code FROM varchar(10) NULL",
    "CREATE TYPE s.pairs AS TABLE (a int NOT NULL PRIMARY KEY, b s.code, CHECK (a > 0))",
    "CREATE SEQUENCE s.up AS int START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 3 CYCLE",
    "CREATE SEQUENCE s.done AS tinyint START WITH 250 INCREMENT BY 5 MAXVALUE 255",
    "CREATE SEQUENCE s.fresh START WITH 7",
    "CREATE FUNCTION s.twice (@v int) RETURNS int WITH SCHEMABINDING AS BEGIN RETURN @v * 2 END",
    # ok() calls positive(), made after it: a CHECK runs both while the rows go in, so both come before the tables
    "CREATE FUNCTION s.ok (@v int) RETURNS bit AS BEGIN RETURN CASE WHEN s.positive(@v) = 1 THEN 1 ELSE 0 END END",
    "CREATE FUNCTION s.positive (@v int) RETURNS bit AS BEGIN RETURN CASE WHEN @v > 0 THEN 1 ELSE 0 END END",
    # the child before its parent, its key added later (H7)
    "CREATE TABLE s.kid (id int PRIMARY KEY, mom_id int, n int CHECK (s.ok(n) = 1), dbl AS s.twice(n) PERSISTED, "
    "tag s.code)",
    "CREATE TABLE s.mom (id int IDENTITY(10, -1) PRIMARY KEY, "
    "label nvarchar(20) MASKED WITH (FUNCTION = 'partial(1, \"XX\", 0)') NULL, "
    "g uniqueidentifier ROWGUIDCOL NOT NULL DEFAULT NEWID(), ticket int DEFAULT NEXT VALUE FOR s.up)",
    "ALTER TABLE s.kid ADD CONSTRAINT kid_mom FOREIGN KEY (mom_id) REFERENCES s.mom (id)",
    "CREATE UNIQUE INDEX kid_tag ON s.kid (tag) WHERE tag IS NOT NULL",
    "CREATE UNIQUE INDEX kid_ignore ON s.kid (n, id) WITH (IGNORE_DUP_KEY = ON)",
    "CREATE STATISTICS kid_n ON s.kid (n)",
    "CREATE TABLE s.toy (id int PRIMARY KEY, kid_id int REFERENCES s.kid (id), mom_id int)",
    "CREATE TABLE s.gone (id int IDENTITY(1, 1) PRIMARY KEY, v int)",
    "CREATE TABLE s.wide (id int PRIMARY KEY, a int SPARSE NULL, b nvarchar(20) SPARSE NULL, "
    "cs xml COLUMN_SET FOR ALL_SPARSE_COLUMNS)",
    "CREATE VIEW s.moms AS SELECT id, label FROM s.mom",
    "CREATE TRIGGER s.moms_io ON s.moms INSTEAD OF INSERT AS SET NOCOUNT ON",
    "CREATE TRIGGER s.kid_a ON s.kid AFTER INSERT AS SET NOCOUNT ON",
    "CREATE TRIGGER s.kid_b ON s.kid AFTER INSERT, UPDATE AS SET NOCOUNT ON",
    "EXEC sp_settriggerorder @triggername = N's.kid_b', @order = N'First', @stmttype = N'INSERT'",
    "EXEC sp_settriggerorder @triggername = N's.kid_a', @order = N'Last', @stmttype = N'INSERT'",
    "CREATE PROCEDURE s.old_name AS SELECT 1 AS one",
    "EXEC sp_rename N's.old_name', N'new_name'",  # its definition keeps the old name (L13)
    "CREATE TABLE s.was (id int PRIMARY KEY)",
    "CREATE TRIGGER s.was_ai ON s.was AFTER INSERT AS SET NOCOUNT ON",
    "EXEC sp_rename N's.was', N'now'",  # the trigger's text still says ON s.was
    "CREATE NONCLUSTERED COLUMNSTORE INDEX now_cs ON s.now (id)",
    "CREATE PROCEDURE s.take @p s.pairs READONLY AS SELECT COUNT(*) AS n FROM @p",
    "CREATE FUNCTION s.only_small (@id int) RETURNS TABLE WITH SCHEMABINDING AS RETURN SELECT 1 AS ok WHERE @id < 1000",
    "CREATE PARTITION FUNCTION pf (int) AS RANGE LEFT FOR VALUES (10)",
    "CREATE PARTITION SCHEME ps AS PARTITION pf ALL TO ([PRIMARY])",
    "CREATE TABLE s.parts (id int PRIMARY KEY) ON ps (id)",
    "CREATE TABLE s.person (id int) AS NODE",
    "CREATE TABLE s.ledgered (id int) WITH (LEDGER = ON (APPEND_ONLY = ON))",
    "CREATE TABLE s.period (id int PRIMARY KEY, a datetime2 GENERATED ALWAYS AS ROW START NOT NULL, "
    "b datetime2 GENERATED ALWAYS AS ROW END NOT NULL, PERIOD FOR SYSTEM_TIME (a, b))",
    "EXEC sp_addextendedproperty N'MS_Description', N'the moms', N'SCHEMA', N's', N'TABLE', N'mom'",
    "CREATE TRIGGER on_ddl ON DATABASE FOR CREATE_TABLE AS SET NOCOUNT ON",
    "DISABLE TRIGGER on_ddl ON DATABASE",
    # rows
    "INSERT INTO s.mom (label) VALUES (N'Ann'), (N'Bea')",  # 10, 9; tickets 1, 2
    "SELECT NEXT VALUE FOR s.up",  # 3: the next is 1 again (CYCLE)
    "SELECT NEXT VALUE FOR s.done", "SELECT NEXT VALUE FOR s.done",  # 250, 255: none left
    "INSERT INTO s.kid (id, mom_id, n, tag) VALUES (1, 10, 5, 'x'), (2, 9, 6, NULL), (3, 9, 7, NULL)",
    "INSERT INTO s.toy (id, kid_id, mom_id) VALUES (2, 1, 10), (500, 2, 9), (501, 1, 777)",
    # what the rows break, kept as the server has it: untrusted, disabled (L13)
    "ALTER TABLE s.toy WITH NOCHECK ADD CONSTRAINT toy_mom FOREIGN KEY (mom_id) REFERENCES s.mom (id)",
    "ALTER TABLE s.toy NOCHECK CONSTRAINT toy_mom",
    "ALTER TABLE s.toy WITH NOCHECK ADD CONSTRAINT toy_small CHECK (id < 100)",
    "ALTER TABLE s.toy WITH NOCHECK ADD CONSTRAINT toy_even CHECK (id % 2 = 0)",
    "ALTER TABLE s.toy NOCHECK CONSTRAINT toy_even",
    "CREATE SECURITY POLICY s.small ADD FILTER PREDICATE s.only_small(id) ON s.toy, "
    "ADD BLOCK PREDICATE s.only_small(id) ON s.toy AFTER INSERT WITH (STATE = ON)",
    "INSERT INTO s.gone (v) VALUES (1), (2), (3)", "DELETE FROM s.gone",  # emptied: its next id is 4 (M8)
    "INSERT INTO s.wide (id, a, b) VALUES (1, 5, N'x'), (2, NULL, NULL)",
    "INSERT INTO s.now (id) VALUES (1)", "INSERT INTO s.parts (id) VALUES (5), (50)",
)
_LEFT_OUT = ("person", "ledgered", "period", "now_cs")  # inventory rows a dump does not bring back (counted)
_RENAMED = {("s", "new_name"), ("s", "was_ai")}  # their text: the source's still has the old names


def _kept(found: dict) -> dict:
    return {part: [row for row in found[part] if not any(name in repr(row) for name in _LEFT_OUT)
                   and not (part == "modules" and row[:2] in _RENAMED)] for part in found}


def _next_ids(database: str) -> list:
    """The id the next row of each identity table gets and the next value of two sequences: taken as sa (scratch
    databases only)."""
    assert database.startswith("zz_06_")
    return sa_rows(database, "INSERT INTO s.gone (v) VALUES (9); SELECT SCOPE_IDENTITY();"
                             "INSERT INTO s.mom (label) VALUES (N'Cy'); SELECT SCOPE_IDENTITY();"
                             "SELECT NEXT VALUE FOR s.up; SELECT NEXT VALUE FOR s.fresh")


@requires_mssql
def test_a_drop_first_dump_of_every_shape_restores_twice_and_reads_back_the_same(tmp_path):
    db = scratch_db("mssql", tmp_path)
    app = DataApp(tmp_path)
    try:
        with scratch_database("mssql", "dumpcat") as (source, admin), \
                scratch_database("mssql", "restore") as (target, _):
            admin.run_in(source, f"ALTER DATABASE [{source}] SET ALLOW_SNAPSHOT_ISOLATION ON")
            for batch in _SHAPES:
                admin.run_in(source, batch)
            found = catalog_in(source, DumpScope(kind="database"))
            listed = kinds(found.pre) + kinds(found.post)
            # the functions the tables call, the one they call too, before the tables; schema-bound after nothing
            assert kinds(found.pre)[:7] == [("schema", "s"), ("type", "s.code"), ("type", "s.pairs"),
                                            ("sequence", "s.up"), ("sequence", "s.done"), ("sequence", "s.fresh"),
                                            ("function", "s.twice")]
            assert listed.index(("function", "s.positive")) < listed.index(("function", "s.ok")) < listed.index(
                ("table", "s.kid"))
            assert [table.name for table in found.tables] == ["s.kid", "s.mom", "s.toy", "s.gone", "s.wide", "s.now",
                                                              "s.parts"]
            statements = {(item.kind, item.name): item.statements for item in (*found.pre, *found.post)}
            assert "START WITH 1 " in statements[("sequence", "s.up")][0]
            assert "START WITH 255 " in statements[("sequence", "s.done")][0]
            assert "START WITH 7 " in statements[("sequence", "s.fresh")][0]
            assert "Sequence s.done has given its last value: the one written gives it once more" in found.warnings
            assert ("check constraint", "s.toy") in listed and "CHECK ([id]<(100))" not in statements[("table", "s.toy")][0]
            assert statements[("trigger", "s.kid_b")][-1] == (
                "EXEC sys.sp_settriggerorder @triggername = N'[s].[kid_b]', @order = N'First', @stmttype = N'INSERT'")
            assert statements[("procedure", "s.new_name")] == ("CREATE PROCEDURE [s].[new_name] AS SELECT 1 AS one",)
            assert "ON [s].[now] AFTER INSERT" in statements[("trigger", "s.was_ai")][0]
            assert {"s.new_name was renamed after it was created (sp_rename): its definition is written under the "
                    "names it has now", "s.was_ai was renamed after it was created (sp_rename): its definition is "
                    "written under the names it has now"} <= set(found.warnings)
            assert ("Row-level security (s.small) filters s.toy: the dump holds only the rows this login may see"
                    in found.warnings)
            assert [c.name for c in found.tables[4].columns if c.copied] == ["id", "a", "b"]  # not the column set
            for left_out in ("s.person", "s.ledgered"):
                assert f"{left_out} is left out: system-versioned, graph and ledger tables are not dumped" in found.warnings
            assert "s.period is left out: its GENERATED ALWAYS columns (a period) cannot be written back" in found.warnings
            assert not any("ledgered_Ledger" in item.name for item in (*found.pre, *found.post))
            for counted in ("2 system-versioned, graph or ledger tables", "1 tables with a period (GENERATED ALWAYS "
                            "columns)", "1 indexes DbaGuy does not write (columnstore, XML, spatial, hash)",
                            "1 indexes and heaps whose options are not written (IGNORE_DUP_KEY, fill factor, "
                            "compression, lock options, disabled)", "1 partitioned tables (they come back "
                            "unpartitioned: partition schemes and filegroups are not written)",
                            "1 statistics made with CREATE STATISTICS", "1 extended properties (descriptions)",
                            "1 database triggers"):
                assert counted in found.not_included, (counted, found.not_included)
            assert found.not_included[-1].startswith("the database collation (")
            path = tmp_path / "shapes.sql"
            path.write_text(script(found, copy_rows(found, source)), encoding="utf-8")
            restore(app, db.profile(database=target), path)  # all or nothing, twice
            before, after = inventory(source), inventory(target)
            changed = differences(_kept(before), _kept(after))
            # what the dump changes on purpose (besides the renamed modules' text, above): the sequence that had no
            # value left gives its last once more (warned), and the emptied table's counter is the RESEED's, which
            # gives the same next id (M8, below) though sys.identity_columns shows none until a row goes in
            assert changed == {
                "sequences": (["('s', 'done', 'tinyint', '5', '0', '255', False, True, None, None)"],
                              ["('s', 'done', 'tinyint', '5', '0', '255', False, True, None, '255')"]),
                "identity": (["('s.gone', 'id', '3')"], ["('s.gone', 'id', None)"])}, changed
            assert {row[:2]: row[2] for row in after["modules"] if row[:2] in _RENAMED} == {
                ("s", "new_name"): "CREATE PROCEDURE [s].[new_name] AS SELECT 1 AS one",
                ("s", "was_ai"): "CREATE TRIGGER s.was_ai ON [s].[now] AFTER INSERT AS SET NOCOUNT ON"}
            rows_before = {name: rows for name, rows in data(source).items() if name not in ("s.period",)}
            assert rows_before == data(target) and rows_before["s.toy"][0] == 3
            again = catalog_in(target, DumpScope(kind="database"))  # the restore dumps to the same objects
            assert (kinds(again.pre), kinds(again.post)) == (kinds(found.pre), kinds(found.post))
            # M8: the emptied table too; s.up cycled (the s.mom row takes its 1)
            assert _next_ids(source) == _next_ids(target) == [(4,), (8,), (2,), (7,)]
    finally:
        app.close()
        db.close()


# ---- review fix round 1: what needs what is left out, defaults and checks after the views, indexed views ------------

_ROUND1 = (
    "CREATE SCHEMA f",
    # S1: a check whose function reads a table made after it (547 with the rows), one reading a view (208), a default
    # through an inline function over a view
    "CREATE TABLE f.aa_checked (id int PRIMARY KEY, code nvarchar(10))",
    "CREATE TABLE f.lookup (code nvarchar(10) PRIMARY KEY)",
    "CREATE VIEW f.v_lookup AS SELECT code FROM f.lookup",
    "CREATE FUNCTION f.in_lookup (@c nvarchar(10)) RETURNS bit AS BEGIN RETURN CASE WHEN EXISTS "
    "(SELECT 1 FROM f.lookup WHERE code = @c) THEN 1 ELSE 0 END END",
    "CREATE FUNCTION f.in_view (@c nvarchar(10)) RETURNS bit AS BEGIN RETURN CASE WHEN EXISTS "
    "(SELECT 1 FROM f.v_lookup WHERE code = @c) THEN 1 ELSE 0 END END",
    "CREATE FUNCTION f.codes () RETURNS TABLE AS RETURN SELECT code FROM f.v_lookup",
    "CREATE FUNCTION f.how_many () RETURNS int AS BEGIN RETURN (SELECT COUNT(*) FROM f.codes()) END",
    "CREATE TABLE f.checked (id int PRIMARY KEY, code nvarchar(10) CONSTRAINT checked_code CHECK (f.in_view(code) = 1), "
    "n int DEFAULT f.how_many())",
    "INSERT INTO f.lookup VALUES (N'a'), (N'b')", "INSERT INTO f.aa_checked VALUES (1, N'a')",
    "INSERT INTO f.checked (id, code) VALUES (1, N'b')",
    "ALTER TABLE f.aa_checked ADD CONSTRAINT aa_code CHECK (f.in_lookup(code) = 1)",
    # S1: a computed column's schema-bound function binds a schema-bound view: both before the table
    "CREATE TABLE f.rates (k int PRIMARY KEY, r int NOT NULL)",
    "CREATE VIEW f.v_rates WITH SCHEMABINDING AS SELECT k, r FROM f.rates",
    "CREATE FUNCTION f.rate_count () RETURNS int WITH SCHEMABINDING AS BEGIN RETURN (SELECT COUNT_BIG(*) FROM f.v_rates) "
    "END",
    "CREATE TABLE f.uses_rates (id int PRIMARY KEY, c AS f.rate_count())",
    # S3: an indexed view (a view after it reads it WITH (NOEXPAND): its index must be there), a table type's index
    "CREATE TABLE f.sales (id int PRIMARY KEY, region nvarchar(10) NOT NULL, amount money NOT NULL)",
    "CREATE VIEW f.by_region WITH SCHEMABINDING AS SELECT region, SUM(amount) AS total, COUNT_BIG(*) AS n "
    "FROM f.sales GROUP BY region",
    "CREATE UNIQUE CLUSTERED INDEX by_region_cx ON f.by_region (region)",
    "CREATE INDEX by_region_total ON f.by_region (total)",
    "CREATE VIEW f.on_by_region AS SELECT region FROM f.by_region WITH (NOEXPAND)",
    "CREATE TYPE f.tt AS TABLE (a int PRIMARY KEY, b int, INDEX tt_b (b))",
    "CREATE PROCEDURE f.take @p f.tt READONLY AS SELECT COUNT(*) AS n FROM @p",
    # S1: what needs a left-out (system-versioned, graph) or hidden (encrypted) object, transitively
    "CREATE TABLE f.prices (id int PRIMARY KEY, p money, a datetime2 GENERATED ALWAYS AS ROW START, "
    "b datetime2 GENERATED ALWAYS AS ROW END, PERIOD FOR SYSTEM_TIME (a, b)) "
    "WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = f.prices_history))",
    "CREATE VIEW f.current_prices AS SELECT id, p FROM f.prices",
    "CREATE VIEW f.on_current AS SELECT id FROM f.current_prices",
    "CREATE TABLE f.person (id int PRIMARY KEY) AS NODE",
    "CREATE VIEW f.people AS SELECT id FROM f.person",
    "CREATE VIEW f.secret WITH ENCRYPTION AS SELECT id FROM f.sales",
    "CREATE VIEW f.on_secret AS SELECT id FROM f.secret",
    "CREATE FUNCTION f.f_enc (@v int) RETURNS bit WITH ENCRYPTION AS BEGIN RETURN 1 END",
    "CREATE TABLE f.enc_checked (id int PRIMARY KEY, v int CHECK (f.f_enc(v) = 1))",
    "CREATE VIEW f.on_enc_checked AS SELECT id FROM f.enc_checked",
    "CREATE FUNCTION f.price_of (@id int) RETURNS money AS BEGIN RETURN (SELECT p FROM f.prices WHERE id = @id) END",
    "CREATE TRIGGER f.sales_ai ON f.sales AFTER INSERT AS DECLARE @x money = f.price_of(1)",
    "CREATE PROCEDURE f.report AS SELECT id FROM f.current_prices",
    # S2: modules that run as a database user; SELF needs none
    "CREATE USER zz_06_runner WITHOUT LOGIN",
    "CREATE PROCEDURE f.as_runner WITH EXECUTE AS 'zz_06_runner' AS SELECT 1 AS one",
    "CREATE FUNCTION f.f_as_runner () RETURNS int WITH EXECUTE AS 'zz_06_runner' AS BEGIN RETURN 1 END",
    "CREATE USER zz_06_maker WITHOUT LOGIN", "GRANT CREATE PROCEDURE TO zz_06_maker",
    "GRANT ALTER ON SCHEMA::f TO zz_06_maker",
    "INSERT INTO f.sales VALUES (1, N'n', 10), (2, N's', 20)", "INSERT INTO f.rates VALUES (1, 2)",
    "INSERT INTO f.uses_rates (id) VALUES (1)", "INSERT INTO f.enc_checked VALUES (1, 1)",
)
# made by zz_06_maker: its execute_as_principal_id is that user's, as `EXECUTE AS 'zz_06_maker'` would be
_AS_SELF = ("EXECUTE AS USER = N'zz_06_maker'; EXEC (N'CREATE PROCEDURE f.as_self WITH EXECUTE AS SELF AS SELECT 1 AS "
            "one'); REVERT")
_ROUND1_LEFT_OUT = {f"f.{name}" for name in (
    "prices", "prices_history", "current_prices", "on_current", "person", "people", "secret", "on_secret", "f_enc",
    "enc_checked", "on_enc_checked", "sales_ai", "as_runner", "f_as_runner")}


def _without(found: dict, labels: set[str]) -> dict:
    """The inventory without the rows of `labels`' objects (an object's row names it in one of its first fields, or
    as schema and name in two of them)."""
    def names(row: tuple) -> set[str]:
        head = [value for value in row[:4] if isinstance(value, str)]
        return {*head, *(f"{a}.{b}" for a, b in zip(head, head[1:]))}
    return {part: [row for row in found[part] if not names(row) & labels] for part in found}


@requires_mssql
def test_what_needs_a_left_out_object_is_left_out_and_defaults_and_checks_wait_for_the_views(tmp_path):  # review S1–S3
    db = scratch_db("mssql", tmp_path)
    app = DataApp(tmp_path)
    try:
        with scratch_database("mssql", "dumpcat") as (source, admin), \
                scratch_database("mssql", "restore") as (target, _):
            for statement in _ROUND1:
                admin.run_in(source, statement)
            sa_rows(source, _AS_SELF)
            found = catalog_in(source, DumpScope(kind="database"))
            listed = kinds(found.pre) + kinds(found.post)
            assert not {name for _, name in listed} & _ROUND1_LEFT_OUT
            text = " | ".join(found.warnings)
            for need in ("f.current_prices (needs f.prices)", "f.on_current (needs f.current_prices)",
                         "f.people (needs f.person)", "f.on_secret (needs f.secret)",
                         "f.on_enc_checked (needs f.enc_checked)", "f.sales_ai (needs f.price_of)"):
                assert need in text, need
            assert ("Not written, their definition needs what the dump leaves out: f.enc_checked (needs f.f_enc)"
                    in found.warnings)
            assert ("Not written, their definition cannot be read (encrypted, or no VIEW DEFINITION): f.secret, f.f_enc"
                    in found.warnings)
            assert ("Not written, they run as a database user the file does not create (EXECUTE AS): f.as_runner (as "
                    "zz_06_runner), f.f_as_runner (as zz_06_runner)") in found.warnings  # S2
            assert "f.price_of needs f.prices" in text and "f.report needs f.current_prices" in text  # written, named
            for counted in ("1 tables whose definition cannot be read", "2 modules that run as a database user "
                            "(EXECUTE AS 'user')", "6 views, functions, synonyms, triggers and security policies that "
                            "need what the dump leaves out", "2 modules whose definition cannot be read (encrypted, or "
                            "no VIEW DEFINITION)"):
                assert counted in found.not_included, (counted, found.not_included)
            assert ("procedure", "f.as_self") in listed
            # S1: the bound view and function before the table; the defaults and checks that read after everything
            pre = kinds(found.pre)
            assert pre.index(("view", "f.v_rates")) < pre.index(("function", "f.rate_count")) < pre.index(
                ("table", "f.uses_rates"))
            post = kinds(found.post)
            last_module = max(i for i, (kind, _) in enumerate(post) if kind in ("view", "function", "procedure"))
            constraints = [i for i, (kind, _) in enumerate(post) if kind in ("check constraint", "default")]
            assert [post[i] for i in constraints] == [("check constraint", "f.aa_checked"),
                                                      ("check constraint", "f.checked"), ("default", "f.checked")]
            assert min(constraints) > last_module
            statements = {(item.kind, item.name): item.statements for item in (*found.pre, *found.post)}
            assert statements[("check constraint", "f.aa_checked")] == (
                "ALTER TABLE [f].[aa_checked] WITH CHECK ADD CONSTRAINT [aa_code] CHECK ([f].[in_lookup]([code])=(1))",)
            assert statements[("default", "f.checked")][0].startswith("ALTER TABLE [f].[checked] ADD CONSTRAINT [DF__")
            assert "checked_code" not in statements[("table", "f.checked")][0]
            # S3: the indexed view with its indexes, the unique clustered one first; the table type's own index
            by_region = statements[("view", "f.by_region")]
            assert by_region[1:] == ("CREATE UNIQUE CLUSTERED INDEX [by_region_cx] ON [f].[by_region] ([region])",
                                     "CREATE NONCLUSTERED INDEX [by_region_total] ON [f].[by_region] ([total])")
            assert "INDEX [tt_b] NONCLUSTERED ([b])" in statements[("type", "f.tt")][0]
            path = tmp_path / "round1.sql"
            path.write_text(script(found, copy_rows(found, source)), encoding="utf-8")
            restore(app, db.profile(database=target), path)  # all or nothing, twice
            assert differences(_without(inventory(source), _ROUND1_LEFT_OUT), inventory(target)) == {}
            rows_before = {name: rows for name, rows in data(source).items() if name not in _ROUND1_LEFT_OUT}
            assert rows_before == data(target)
            again = catalog_in(target, DumpScope(kind="database"))
            assert (kinds(again.pre), kinds(again.post)) == (kinds(found.pre), kinds(found.post))
    finally:
        app.close()
        db.close()


@requires_mssql
@pytest.mark.parametrize("snapshot", ["ON", "OFF"])
def test_a_session_with_implicit_transactions_dumps_and_leaves_none_open(database, snapshot):  # review S5, S6
    db, name, admin = database
    admin.run_in("master", f"ALTER DATABASE [{name}] SET ALLOW_SNAPSHOT_ISOLATION {snapshot}")
    conn = db.open(database=name)

    def trancount() -> int:
        result = conn.run("SELECT @@TRANCOUNT")
        try:
            return result.fetch(1)[0][0]
        finally:
            result.close()

    try:
        conn.run("SET IMPLICIT_TRANSACTIONS ON").close()  # what a login's user options can give it
        session = conn.dump_session(DumpScope(kind="database"))
        assert trancount() == 0  # dump_session's own read opened nothing
        for statement in session.setup:
            conn.run(statement).close()
        found = conn.dump_catalog(DumpScope(kind="database"), DumpObjectKinds())
        assert (found.consistency, trancount()) == (("snapshot", 1) if snapshot == "ON" else ("per_table", 0))
        if snapshot == "ON":  # S6: another session's DDL fails the snapshot's read (3961) and ends its transaction
            admin.run_in(name, "ALTER TABLE app.child ADD w int NULL")
            with pytest.raises(StatementError):
                conn.run("SELECT COUNT(*) FROM app.child").close()
        for statement in session.teardown:
            conn.run(statement).close()
        assert trancount() == 0
    finally:
        conn.close()


@requires_mssql
def test_a_per_table_dump_never_moves_a_counter_back_past_rows_it_read_later(tmp_path):  # S8, M8
    """Without a snapshot the rows are read after the counter: one inserted between the two is in the file, and the
    RESEED leaves the restored counter past it (RESEED to the counter read first would give its id again)."""
    db = scratch_db("mssql", tmp_path)
    app = DataApp(tmp_path)
    try:
        with scratch_database("mssql", "dumpcat") as (source, admin), \
                scratch_database("mssql", "restore") as (target, _):
            admin.run_in(source, "CREATE TABLE dbo.t (id int IDENTITY(1, 1) PRIMARY KEY, v int)",
                         "INSERT INTO dbo.t (v) VALUES (1), (2)")
            found = catalog_in(source, DumpScope(kind="database"))
            assert found.consistency == "per_table"
            admin.run_in(source, "INSERT INTO dbo.t (v) VALUES (3)")  # after the catalog, before the rows
            path = tmp_path / "t.sql"
            path.write_text(script(found, copy_rows(found, source)), encoding="utf-8")
            restore(app, db.profile(database=target), path, times=1)
            assert sa_rows(target, "INSERT INTO dbo.t (v) VALUES (4); SELECT SCOPE_IDENTITY()") == [(4,)]
    finally:
        app.close()
        db.close()


@requires_mssql
@pytest.mark.parametrize("seed", ["dbaguy", "analytics"])
def test_the_seed_dumps_and_restores_twice(tmp_path, seed):
    """Identifiers that need quoting, every type, an alias and a table type, a sequence, a synonym, a view, functions,
    procedures and a trigger: a `drop_first` script restored twice into a scratch database (statement by statement,
    then all or nothing) reads back the same, row for row (the seed is only read; its sql_variant values are what a
    dump cannot carry)."""
    db = scratch_db("mssql", tmp_path)
    app = DataApp(tmp_path)
    try:
        with scratch_database("mssql", "restore") as (target, _):
            found = catalog_in(seed, DumpScope(kind="database"))
            assert found.consistency == "per_table" and found.warnings[0] == PER_TABLE_WARNING  # A S9
            path = tmp_path / "seed.sql"
            path.write_text(script(found, copy_rows(found, seed)), encoding="utf-8")
            restore(app, db.profile(database=target), path, mode="statements", times=1)
            restore(app, db.profile(database=target), path, mode="all_or_nothing", times=1)
            assert differences(inventory(seed), inventory(target)) == {}
            assert data(seed) == data(target)
            if seed == "dbaguy":
                assert "1 extended properties (descriptions)" in found.not_included
                lost = sa_rows(target, "SELECT COUNT(*) FROM dbo.variant_cases WHERE v IS NOT NULL")
                assert lost == [(0,)]  # sql_variant: not copied (Task 8's header names it)
    finally:
        app.close()
        db.close()


@requires_mssql
def test_a_catalog_read_is_a_fixed_number_of_queries(tmp_path):
    """Q1 (the Task 5 review's lesson): the same number of round trips for 3 tables and for 1,000, each with an
    identity, a sequence default, an index and a foreign key, and 300 rows, views, functions and triggers."""
    with scratch_database("mssql", "dumpcat") as (name, admin):
        admin.run_in(name, "CREATE SCHEMA small", "CREATE SCHEMA big")
        # read to its end (sa_rows): execute_non_query comes back while the loop still runs, and the connection
        # closing after it stops the loop
        sa_rows(name, """
SET NOCOUNT ON;
DECLARE @i int = 1, @n nvarchar(10), @p nvarchar(10), @s sysname, @fk nvarchar(200);
WHILE @i <= 1003 BEGIN
    SET @s = CASE WHEN @i <= 3 THEN N'small' ELSE N'big' END;
    SET @n = RIGHT(N'000' + CAST(@i AS nvarchar(10)), 4);
    SET @p = RIGHT(N'000' + CAST(@i - 1 AS nvarchar(10)), 4);
    SET @fk = CASE WHEN @i NOT IN (1, 4) THEN N' REFERENCES ' + @s + N'.t' + @p + N' (id)' ELSE N'' END;
    EXEC (N'CREATE SEQUENCE ' + @s + N'.q' + @n + N' AS int START WITH 1');
    EXEC (N'CREATE TABLE ' + @s + N'.t' + @n + N' (id int IDENTITY(1, 1) PRIMARY KEY, prev int' + @fk
          + N', label nvarchar(20) DEFAULT N''x'', k int DEFAULT NEXT VALUE FOR ' + @s + N'.q' + @n + N')');
    EXEC (N'CREATE INDEX t' + @n + N'_label ON ' + @s + N'.t' + @n + N' (label)');
    IF @i <= 3 OR @i <= 303 BEGIN
        EXEC (N'CREATE VIEW ' + @s + N'.v' + @n + N' AS SELECT id FROM ' + @s + N'.t' + @n);
        EXEC (N'CREATE FUNCTION ' + @s + N'.f' + @n + N' () RETURNS int AS BEGIN RETURN ' + @n + N' END');
        EXEC (N'CREATE TRIGGER ' + @s + N'.r' + @n + N' ON ' + @s + N'.t' + @n + N' AFTER INSERT AS SET NOCOUNT ON');
        EXEC (N'INSERT INTO ' + @s + N'.t' + @n + N' DEFAULT VALUES');
    END;
    SET @i += 1;
END
""")
        small, big = [], []
        catalog_in(name, DumpScope(kind="schema", schema_name="small"), count=small)
        found = catalog_in(name, DumpScope(kind="schema", schema_name="big"), count=big)
        planned = []
        catalog_in(name, DumpScope(kind="schema", schema_name="big"), ddl=False, setup=False, count=planned)
    assert len(found.tables) == 1000 and len(found.pre) == 1 + 1000 + 1000  # the schema, sequences, tables
    assert [kind for kind, _ in kinds(found.post)].count("foreign key") == 999
    assert {kind: [k for k, _ in kinds(found.post)].count(kind) for kind in (
        "identity value", "index", "view", "function", "trigger")} == {
        "identity value": 300, "index": 1000, "view": 300, "function": 300, "trigger": 300}
    # O(kinds): no query per object, and no list of a thousand ids to compile (a schema's reads filter by schema)
    assert big[0] == small[0] and big[0] <= 16 and planned[0] <= big[0], (small, big, planned)
    print(f"\nqueries: 3 tables {small[0]} ({small[1]:.2f} s), 1,000 tables {big[0]} ({big[1]:.2f} s), "
          f"plan {planned[0]} ({planned[1]:.2f} s)")


def _drop_errors(database: str, drops: list[str]) -> list[int | str]:
    """The error each DROP stops at, in a transaction rolled back (06b I2: what the warning promises). Only `number`
    is read of the driver's own exception."""
    found: list[int | str] = []
    for drop in drops:
        try:
            sa_rows(database, f"BEGIN TRANSACTION; {drop}; ROLLBACK TRANSACTION")
            found.append("dropped")
        except _mssql.MSSQLDatabaseException as exc:
            found.append(exc.number)
    return found


@requires_mssql
def test_a_drop_first_dump_names_what_outside_it_its_drops_run_into():
    """06b I2: a foreign key of another schema's table (3726) and what is schema-bound to what a schema dump writes —
    another schema's view, a table whose computed column calls its function, and its own schema-bound view when views
    are not chosen (3729) — are named; a view that is not schema-bound blocks nothing and is not. Each named DROP stops
    where they exist. The plan's read and the dump's say the same."""
    with scratch_database("mssql", "drops", prefix=SCRATCH_06B) as (database, admin):
        for batch in (
            "CREATE SCHEMA app", "CREATE SCHEMA other", "CREATE TABLE app.t (id int PRIMARY KEY)",
            "CREATE FUNCTION app.f (@v int) RETURNS int WITH SCHEMABINDING AS BEGIN RETURN @v * 2 END",
            "CREATE TABLE other.child (id int PRIMARY KEY, t_id int CONSTRAINT child_t REFERENCES app.t (id))",
            "CREATE VIEW other.bound WITH SCHEMABINDING AS SELECT id FROM app.t",
            "CREATE VIEW other.loose AS SELECT id FROM app.t",
            "CREATE TABLE other.calc (id int, twice AS app.f(id))",
            "CREATE VIEW app.v_bound WITH SCHEMABINDING AS SELECT id FROM app.t",
        ):
            admin.run_in(database, batch)
        schema = DumpScope(kind="schema", schema_name="app")
        for ddl in (True, False):
            found = catalog_in(database, schema, DumpObjectKinds(views=False), ddl=ddl)
            assert [(d.target, d.kind, d.name) for d in found.dependents] == [
                ("app.f", "table", "other.calc"), ("app.t", "foreign key", "other.child.child_t"),
                ("app.t", "view", "app.v_bound"), ("app.t", "view", "other.bound")], ddl
        found = catalog_in(database, schema)  # with its views: its own is dropped by the file first
        assert [(d.target, d.name) for d in found.dependents] == [
            ("app.f", "other.calc"), ("app.t", "other.child.child_t"), ("app.t", "other.bound")]
        assert drop_warnings(found, "both", True) == [
            "Drop objects first: other.calc (table, not in this dump) depends on app.f, so restoring where it exists "
            "fails at that DROP. Dump it too, or turn Drop objects first off.",
            "Drop objects first: other.child.child_t (foreign key), other.bound (view), not in this dump, depend on "
            "app.t, so restoring where they exist fails at that DROP. Dump them too, or turn Drop objects first off."]
        drops = {item.name: item.drop for item in (*found.pre, *found.post) if item.drop and item.kind != "foreign key"}
        assert _drop_errors(database, [drops["app.f"], drops["app.t"]]) == [3729, 3726]
        tables = catalog_in(database, DumpScope(kind="tables", schema_name="app", names=["t"]))
        assert [d.name for d in tables.dependents] == ["other.child.child_t", "app.v_bound", "other.bound"]
