"""What a MySQL and a SQLite dump write and in which order (spec ⑥ §3.3.3): DEFINER left out in every account form
(A14), stored programs under their own sql_mode (an event also its time_zone) between DELIMITER lines, views by their
dependencies and with the functions they call, SQLite's counters, view triggers and left-out virtual tables, and what
is not written counted (pre-flight H6); the snapshots (A3: MySQL's SHOW CREATE inside one, InnoDB only (M6), read
under the restore's sql_mode (M5); A5: SQLite's read transaction and other connections' writes). A `drop_first` script
built from each catalog in the order Task 8's DumpScript writes it runs twice into a scratch database or file through
Run SQL file and reads back the same: a scratch schema, and MySQL's seed databases (H7). "The same" is an independent
inventory read as root (tests/mysql_inventory.py, tests/sqlite_inventory.py: review T10), never the dump's reader.
The review's cases (T1–T8): what cannot be written takes what needs it out, said by name; MERGE rows; collations;
one column query; a `*/` in a version comment's string. Scratch: MySQL databases `zz_06_dumpcat_*` /
`zz_06_restore_*`, dropped (and their grant revoked) by name; SQLite files in `tmp_path`."""

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.adapters.dump import DumpCatalog
from dbaguy_backend.adapters.mysql.dump import (
    DEFINERS_LEFT_OUT, RESTORE_SQL_MODE, RESTORE_TIME_ZONE, restored_settings, strip_definer,
)
from dbaguy_backend.adapters.sqlite.dump import JOURNAL_WARNING
from dbaguy_backend.adapters.sqlite.dump import SNAPSHOT_TEXT as SQLITE_SNAPSHOT
from dbaguy_backend.domain.data_jobs import DumpObjectKinds, DumpScope
from dbaguy_backend.domain.errors import NotFound, ValidationFailed
from dbaguy_backend.domain.execution import RunFileCommand
from dbaguy_backend.domain.profiles import SqliteProfile
from dbaguy_backend.sql.dialects import Dialect, qualified, quote_ident, quote_literal
from tests import mysql_inventory, sqlite_inventory
from tests.data_support import DataApp, scratch_database
from tests.edit_support import ScratchDb, scratch_db
from tests.support import requires_mysql

UTF8MB4 = "SESSION character_set_client = 'utf8mb4', SESSION collation_connection = 'utf8mb4_0900_ai_ci'"

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
            # S20 and T6: its own sql_mode, character_set_client and collation_connection, then the file's again
            assert two.compound and two.statements[0] == f"SET SESSION sql_mode = 'ANSI_QUOTES', {UTF8MB4}"
            assert two.statements[1].startswith('CREATE PROCEDURE "two"()')  # its own sql_mode quotes with "
            assert "DEFINER" not in two.statements[1]
            assert two.statements[2] == f"SET NAMES utf8mb4, SESSION sql_mode = '{RESTORE_SQL_MODE}'"
            a_top = program(found, "a_top")
            assert not a_top.compound and a_top.statements[0] == f"SET {UTF8MB4}"
            assert a_top.statements[1].startswith("CREATE ALGORITHM=UNDEFINED SQL SECURITY DEFINER VIEW `a_top` AS select")
            assert f"`{name}`" not in a_top.statements[1]  # USE: no database in the names
            assert a_top.statements[2] == "SET NAMES utf8mb4"
            tidy = program(found, "tidy")
            assert "SESSION time_zone = '+08:00'" in tidy.statements[0]  # L13
            assert tidy.statements[1].startswith("CREATE EVENT `tidy`") and "STARTS '2030-01-01 00:00:00'" in \
                tidy.statements[1]
            assert tidy.statements[2] == restored_settings(sql_mode=True, time_zone=True) == (
                f"SET NAMES utf8mb4, SESSION sql_mode = '{RESTORE_SQL_MODE}', SESSION time_zone = '{RESTORE_TIME_ZONE}'")
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


def mysql_restored(db: ScratchDb, found: DumpCatalog, source: str, target: str, tmp_path: Path) -> None:
    """`found` as a `drop_first` file (rows copied from `source` on the server), run twice into `target` statement
    by statement (S6) through Run SQL file."""
    path = tmp_path / "dump.sql"
    path.write_text(script("mysql", found, mysql_copy(found, source)), encoding="utf-8")
    app = DataApp(tmp_path)
    try:
        restore(app, db.profile(database=target), path, "statements")
    finally:
        app.close()


