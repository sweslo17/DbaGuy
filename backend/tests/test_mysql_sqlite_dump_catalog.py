"""What a MySQL and a SQLite dump write and in which order (spec ⑥ §3.3.3): DEFINER left out in every account form
(A14), stored programs under their own sql_mode (an event also its time_zone) between DELIMITER lines, views by their
dependencies and with the functions they call, SQLite's counters, view triggers and left-out virtual tables, and what
is not written counted (pre-flight H6); the snapshots (A3: MySQL's SHOW CREATE inside one, InnoDB only (M6), read
under the restore's sql_mode (M5); A5: SQLite's read transaction and other connections' writes). A `drop_first` script
built from each catalog in the order Task 8's DumpScript writes it runs twice into a scratch database or file through
Run SQL file and reads back the same: a scratch schema, and MySQL's seed databases (H7). Scratch: MySQL databases
`zz_06_dumpcat_*` / `zz_06_restore_*`, dropped by name; SQLite files in `tmp_path`."""

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.adapters.dump import DumpCatalog
from dbaguy_backend.adapters.mysql.dump import DEFINERS_LEFT_OUT, RESTORE_SQL_MODE, RESTORE_TIME_ZONE, strip_definer
from dbaguy_backend.adapters.sqlite.dump import JOURNAL_WARNING
from dbaguy_backend.adapters.sqlite.dump import SNAPSHOT_TEXT as SQLITE_SNAPSHOT
from dbaguy_backend.domain.data_jobs import DumpObjectKinds, DumpScope
from dbaguy_backend.domain.errors import NotFound, ValidationFailed
from dbaguy_backend.domain.execution import RunFileCommand
from dbaguy_backend.domain.profiles import SqliteProfile
from dbaguy_backend.sql.dialects import Dialect, qualified, quote_ident, quote_literal
from tests.data_support import DataApp, scratch_database
from tests.edit_support import ScratchDb, scratch_db
from tests.support import requires_mysql

# The restore settings and the reset sql/dump_script.py writes around a MySQL and a SQLite dump (spec §3.3.3, Task 8)
RESTORE = {
    "mysql": ("SET NAMES utf8mb4", "SET FOREIGN_KEY_CHECKS = 0", f"SET SESSION sql_mode = '{RESTORE_SQL_MODE}'",
              f"SET time_zone = '{RESTORE_TIME_ZONE}'"),
    "sqlite": ("PRAGMA foreign_keys = OFF", "PRAGMA defer_foreign_keys = ON"),
}
RESET = {"mysql": ("SET FOREIGN_KEY_CHECKS = 1",), "sqlite": ("PRAGMA foreign_keys = ON",)}


@pytest.mark.parametrize("account", [
    "dbaguy@localhost", "'dbaguy'@'%'", "`dbaguy`@`%`", "`db``x`@`h`", "CURRENT_USER", "CURRENT_USER()",
    "`u` @ `h`", '"u"@"h"',  # the last: a program made under ANSI_QUOTES (its SHOW CREATE quotes with ")
])
def test_definer_is_left_out_in_every_form(account):  # A14
    assert strip_definer(f"CREATE DEFINER={account} PROCEDURE p() SELECT 1") == "CREATE PROCEDURE p() SELECT 1"
    view = f"CREATE ALGORITHM=UNDEFINED DEFINER = {account} SQL SECURITY DEFINER VIEW `v` AS select 1 AS `definer`"
    assert strip_definer(view) == "CREATE ALGORITHM=UNDEFINED SQL SECURITY DEFINER VIEW `v` AS select 1 AS `definer`"
    assert strip_definer("CREATE PROCEDURE p() SELECT 'DEFINER=x@y'") == "CREATE PROCEDURE p() SELECT 'DEFINER=x@y'"
    # only before the object's word: a body's `definer = …` is no clause
    assert strip_definer("CREATE PROCEDURE p() UPDATE t SET definer = 1") == "CREATE PROCEDURE p() UPDATE t SET definer = 1"