def behaviour(database: str, *queries: str) -> list:
    conn = mysql_inventory.root(database)
    try:
        return [mysql_inventory.run(conn, query) for query in queries]
    finally:
        conn.close()


@requires_mysql
def test_a_mysql_dump_read_under_ansi_quotes_restores_twice_and_says_what_is_not_in_its_snapshot(tmp_path):
    """M5: a profile whose sql_mode quotes with " still writes DDL the restore parses. M6: a MyISAM table is read
    outside the snapshot, and the header says so. H7: a child table older than its parent, DROPs first, twice. T2: a
    MERGE table's rows are its tables' (written once). T8: a `*/` in a string of SHOW CREATE TABLE's /*!50500 …*/."""
    db = scratch_db("mysql", tmp_path)
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
                "size INT GENERATED ALWAYS AS (CHAR_LENGTH(label)) VIRTUAL, CHECK (label <> '')) "
                "COMMENT = 'the \"mom\" table'",
                "CREATE TABLE logbook (id INT PRIMARY KEY, note TEXT) ENGINE=MyISAM",
                "SET FOREIGN_KEY_CHECKS = 1",
                "CREATE TABLE part1 (id INT, v VARCHAR(10)) ENGINE=MyISAM",
                "CREATE TABLE part2 (id INT, v VARCHAR(10)) ENGINE=MyISAM",
                "CREATE TABLE whole (id INT, v VARCHAR(10)) ENGINE=MERGE UNION=(part1, part2) INSERT_METHOD=LAST",
                "CREATE TABLE pl (id INT NOT NULL, s VARCHAR(10) NOT NULL) PARTITION BY LIST COLUMNS (s) "
                "(PARTITION p1 VALUES IN ('a*/b') COMMENT 'x */ ; y', PARTITION p2 VALUES IN ('c'))",
                "INSERT INTO mom (label) VALUES ('a'), ('b'), ('c')",
                "DELETE FROM mom WHERE id = 3",  # AUTO_INCREMENT stays at 4
                "INSERT INTO kid VALUES (1, 1), (2, 2)",
                "INSERT INTO logbook VALUES (1, 'x\\\\y')",
                "INSERT INTO part1 VALUES (1, 'one')", "INSERT INTO part2 VALUES (2, 'two')",
                "INSERT INTO pl VALUES (1, 'a*/b'), (2, 'c')",
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
            assert listed(found) == [
                ("table", "kid"), ("table", "logbook"), ("table", "mom"), ("table", "part1"), ("table", "part2"),
                ("table", "pl"), ("table", "whole"), ("procedure", "count kids"), ("view", "labels"),
                ("trigger", "kid_first"), ("trigger", "kid_bi"), ("event", "nightly")]
            assert (found.consistency, found.snapshot_text, found.warnings) == (  # M6; the MERGE table is not read
                "per_table", "one consistent snapshot of the InnoDB tables; 3 other tables read as the dump reached them",
                ("Not in the snapshot (not InnoDB): logbook, part1, part2",))
            assert [(table.name, table.references) for table in found.tables] == [
                ("kid", ("mom",)), ("logbook", ()), ("mom", ()), ("part1", ()), ("part2", ()), ("pl", ())]  # T2
            unreadable = ("owners and privileges", "1 procedures whose definition cannot be read", "1 column histograms",
                          "1 MERGE and FEDERATED tables written without their rows (other tables hold them)",
                          DEFINERS_LEFT_OUT)
            assert found.not_included == unreadable
            plan = mysql_catalog(db, name, ddl=False)  # it knows without reading the definitions
            assert (listed(plan), plan.not_included) == (listed(found), unreadable)
            assert [column.name for column in found.tables[2].columns if column.copied] == ["id", "label"]
            assert "AUTO_INCREMENT=4" in found.pre[2].statements[0]
            mysql_restored(db, found, name, target, tmp_path)
            source = mysql_inventory.without(mysql_inventory.inventory(name), "roots")  # root's: not written
            source["histograms"] = []  # counted, not written
            assert mysql_inventory.differences(source, mysql_inventory.inventory(target)) == []
            assert behaviour(target, "SELECT COUNT(*) FROM whole") == [[(2,)]]  # T2: not 2 + 2
    finally:
        db.close()


@requires_mysql
@pytest.mark.parametrize("seed", ["dbaguy", "analytics"])
def test_mysqls_seed_dumps_and_restores_twice(tmp_path, seed):
    """The seed's tables (identifiers that need quoting, every type, a zero date), its view, routines, trigger and
    event: a `drop_first` script restored twice into a scratch database reads back the same in root's inventory, row
    for row (the seed is only read)."""
    db = scratch_db("mysql", tmp_path)
    try:
        with scratch_database("mysql", "restore") as (target, _):
            found = mysql_catalog(db, seed)
            assert found.not_included[0] == "owners and privileges" and found.consistency == "snapshot"
            mysql_restored(db, found, seed, target, tmp_path)
            assert mysql_inventory.differences(mysql_inventory.inventory(seed), mysql_inventory.inventory(target)) == []
    finally:
        db.close()


@requires_mysql
def test_what_a_mysql_dump_cannot_write_takes_what_needs_it_out_by_name(tmp_path):
    """T3: a program made under NO_BACKSLASH_ESCAPES. Unreadable: root's routines. T5: an invalid view. T4: the views
    that use those, the views built on them, and (beyond T4) the triggers and events that call them. Each is left out,
    counted and named, the plan says the same, and what is written restores twice and works."""
    db = scratch_db("mysql", tmp_path)
    d = "DEFINER=`dbaguy`@`%`"
    try:
        with scratch_database("mysql", "dumpcat") as (name, admin), \
                scratch_database("mysql", "restore") as (target, _):
            admin.run_in(
                name,
                "CREATE TABLE base (id INT PRIMARY KEY, v VARCHAR(10))",
                "INSERT INTO base VALUES (1, 'a')",
                "SET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES'",
                f"CREATE {d} FUNCTION f_nbe() RETURNS VARCHAR(20) DETERMINISTIC RETURN 'C:\\'",
                f"CREATE {d} TRIGGER base_nbe BEFORE UPDATE ON base FOR EACH ROW SET NEW.v = 'C:\\'",
                "SET SESSION sql_mode = DEFAULT",
                "CREATE FUNCTION f_root() RETURNS INT DETERMINISTIC RETURN 41",
                "CREATE PROCEDURE p_root() SELECT 1",
                f"CREATE {d} VIEW v_root AS SELECT f_root() + id AS n FROM base",
                f"CREATE {d} VIEW v_on_root AS SELECT n FROM v_root",
                f"CREATE {d} VIEW v_nbe AS SELECT f_nbe() AS p",
                "CREATE TABLE gone (id INT)",
                f"CREATE {d} VIEW v_gone AS SELECT id FROM gone",
                f"CREATE {d} VIEW v_on_gone AS SELECT id FROM v_gone",
                "DROP TABLE gone",
                f"CREATE {d} VIEW v_fine AS SELECT id FROM base",
                f"CREATE {d} TRIGGER base_calls BEFORE INSERT ON base FOR EACH ROW SET NEW.v = f_root()",
                f"CREATE {d} TRIGGER base_fine AFTER INSERT ON base FOR EACH ROW SET @x = NEW.id",
                f"CREATE {d} EVENT ev_calls ON SCHEDULE EVERY 1 DAY STARTS '2030-01-01' DISABLE DO CALL p_root()",
            )
            found = mysql_catalog(db, name)
            assert listed(found) == [("table", "base"), ("view", "v_fine"), ("trigger", "base_fine")]
            assert found.not_included == (
                "owners and privileges", "1 functions whose definition cannot be read",
                "1 procedures whose definition cannot be read", "2 stored programs made under NO_BACKSLASH_ESCAPES",
                "2 invalid views", "5 views, triggers and events that use a routine or view the dump leaves out",
                DEFINERS_LEFT_OUT)
            assert found.warnings == (
                "Not written, made under NO_BACKSLASH_ESCAPES (the file reads a backslash in a string as an escape): "
                "function f_nbe, trigger base_nbe",
                "Not written, invalid where they are (a table, column or function they use is gone, or their definer "
                "lacks rights to it): view v_gone, view v_on_gone",  # built on an invalid view, it is one too
                "Not written, they use a routine or view the dump leaves out: view v_nbe, view v_root, view v_on_root, "
                "trigger base_calls, event ev_calls")
            plan = mysql_catalog(db, name, ddl=False)
            assert (listed(plan), plan.not_included, plan.warnings) == (listed(found), found.not_included,
                                                                        found.warnings)
            mysql_restored(db, found, name, target, tmp_path)
            left = ("f_nbe", "f_root", "p_root", "v_root", "v_on_root", "v_nbe", "v_gone", "v_on_gone", "base_calls",
                    "base_nbe", "ev_calls")
            assert mysql_inventory.differences(mysql_inventory.without(mysql_inventory.inventory(name), *left),
                                               mysql_inventory.inventory(target)) == []
            assert behaviour(target, "INSERT INTO base VALUES (2, 'b')", "SELECT COUNT(*) FROM v_fine") == [
                None, [(2,)]]  # nothing written calls what is missing
    finally:
        db.close()