def program(found: DumpCatalog, name: str):
    return next(item for item in found.post if item.name == name)


def listed(found: DumpCatalog) -> list[tuple[str, str]]:
    return [(item.kind, item.name) for item in (*found.pre, *found.post)]


def same(found: DumpCatalog) -> tuple:
    """What a restore must give back: every object as the dump writes it, and the tables' columns and references
    (not their size estimates)."""
    objects = [(item.kind, item.name, item.statements, item.drop, item.section, item.compound)
               for item in (*found.pre, *found.post)]
    return objects, [(table.name, table.columns, table.references) for table in found.tables]


def _text(statements, compound: bool = False) -> str:
    if compound:  # sql/dump_script.object_text
        return "DELIMITER ;;\n" + "".join(f"{statement};;\n" for statement in statements) + "DELIMITER ;\n"
    return "".join(f"{statement};\n" for statement in statements)


def script(engine: str, found: DumpCatalog, data: list[str]) -> str:
    """A `drop_first` dump's text in Task 8's order: settings, the DROPs of the objects after the data newest first and
    then of those before it, the objects before the data, the rows (`data`), the objects after it, the reset."""
    drops = [item.drop for item in (*reversed(found.post), *reversed(found.pre)) if item.drop]
    return "".join([_text(RESTORE[engine]), _text(drops), *(_text(item.statements, item.compound) for item in found.pre),
                    _text(data), *(_text(item.statements, item.compound) for item in found.post), _text(RESET[engine])])


def restore(app: DataApp, body: dict, path: Path, mode: str, times: int = 2) -> None:
    """Run the file `times` times (a `drop_first` file replaces what the run before made) through Run SQL file."""
    profile = app.add_profile(body)
    for _ in range(times):
        tab = app.job_session(profile)
        ran = tab.run(lambda rid: RunFileCommand(request_id=rid, path=str(path), mode=mode))
        assert (ran.outcome.outcome, ran.outcome.error_count, ran.error) == ("completed", 0, None), (
            ran.outcome.errors, ran.error)
        app.close_sessions()


# -- MySQL


def mysql_catalog(db: ScratchDb, schema: str, scope: DumpScope | None = None, kinds: DumpObjectKinds | None = None,
                  *, ddl: bool = True, **fields) -> DumpCatalog:
    """The catalog as a dump reads it, inside the dump session (`USE`, the restore's sql_mode, the snapshot); a plan's
    (`ddl` False) as the metadata connection reads it, in the profile's database and outside any session."""
    scope = scope or DumpScope(kind="schema", schema_name=schema)
    conn = db.open(**fields)
    try:
        session = conn.dump_session(scope) if ddl else None
        for statement in session.setup if session else ():
            conn.run(statement).close()
        found = conn.dump_catalog(scope, kinds or DumpObjectKinds(), ddl=ddl)
        for statement in session.teardown if session else ():
            conn.run(statement).close()
        return found
    finally:
        conn.close()


def mysql_copy(found: DumpCatalog, source: str) -> list[str]:
    """The rows of each table, copied on the server from `source` (the data a dump writes is Task 8's and 9's)."""
    statements = []
    for table in found.tables:
        columns = ", ".join(quote_ident(Dialect.MYSQL, column.name) for column in table.columns if column.copied)
        statements.append(f"INSERT INTO {quote_ident(Dialect.MYSQL, table.table)} ({columns}) SELECT {columns} FROM "
                          f"{qualified(Dialect.MYSQL, source, table.table)}")
    return statements