@requires_mysql
def test_mysql_programs_and_views_keep_their_collation_and_a_database_default_is_said(tmp_path):
    """T6: a function, a view, a trigger and an event made under utf8mb4_bin compare 'a' = 'A' as they did; a latin1
    database's routines take their parameters' character set from the database the file runs in, so the dump says
    which one to make; a non-ASCII body made under a latin1 client keeps its meaning (its client character set stays
    utf8mb4: the file is UTF-8)."""
    db = scratch_db("mysql", tmp_path)
    d = "DEFINER=`dbaguy`@`%`"
    try:
        with scratch_database("mysql", "dumpcat") as (name, admin), \
                scratch_database("mysql", "restore") as (target, _):
            admin.run_in(
                name,
                "SET NAMES utf8mb4 COLLATE utf8mb4_bin",
                f"CREATE {d} FUNCTION f_bin() RETURNS INT DETERMINISTIC RETURN 'a' = 'A'",
                f"CREATE {d} VIEW v_bin AS SELECT 'a' = 'A' AS same",
                "CREATE TABLE tb (id INT PRIMARY KEY, s VARCHAR(10))",
                f"CREATE {d} TRIGGER tb_bi BEFORE INSERT ON tb FOR EACH ROW SET NEW.s = IF('x' = 'X', 'same', 'apart')",
                f"CREATE {d} EVENT ev_bin ON SCHEDULE EVERY 1 DAY STARTS '2030-01-01' DISABLE DO SELECT 'a' = 'A'",
            )
            found = mysql_catalog(db, name)
            binary = "SESSION character_set_client = 'utf8mb4', SESSION collation_connection = 'utf8mb4_bin'"
            assert all(binary in item.statements[0] for item in found.post), [i.statements[0] for i in found.post]
            assert not [warning for warning in found.warnings if "default" in warning]  # a utf8mb4 database
            mysql_restored(db, found, name, target, tmp_path)
            assert mysql_inventory.differences(mysql_inventory.inventory(name), mysql_inventory.inventory(target)) == []
            queries = ("SELECT f_bin()", "SELECT * FROM v_bin", "INSERT INTO tb VALUES (1, 'x')", "SELECT s FROM tb")
            assert behaviour(target, *queries) == behaviour(name, *queries) == [[(0,)], [(0,)], None, [("apart",)]]
        with scratch_database("mysql", "dumpcat") as (name, admin), \
                scratch_database("mysql", "restore") as (target, _):
            admin.run_in(name, f"ALTER DATABASE `{name}` CHARACTER SET latin1 COLLATE latin1_swedish_ci",
                         f"CREATE {d} FUNCTION f_len(s VARCHAR(10)) RETURNS INT DETERMINISTIC RETURN LENGTH(s)")
            latin1 = mysql_inventory.root(name, charset="latin1")  # a latin1 client sends 'é' as one byte
            try:
                mysql_inventory.run(latin1, f"CREATE {d} FUNCTION f_e() RETURNS INT DETERMINISTIC RETURN LENGTH('é')")
            finally:
                latin1.close()
            found = mysql_catalog(db, name)
            assert found.warnings == (
                "This database's default is latin1 (latin1_swedish_ci), not utf8mb4 (utf8mb4_0900_ai_ci): its stored "
                "programs' parameters, return values and variables without a character set of their own take the "
                "default of the database the file is run in, so run it in one created with CHARACTER SET latin1 "
                "COLLATE latin1_swedish_ci",)
            assert program(found, "f_e").statements[0].endswith(
                "SESSION character_set_client = 'utf8mb4', SESSION collation_connection = 'latin1_swedish_ci'")
            admin.run_in(target, f"ALTER DATABASE `{target}` CHARACTER SET latin1 COLLATE latin1_swedish_ci")
            mysql_restored(db, found, name, target, tmp_path)
            source, restored = mysql_inventory.inventory(name), mysql_inventory.inventory(target)
            # the one difference: f_e's client character set (utf8mb4, not latin1); its definition and meaning hold
            assert [row[0] for row in source["routines"] if row not in restored["routines"]] == ["f_e"]
            assert [row[14] for row in source["routines"]] == ["latin1", "utf8mb4"]
            assert [row[14] for row in restored["routines"]] == ["utf8mb4", "utf8mb4"]
            source["routines"], restored["routines"] = (
                [row[:14] + row[15:] for row in rows] for rows in (source["routines"], restored["routines"]))
            assert mysql_inventory.differences(source, restored) == []
            assert behaviour(target, "SELECT f_len('é'), f_e()") == behaviour(name, "SELECT f_len('é'), f_e()") == [
                [(1, 1)]]
    finally:
        db.close()