@requires_mysql
def test_a_mysql_dump_orders_its_objects_and_drops_their_definers(tmp_path):
    db = scratch_db("mysql", tmp_path)
    try:
        with scratch_database("mysql", "dumpcat") as (name, admin):
            admin.run_in(
                name,
                "CREATE TABLE parent (id INT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(20))",
                "CREATE TABLE child (id INT PRIMARY KEY, parent_id INT, FOREIGN KEY (parent_id) REFERENCES parent (id))",
                "INSERT INTO parent (label) VALUES ('a'), ('b')",
                "CREATE DEFINER=`dbaguy`@`%` VIEW b_base AS SELECT id FROM parent",
                "CREATE DEFINER=`dbaguy`@`%` VIEW a_top AS SELECT id FROM b_base",  # its name sorts before what it uses
                "SET SESSION sql_mode = 'ANSI_QUOTES'",
                'CREATE DEFINER=`dbaguy`@`%` PROCEDURE two() BEGIN SELECT 1; SELECT "parent".id FROM parent; END',
                "SET SESSION sql_mode = DEFAULT",
                "CREATE DEFINER=`dbaguy`@`%` FUNCTION add1(i INT) RETURNS INT DETERMINISTIC RETURN i + 1",
                "CREATE DEFINER=`dbaguy`@`%` VIEW calls_add1 AS SELECT add1(id) AS n FROM parent",
                "CREATE DEFINER=`dbaguy`@`%` TRIGGER child_bi BEFORE INSERT ON child FOR EACH ROW "
                "BEGIN IF NEW.parent_id IS NULL THEN SET NEW.parent_id = 1; END IF; END",
                "CREATE DEFINER=`dbaguy`@`%` TRIGGER parent_bu BEFORE UPDATE ON parent FOR EACH ROW "
                "SET NEW.label = NEW.label",
                "SET SESSION time_zone = '+08:00'",  # its STARTS is in this zone (pre-flight L13)
                "CREATE DEFINER=`dbaguy`@`%` EVENT tidy ON SCHEDULE EVERY 1 DAY STARTS '2030-01-01 00:00:00' "
                "DO DELETE FROM child WHERE id < 0",
            )
            scope = DumpScope(kind="schema", schema_name=name)
            conn = db.open(database=name)
            try:
                for statement in conn.dump_session(scope).setup:
                    conn.run(statement).close()
                admin.run_in(name, "INSERT INTO parent (label) VALUES ('after the snapshot')")
                found = conn.dump_catalog(scope, DumpObjectKinds())  # A3
                result = conn.run("SELECT COUNT(*) FROM parent")
                assert result.fetch(10) == [[2]]  # the snapshot does not see the later row
                result.close()
                conn.rollback()
            finally:
                conn.close()
            assert [(item.kind, item.name) for item in found.pre] == [("table", "child"), ("table", "parent")]
            assert [(item.kind, item.name) for item in found.post] == [
                ("function", "add1"), ("procedure", "two"), ("view", "b_base"), ("view", "a_top"),
                ("view", "calls_add1"), ("trigger", "child_bi"), ("trigger", "parent_bu"), ("event", "tidy")]
            assert [(table.name, table.references) for table in found.tables] == [("child", ("parent",)),
                                                                                  ("parent", ())]
            assert "REFERENCES `parent`" in found.pre[0].statements[0]
            assert "AUTO_INCREMENT=4" in found.pre[1].statements[0]  # S10: the counter is no part of the snapshot
            assert found.pre[1].drop == "DROP TABLE IF EXISTS parent"
            two = program(found, "two")
            assert two.compound and two.statements[0] == "SET SESSION sql_mode = 'ANSI_QUOTES'"
            assert two.statements[1].startswith('CREATE PROCEDURE "two"()')  # its own sql_mode quotes with "
            assert "DEFINER" not in two.statements[1]
            assert two.statements[2] == f"SET SESSION sql_mode = '{RESTORE_SQL_MODE}'"
            view = program(found, "a_top").statements[0]
            assert view.startswith("CREATE ALGORITHM=UNDEFINED SQL SECURITY DEFINER VIEW `a_top` AS select")
            assert f"`{name}`" not in view  # USE: no database in the names
            tidy = program(found, "tidy")
            assert tidy.statements[0].endswith(", SESSION time_zone = '+08:00'")  # L13
            assert tidy.statements[1].startswith("CREATE EVENT `tidy`") and "STARTS '2030-01-01 00:00:00'" in \
                tidy.statements[1]
            assert tidy.statements[2] == f"SET SESSION sql_mode = '{RESTORE_SQL_MODE}', SESSION time_zone = '+00:00'"
            assert program(found, "child_bi").drop == "DROP TRIGGER IF EXISTS child_bi"
            assert program(found, "child_bi").statements[1].startswith("CREATE TRIGGER `child_bi`")
            assert found.not_included == ("owners and privileges", DEFINERS_LEFT_OUT)
            assert (found.consistency, found.snapshot_text, found.warnings) == (
                "snapshot", "one consistent snapshot (InnoDB)", ())
            plan = mysql_catalog(db, name, ddl=False)  # the metadata connection: in the profile's database
            assert listed(plan) == listed(found) and not any(item.statements for item in (*plan.pre, *plan.post))
            assert plan.not_included == found.not_included
            kinds = DumpObjectKinds(views=False, routines=False, triggers=False)
            bare = mysql_catalog(db, name, kinds=kinds)
            assert listed(bare) == [("table", "child"), ("table", "parent")]
            assert bare.not_included == ("owners and privileges", "3 views (not chosen)",
                                         "2 functions and procedures (not chosen)", "2 triggers (not chosen)",
                                         "1 events (not chosen)")
            named = mysql_catalog(db, name, DumpScope(kind="tables", schema_name=name, names=["child", "calls_add1"]))
            # the function its view calls comes with it (VIEW_ROUTINE_USAGE)
            assert listed(named) == [("table", "child"), ("function", "add1"), ("view", "calls_add1"),
                                     ("trigger", "child_bi")]
            assert named.warnings == ("Foreign key child_ibfk_1 of child references parent, which is not in this dump: "
                                      "the file keeps it, and needs that table where it is run",)
            with pytest.raises(NotFound):
                mysql_catalog(db, name, DumpScope(kind="tables", schema_name=name, names=["nope"]))
            with pytest.raises(ValidationFailed, match="A MySQL dump covers one database"):  # S18
                mysql_catalog(db, name, DumpScope(kind="database"))
    finally:
        db.close()


@requires_mysql
def test_a_mysql_dump_read_under_ansi_quotes_restores_twice_and_says_what_is_not_in_its_snapshot(tmp_path):
    """M5: a profile whose sql_mode quotes with " still writes DDL the restore parses. M6: a MyISAM table is read
    outside the snapshot, and the header says so. H7: a child table older than its parent, DROPs first, twice."""
    db = scratch_db("mysql", tmp_path)
    app = DataApp(tmp_path)
    ansi = {"sql_mode": "ANSI_QUOTES,STRICT_TRANS_TABLES"}
    try:
        with scratch_database("mysql", "dumpcat") as (name, admin), \
                scratch_database("mysql", "restore") as (target, _):
            admin.run_in(
                name,
                "SET FOREIGN_KEY_CHECKS = 0",
                "CREATE TABLE kid (id INT PRIMARY KEY, mom_id INT, CONSTRAINT kid_mom FOREIGN KEY (mom_id) "
                "REFERENCES mom (id))",
                "CREATE TABLE mom (id INT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(20) DEFAULT 'it''s' NOT NULL, "
                "size INT GENERATED ALWAYS AS (CHAR_LENGTH(label)) VIRTUAL, CHECK (label <> '')) COMMENT = 'the \"mom\" table'",
                "CREATE TABLE logbook (id INT PRIMARY KEY, note TEXT) ENGINE=MyISAM",
                "SET FOREIGN_KEY_CHECKS = 1",
                "INSERT INTO mom (label) VALUES ('a'), ('b'), ('c')",
                "DELETE FROM mom WHERE id = 3",  # AUTO_INCREMENT stays at 4
                "INSERT INTO kid VALUES (1, 1), (2, 2)",
                "INSERT INTO logbook VALUES (1, 'x\\\\y')",
                "CREATE DEFINER=`dbaguy`@`%` VIEW labels AS SELECT label, size FROM mom",
                "SET SESSION sql_mode = 'ANSI_QUOTES'",
                'CREATE DEFINER=`dbaguy`@`%` TRIGGER kid_bi BEFORE INSERT ON kid FOR EACH ROW SET NEW."mom_id" = '
                'COALESCE(NEW."mom_id", 1)',
                "SET SESSION sql_mode = DEFAULT",
                # created after kid_bi, it runs before it: SHOW CREATE TRIGGER drops PRECEDES, ACTION_ORDER keeps it
                "CREATE DEFINER=`dbaguy`@`%` TRIGGER kid_first BEFORE INSERT ON kid FOR EACH ROW PRECEDES kid_bi "
                "SET NEW.id = NEW.id",
                "SET SESSION sql_mode = 'ANSI_QUOTES'",
                'CREATE DEFINER=`dbaguy`@`%` PROCEDURE "count kids"() SELECT COUNT(*) FROM "kid"',
                "SET SESSION sql_mode = DEFAULT",
                "SET SESSION time_zone = '-05:00'",
                "CREATE DEFINER=`dbaguy`@`%` EVENT nightly ON SCHEDULE EVERY 1 DAY STARTS '2030-01-01 02:00:00' "
                "ENDS '2031-01-01 00:00:00' DO DELETE FROM logbook WHERE id < 0",
                "CREATE PROCEDURE roots() SELECT 1",  # root's: the dev login may not read its definition
                "ANALYZE TABLE mom UPDATE HISTOGRAM ON label WITH 4 BUCKETS",  # statistics: counted, not written
            )
            found = mysql_catalog(db, name, options=ansi)
            assert found.pre[0].statements[0].startswith("CREATE TABLE `kid` (")  # M5: not "kid"
            assert listed(found) == [("table", "kid"), ("table", "logbook"), ("table", "mom"),
                                     ("procedure", "count kids"), ("view", "labels"), ("trigger", "kid_first"),
                                     ("trigger", "kid_bi"), ("event", "nightly")]
            assert (found.consistency, found.snapshot_text, found.warnings) == (  # M6
                "per_table", "one consistent snapshot of the InnoDB tables; 1 other tables read as the dump reached them",
                ("Not in the snapshot (not InnoDB): logbook",))
            assert [(table.name, table.references) for table in found.tables] == [
                ("kid", ("mom",)), ("logbook", ()), ("mom", ())]
            unreadable = ("owners and privileges", "1 procedures whose definition cannot be read", "1 column histograms",
                          DEFINERS_LEFT_OUT)
            assert found.not_included == unreadable
            plan = mysql_catalog(db, name, ddl=False)  # it knows without reading the definitions
            assert (listed(plan), plan.not_included) == (listed(found), unreadable)
            assert [column.name for column in found.tables[2].columns if column.copied] == ["id", "label"]
            assert "AUTO_INCREMENT=4" in found.pre[2].statements[0]  # the restore's DDL is compared below
            path = tmp_path / "dump.sql"
            path.write_text(script("mysql", found, mysql_copy(found, name)), encoding="utf-8")
            restore(app, db.profile(database=target), path, "statements")  # S6: statement by statement, twice
            again = mysql_catalog(db, target, options=ansi)
            assert same(again) == same(found)
            assert (again.not_included, again.warnings, again.consistency) == (  # root's procedure was not written
                ("owners and privileges", DEFINERS_LEFT_OUT), found.warnings, found.consistency)
            for table in ("kid", "logbook", "mom"):
                assert db.rows(f"SELECT * FROM {qualified(Dialect.MYSQL, target, table)} ORDER BY id") == \
                    db.rows(f"SELECT * FROM {qualified(Dialect.MYSQL, name, table)} ORDER BY id"), table
            event = "SELECT STARTS, ENDS, TIME_ZONE FROM information_schema.EVENTS WHERE EVENT_SCHEMA = "
            assert db.rows(event + quote_literal(Dialect.MYSQL, target)) == \
                db.rows(event + quote_literal(Dialect.MYSQL, name))  # L13
    finally:
        app.close()
        db.close()