@requires_mysql
def test_a_mysql_catalog_reads_every_tables_columns_in_one_query(tmp_path):
    """T7 (Q1): a fixed number of catalog queries, plus one SHOW CREATE per object with definitions; none per table
    for a plan (the review measured 2,230 statements for 1,000 tables, one COLUMNS query each)."""
    db = scratch_db("mysql", tmp_path)
    try:
        with scratch_database("mysql", "dumpcat") as (name, admin):
            admin.run_in(name, *(f"CREATE TABLE t{i:02} (id INT PRIMARY KEY, v VARCHAR(10))" for i in range(40)),
                         *(f"CREATE DEFINER=`dbaguy`@`%` VIEW v{i} AS SELECT id FROM t{i:02}" for i in range(4)),
                         *(f"CREATE DEFINER=`dbaguy`@`%` TRIGGER tr{i} BEFORE INSERT ON t{i:02} FOR EACH ROW "
                           "SET NEW.v = NEW.v" for i in range(4)),
                         "CREATE DEFINER=`dbaguy`@`%` PROCEDURE p() SELECT 1")
            scope = DumpScope(kind="schema", schema_name=name)
            sent = {}
            for ddl in (True, False):
                conn = db.open(database=name)
                try:
                    for statement in conn.dump_session(scope).setup if ddl else ():
                        conn.run(statement).close()
                    before = questions(conn)
                    found = conn.dump_catalog(scope, DumpObjectKinds(), ddl=ddl)
                    sent[ddl] = questions(conn) - before - 1  # less the counter's own SHOW
                finally:
                    conn.close()
                assert len(found.tables) == 40 and all(table.columns for table in found.tables)
            objects = 40 + 4 + 4 + 1
            assert sent[False] <= 12 and sent[True] <= sent[False] + objects, sent
    finally:
        db.close()


def questions(conn) -> int:
    """The statements this connection has sent (the server's own counter)."""
    result = conn.run("SHOW SESSION STATUS LIKE 'Questions'")
    try:
        return int(result.fetch(1)[0][1])
    finally:
        result.close()


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


def sqlite_restored(source: Path, found: DumpCatalog, tmp_path: Path, mode: str = "all_or_nothing") -> Path:
    """`found` as a `drop_first` file with `source`'s rows, run twice into a new file through Run SQL file."""
    target = tmp_path / f"target-{mode}.db"
    sqlite3.connect(target).close()
    path = tmp_path / "lite.sql"
    path.write_text(script("sqlite", found, sqlite_rows(source, found)), encoding="utf-8")
    app = DataApp(tmp_path)
    try:
        restore(app, {"profile": {"engine": "sqlite", "name": "target", "path": str(target)}}, path, mode)
    finally:
        app.close()
    return target


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
        target = sqlite_restored(db.file, found, tmp_path)
        again = sqlite_catalog(target)
        assert same(again) == same(found) and again.not_included == ("owners and privileges",)
        assert sqlite_inventory.differences(sqlite_inventory.inventory(db.file), sqlite_inventory.inventory(target)) == []
        named = sqlite_catalog(db.file, DumpScope(kind="tables", schema_name="main", names=["kid", "labels"]))
        assert listed(named) == [("table", "kid"), ("view", "labels"), ("trigger", "labels_add"), ("trigger", "kid_ai")]
        assert named.warnings == (JOURNAL_WARNING, "A foreign key of kid references mom, which is not in this dump: "
                                  "the file keeps it, and needs that table where it is run")
        assert named.not_included == ("owners and privileges",)  # a tables scope: no settings, nothing else counted
    finally:
        db.close()