@requires_mysql
@pytest.mark.parametrize("seed", ["dbaguy", "analytics"])
def test_mysqls_seed_dumps_and_restores_twice(tmp_path, seed):
    """The seed's tables (identifiers that need quoting, every type, a zero date), its view, routines, trigger and
    event: a `drop_first` script restored twice into a scratch database reads back the same, row for row (the seed
    is only read)."""
    db = scratch_db("mysql", tmp_path)
    app = DataApp(tmp_path)
    try:
        with scratch_database("mysql", "restore") as (target, _):
            found = mysql_catalog(db, seed)
            assert found.not_included[0] == "owners and privileges" and found.consistency == "snapshot"
            path = tmp_path / "seed.sql"
            path.write_text(script("mysql", found, mysql_copy(found, seed)), encoding="utf-8")
            restore(app, db.profile(database=target), path, "statements")
            again = mysql_catalog(db, target)
            assert same(again) == same(found) and again.not_included == found.not_included
            names = [qualified(Dialect.MYSQL, schema, table.table) for table in found.tables for schema in (seed, target)]
            sums = db.rows(f"CHECKSUM TABLE {', '.join(names)}")
            assert [row[1] for row in sums[0::2]] == [row[1] for row in sums[1::2]]
            assert all(row[1] is not None for row in sums)  # None: no such table
    finally:
        app.close()
        db.close()