@pytest.mark.parametrize("mode", ["all_or_nothing", "statements"])
def test_a_sqlite_dump_leaves_out_what_uses_a_virtual_table_and_says_so(tmp_path, mode):
    """Review T1: SQLite's external-content FTS pattern — a trigger that keeps the index, a view over it, a view and
    an INSTEAD OF trigger on that view — and a view over SQLite's statistics. The virtual tables are not written, so
    neither is what uses them (it would restore and then fail every INSERT): left out, counted, named."""
    db = scratch_db("sqlite", tmp_path)
    db.execute(
        "CREATE TABLE docs (id INTEGER PRIMARY KEY, body TEXT)",
        "CREATE VIRTUAL TABLE docs_fts USING fts5(body, content='docs', content_rowid='id')",
        "CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN INSERT INTO docs_fts (rowid, body) VALUES (new.id, "
        "new.body); END",
        "CREATE VIEW hits AS SELECT rowid FROM \"docs_fts\" WHERE docs_fts MATCH 'hello'",
        "CREATE VIEW recent_hits AS SELECT rowid FROM hits",  # built on a view that is left out
        "CREATE TRIGGER hits_add INSTEAD OF INSERT ON hits BEGIN SELECT 1; END",  # on a view that is left out
        "CREATE VIRTUAL TABLE boxes USING rtree(id, minx, maxx)",
        "CREATE VIEW sizes AS SELECT tbl, stat FROM sqlite_stat1",
        "CREATE VIEW plain AS SELECT id FROM docs",
        "CREATE TRIGGER docs_bu BEFORE UPDATE ON docs BEGIN SELECT 1; END",  # names no left-out table: written
        "INSERT INTO docs (body) VALUES ('hello world')",
        "INSERT INTO boxes VALUES (1, 0, 1)",
        "ANALYZE",
    )
    try:
        found = sqlite_catalog(db.file)
        assert listed(found) == [("table", "docs"), ("view", "plain"), ("trigger", "docs_bu")]
        assert found.not_included == (
            "owners and privileges", "2 virtual tables", "7 internal tables of virtual tables",
            "2 SQLite statistics tables (ANALYZE)",
            "5 views and triggers that use a virtual table, SQLite's statistics or a view the dump leaves out")
        assert found.warnings == (
            JOURNAL_WARNING, "Not written, they use a virtual table (or its internal tables), SQLite's statistics or a "
            "view the dump leaves out: view hits, view recent_hits, view sizes, trigger docs_ai, trigger hits_add")
        plan = sqlite_catalog(db.file, ddl=False)
        assert (listed(plan), plan.not_included, plan.warnings) == (listed(found), found.not_included, found.warnings)
        no_views = sqlite_catalog(db.file, kinds=DumpObjectKinds(views=False))  # a trigger on a view goes with it
        assert listed(no_views) == [("table", "docs"), ("trigger", "docs_bu")]
        assert no_views.not_included[-3:] == (
            "4 views (not chosen)", "1 triggers on views not chosen",
            "1 views and triggers that use a virtual table, SQLite's statistics or a view the dump leaves out")
        target = sqlite_restored(db.file, found, tmp_path, mode)
        source = sqlite_inventory.inventory(db.file)
        left = {"docs_ai", "hits", "recent_hits", "hits_add", "sizes"}
        source["master"] = [row for row in source["master"] if row[1] not in left]
        assert sqlite_inventory.differences(source, sqlite_inventory.inventory(target)) == []
        conn = sqlite3.connect(target, isolation_level=None)
        try:
            conn.execute("INSERT INTO docs (body) VALUES ('hello again')")  # nothing written needs what is missing
            assert conn.execute("SELECT COUNT(*) FROM plain").fetchone() == (2,)
        finally:
            conn.close()
    finally:
        db.close()