# -- SQLite


def sqlite_catalog(path: Path, scope: DumpScope | None = None, kinds: DumpObjectKinds | None = None, *,
                   ddl: bool = True) -> DumpCatalog:
    conn = SqliteAdapter().open(SqliteProfile(id=uuid4(), engine="sqlite", name="s", path=str(path)), None, None, None)
    try:
        return conn.dump_catalog(scope or DumpScope(kind="schema", schema_name="main"), kinds or DumpObjectKinds(),
                                 ddl=ddl)
    finally:
        conn.close()


def test_a_sqlite_dump_keeps_sqlite_masters_order_and_its_counters(tmp_path):
    db = scratch_db("sqlite", tmp_path)
    db.execute(
        "CREATE TABLE parent (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT)",
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent (id), v)",
        "CREATE INDEX child_parent ON child (parent_id)",
        "CREATE VIEW labels AS SELECT label FROM parent",
        "CREATE TRIGGER child_ai AFTER INSERT ON child BEGIN UPDATE child SET v = 1 WHERE id = new.id; END",
        "CREATE VIRTUAL TABLE notes USING fts5(body)",
        "INSERT INTO parent (label) VALUES ('a'), ('b'), ('c')",
        "DELETE FROM parent WHERE id = 3",  # the counter stays at 3: the next id is 4, not 3
        "ANALYZE",
    )
    try:
        found = sqlite_catalog(db.file)
        assert [(item.kind, item.name) for item in found.pre] == [("table", "parent"), ("table", "child")]
        assert [(item.kind, item.name) for item in found.post] == [
            ("sequence value", "parent"), ("index", "child_parent"), ("view", "labels"), ("trigger", "child_ai")]
        assert found.not_included == ("owners and privileges", "1 virtual tables",
                                      "5 internal tables of virtual tables", "2 SQLite statistics tables (ANALYZE)")
        counter = program(found, "parent")
        assert counter.section == "data" and counter.statements == (
            "UPDATE sqlite_sequence SET seq = max(seq, 3) WHERE name = 'parent'",
            "INSERT INTO sqlite_sequence (name, seq) SELECT 'parent', 3 WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence "
            "WHERE name = 'parent')")
        assert [column.name for column in found.tables[1].columns] == ["id", "parent_id", "v"]
        assert [(table.name, table.references) for table in found.tables] == [("parent", ()), ("child", ("parent",))]
        assert (found.consistency, found.snapshot_text, found.warnings) == ("snapshot", SQLITE_SNAPSHOT,
                                                                             (JOURNAL_WARNING,))
        plan = sqlite_catalog(db.file, ddl=False)
        assert listed(plan) == listed(found) and not any(item.statements for item in (*plan.pre, *plan.post))
        assert plan.not_included == found.not_included
        bare = sqlite_catalog(db.file, kinds=DumpObjectKinds(views=False, triggers=False))
        assert listed(bare) == [("table", "parent"), ("table", "child"), ("sequence value", "parent"),
                                ("index", "child_parent")]
        assert bare.not_included[-2:] == ("1 views (not chosen)", "1 triggers (not chosen)")
        with pytest.raises(NotFound):
            sqlite_catalog(db.file, DumpScope(kind="tables", schema_name="main", names=["nope"]))
        with pytest.raises(ValidationFailed):
            sqlite_catalog(db.file, DumpScope(kind="schema", schema_name="other"))
    finally:
        db.close()


@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_a_sqlite_dump_holds_other_writers_back_only_without_wal(tmp_path, journal):  # A5
    db = scratch_db("sqlite", tmp_path)
    db.execute(f"PRAGMA journal_mode = {journal}", "CREATE TABLE t (a)", "INSERT INTO t VALUES (1)")
    conn = db.open()
    other = sqlite3.connect(db.file, isolation_level=None, timeout=0.1)
    try:
        for statement in conn.dump_session(DumpScope(kind="schema", schema_name="main")).setup:
            conn.run(statement).close()
        found = conn.dump_catalog(DumpScope(kind="schema", schema_name="main"), DumpObjectKinds())  # the read starts
        assert found.warnings == ((JOURNAL_WARNING,) if journal == "delete" else ())
        if journal == "delete":
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                other.execute("INSERT INTO t VALUES (2)")
        else:
            other.execute("INSERT INTO t VALUES (2)")
        conn.rollback()
    finally:
        other.close()
        conn.close()
        db.close()


def _sqlite_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return f"X'{value.hex()}'"
    return quote_literal(Dialect.SQLITE, value) if isinstance(value, str) else repr(value)


def sqlite_rows(source: Path, found: DumpCatalog) -> list[str]:
    """Each table's rows as INSERTs of the copied columns (the dump's own INSERTs are Task 8's)."""
    conn = sqlite3.connect(source)
    try:
        statements = []
        for table in found.tables:
            columns = [quote_ident(Dialect.SQLITE, column.name) for column in table.columns if column.copied]
            for row in conn.execute(f"SELECT {', '.join(columns)} FROM {quote_ident(Dialect.SQLITE, table.table)}"):
                values = ", ".join(_sqlite_literal(value) for value in row)
                statements.append(f"INSERT INTO {quote_ident(Dialect.SQLITE, table.table)} ({', '.join(columns)}) "
                                  f"VALUES ({values})")
        return statements
    finally:
        conn.close()


def test_a_sqlite_dump_restores_twice_with_its_counters_view_triggers_and_user_version(tmp_path):
    """A child table older than its parent (foreign keys deferred inside the run's transaction), AUTOINCREMENT
    counters past the rows and of an emptied table, a generated column, WITHOUT ROWID, STRICT, a partial index, an
    INSTEAD OF trigger on a view (H6), user_version; the FTS table is left out and counted."""
    db = scratch_db("sqlite", tmp_path)
    db.execute(
        "CREATE TABLE kid (id INTEGER PRIMARY KEY, mom_id INTEGER REFERENCES mom (id))",
        "CREATE TABLE mom (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL DEFAULT 'it''s', "
        "twice INTEGER GENERATED ALWAYS AS (id * 2) VIRTUAL)",
        "CREATE TABLE pairs (a INT, b TEXT, PRIMARY KEY (a, b)) WITHOUT ROWID",
        'CREATE TABLE "odd name" (id INTEGER PRIMARY KEY, v ANY, data BLOB) STRICT',
        "CREATE TABLE emptied (id INTEGER PRIMARY KEY AUTOINCREMENT)",
        "CREATE INDEX mom_label ON mom (label) WHERE label <> ''",
        "CREATE UNIQUE INDEX pairs_b ON pairs (b)",
        "CREATE VIEW labels AS SELECT label FROM mom",
        "CREATE TRIGGER labels_add INSTEAD OF INSERT ON labels BEGIN INSERT INTO mom (label) VALUES (new.label); END",
        "CREATE TRIGGER kid_ai AFTER INSERT ON kid BEGIN UPDATE mom SET label = label WHERE id = new.mom_id; END",
        "CREATE VIRTUAL TABLE notes USING fts5(body)",
        "INSERT INTO mom (label) VALUES ('a'), ('b'), ('c')",
        "DELETE FROM mom WHERE id = 3",
        "INSERT INTO kid VALUES (1, 1), (2, 2)",
        "INSERT INTO pairs VALUES (1, 'x'), (2, 'y')",
        """INSERT INTO "odd name" VALUES (1, '007', X'00ff'), (2, 7, NULL), (3, 1.5, X'')""",
        "INSERT INTO emptied DEFAULT VALUES",
        "DELETE FROM emptied",
        "INSERT INTO notes (body) VALUES ('left out')",
        "PRAGMA user_version = 12",
    )
    try:
        found = sqlite_catalog(db.file)
        assert listed(found) == [
            ("table", "kid"), ("table", "mom"), ("table", "pairs"), ("table", "odd name"), ("table", "emptied"),
            ("sequence value", "mom"), ("sequence value", "emptied"), ("index", "mom_label"), ("index", "pairs_b"),
            ("view", "labels"), ("trigger", "labels_add"), ("trigger", "kid_ai"), ("setting", "user_version")]
        assert program(found, "user_version").statements == ("PRAGMA user_version = 12",)
        assert found.not_included == ("owners and privileges", "1 virtual tables", "5 internal tables of virtual tables")
        target = tmp_path / "target.db"
        sqlite3.connect(target).close()
        path = tmp_path / "lite.sql"
        path.write_text(script("sqlite", found, sqlite_rows(db.file, found)), encoding="utf-8")
        app = DataApp(tmp_path)
        try:
            restore(app, {"profile": {"engine": "sqlite", "name": "target", "path": str(target)}}, path,
                    "all_or_nothing")
        finally:
            app.close()
        again = sqlite_catalog(target)
        assert same(again) == same(found) and again.not_included == ("owners and privileges",)
        checks = ["SELECT * FROM sqlite_sequence ORDER BY name", "PRAGMA user_version",
                  "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'notes%' ORDER BY name",
                  *(f"SELECT *, {', '.join(f'typeof({quote_ident(Dialect.SQLITE, c.name)})' for c in t.columns)} FROM "
                    f"{quote_ident(Dialect.SQLITE, t.table)} ORDER BY 1, 2" for t in found.tables)]
        a, b = sqlite3.connect(db.file), sqlite3.connect(target)
        try:
            for check in checks:
                assert b.execute(check).fetchall() == a.execute(check).fetchall(), check
        finally:
            a.close()
            b.close()
        named = sqlite_catalog(db.file, DumpScope(kind="tables", schema_name="main", names=["kid", "labels"]))
        assert listed(named) == [("table", "kid"), ("view", "labels"), ("trigger", "labels_add"), ("trigger", "kid_ai")]
        assert named.warnings == (JOURNAL_WARNING, "A foreign key of kid references mom, which is not in this dump: "
                                  "the file keeps it, and needs that table where it is run")
        assert named.not_included == ("owners and privileges",)  # a tables scope: no settings, nothing else counted
    finally:
        db.close()
