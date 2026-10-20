import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import sqlglot
from loguru import logger
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect as SqlglotDialect

from dbaguy_backend.domain.classification import Classification, DangerReason
from dbaguy_backend.sql import classifier
from dbaguy_backend.sql.classifier import StatementKind as K
from dbaguy_backend.sql.classifier import _from_sqlglot, classify, is_transaction_statement, warm_up
from dbaguy_backend.sql.dialects import KEYWORD_ONLY_DIALECTS, Dialect
from dbaguy_backend.sql.splitter import StatementSpan

PG, LITE, MY = Dialect.POSTGRES, Dialect.SQLITE, Dialect.MYSQL


@pytest.mark.parametrize(
    ("sql", "kind", "reasons"),
    [
        ("SELECT * FROM t WHERE a = 1", K.READ, []),
        ("WITH x AS (SELECT 1) SELECT * FROM x", K.READ, []),
        ("VALUES (1), (2)", K.READ, []),
        ("SHOW search_path", K.READ, []),
        ("EXPLAIN SELECT 1", K.READ, []),
        ("EXPLAIN ANALYZE DELETE FROM t", K.WRITE, ["delete_without_where"]),
        ("EXPLAIN ANALYZE SELECT 1", K.READ, []),
        ("EXPLAIN (ANALYZE, BUFFERS) UPDATE t SET a = 1 WHERE id = 1", K.WRITE, []),
        ("EXPLAIN ANALYSE DELETE FROM t", K.WRITE, ["delete_without_where"]),
        ("explain (analyse) insert into t values (1)", K.WRITE, []),
        ("EXPLAIN ANALYZE EXPLAIN ANALYZE DELETE FROM t", K.WRITE, ["delete_without_where"]),
        ("SELECT * FROM t FOR UPDATE", K.WRITE, []),
        ("SELECT * FROM t FOR SHARE", K.WRITE, []),
        ("SELECT * INTO n FROM t", K.DDL, []),
        ("WITH d AS (DELETE FROM t WHERE id = 1 RETURNING *) SELECT * FROM d", K.WRITE, []),
        ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", K.WRITE, ["delete_without_where"]),
        ("WITH u AS (UPDATE t SET a = 1 RETURNING *) INSERT INTO l SELECT * FROM u", K.WRITE, ["update_without_where"]),
        ("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE", K.WRITE, []),
        ("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = s.a", K.WRITE, []),
        (
            "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = s.a"
            " WHEN NOT MATCHED THEN INSERT (a) VALUES (s.a)",
            K.WRITE,
            [],
        ),
        ("WITH x AS (SELECT * FROM t FOR UPDATE) SELECT * FROM x", K.WRITE, []),
        ("SELECT * FROM (SELECT * FROM t FOR UPDATE) s", K.WRITE, []),
        ("SELECT 1; DROP TABLE t", K.DDL, ["drop"]),
        ("SELECT 1; DELETE FROM t", K.WRITE, ["delete_without_where"]),
        ("DELETE FROM t WHERE id = 1; DELETE FROM u", K.WRITE, ["delete_without_where"]),
        ("SELECT 1; frobnicate", K.UNKNOWN, []),
        ("BEGIN; UPDATE t SET a = 1; COMMIT", K.WRITE, ["update_without_where"]),
        ("SET x = 1; SELECT 1", K.SESSION, []),
        ("DROP TABLE a; DELETE FROM t; DROP TABLE b; TRUNCATE t", K.DDL, ["drop", "delete_without_where", "truncate"]),
        ("SELECT 1;", K.READ, []),
        ("INSERT INTO t VALUES (1) ON CONFLICT (id) DO UPDATE SET a = 1", K.WRITE, []),
        ("INSERT INTO t VALUES (1)", K.WRITE, []),
        ("UPDATE t SET a = 1", K.WRITE, ["update_without_where"]),
        ("UPDATE t SET a = 1 WHERE id = 2", K.WRITE, []),
        ("DELETE FROM t", K.WRITE, ["delete_without_where"]),
        ("DELETE FROM t WHERE id = 1", K.WRITE, []),
        ("DELETE FROM t;", K.WRITE, ["delete_without_where"]),
        ("-- cleanup\nDROP TABLE t", K.DDL, ["drop"]),
        ("DROP TABLE t", K.DDL, ["drop"]),
        ("TRUNCATE t", K.DDL, ["truncate"]),
        ("ALTER TABLE t DROP COLUMN a", K.DDL, ["alter_drop"]),
        ("ALTER TABLE t ADD COLUMN a int", K.DDL, []),
        ("CREATE INDEX i ON t (a)", K.DDL, []),
        ("CREATE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql", K.DDL, []),
        ("VACUUM t", K.DDL, []),
        ("BEGIN", K.TRANSACTION, []),
        ("COMMIT", K.TRANSACTION, []),
        ("ROLLBACK", K.TRANSACTION, []),
        ("BEGIN READ WRITE", K.TRANSACTION, []),
        ("START TRANSACTION READ WRITE", K.TRANSACTION, []),
        ("SET search_path TO public", K.SESSION, []),
        ("SET default_transaction_read_only = off", K.SESSION, []),
        ("RESET ALL", K.SESSION, []),
        ("DISCARD ALL", K.SESSION, []),
        ("DO $$ BEGIN PERFORM 1; END $$", K.WRITE, []),
        ("COPY t FROM STDIN", K.WRITE, []),
        ("frobnicate the widgets", K.UNKNOWN, []),
        ("frobnicate", K.UNKNOWN, []),
    ],
)
def test_postgres(sql, kind, reasons):
    result = classify(sql, PG)
    assert result.kind == kind
    assert result.dangerous_reasons == reasons


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("PRAGMA table_info(t)", K.READ),
        ("PRAGMA table_info('a=b')", K.READ),
        ("PRAGMA journal_mode = WAL", K.SESSION),
        ("ATTACH DATABASE 'x.db' AS x", K.SESSION),
        ("REPLACE INTO t VALUES (1)", K.WRITE),
        ("SELECT 1", K.READ),
        ("EXPLAIN QUERY PLAN DELETE FROM t", K.READ),
    ],
)
def test_sqlite(sql, kind):
    assert classify(sql, LITE).kind == kind


@pytest.mark.parametrize(("sql", "kind", "reasons"), [
    ("SELECT a INTO @x FROM t", K.READ, []),
    ("SELECT a, b INTO @x, @y FROM t WHERE id = 1", K.READ, []),
    ("SELECT * FROM t INTO OUTFILE '/tmp/x'", K.WRITE, []),
    ("SELECT * INTO DUMPFILE '/tmp/x' FROM t", K.WRITE, []),
    ("SELECT * FROM t LOCK IN SHARE MODE", K.WRITE, []),
    ("LOAD DATA INFILE '/x' INTO TABLE t", K.WRITE, []),
    ("LOAD XML INFILE '/x' INTO TABLE t", K.WRITE, []),
    ("HANDLER t OPEN", K.WRITE, []),
    ("DO SLEEP(1)", K.WRITE, []),
    ("REPLACE INTO t VALUES (1)", K.WRITE, []),
    ("CALL p(1)", K.WRITE, []),
    ("/*!40101 SET NAMES utf8 */", K.UNKNOWN, []),
    ("SELECT 1 /*! , (SELECT 1 FROM t FOR UPDATE) */", K.UNKNOWN, []),
    ("/*!50000 DELETE FROM t */", K.UNKNOWN, []),
    ("SELECT /*+ MAX_EXECUTION_TIME(1000) */ * FROM t", K.READ, []),
    ("SELECT 'x\\'; DROP TABLE t; -- '", K.READ, []),
    ("SHOW CREATE TABLE t", K.READ, []),
    ("TABLE t", K.READ, []),
    ("UPDATE t SET a = 1", K.WRITE, ["update_without_where"]),
    ("DELETE t FROM t JOIN u ON t.id = u.id", K.WRITE, ["delete_without_where"]),
    ("ALTER TABLE t DROP COLUMN a", K.DDL, ["alter_drop"]),
    ("CREATE DEFINER=`root`@`%` PROCEDURE p() BEGIN DELETE FROM t; DROP TABLE u; END", K.DDL, []),
    ("EXPLAIN ANALYZE FORMAT=TREE DELETE FROM t", K.WRITE, ["delete_without_where"]),
    ("EXPLAIN FORMAT=JSON SELECT 1", K.READ, []),
    ("SET SESSION TRANSACTION READ WRITE", K.SESSION, []),
    ("SET NAMES utf8mb4", K.SESSION, []),
    ("USE analytics", K.SESSION, []),
    ("START TRANSACTION READ ONLY", K.TRANSACTION, []),
    ("FLUSH TABLES", K.UNKNOWN, []),
    ("SELECT 1; DELETE FROM t", K.WRITE, ["delete_without_where"]),
    # Optimizer hints are not code for the server (plan Ruling 3): the statement is judged as if they were not there.
    ("UPDATE /*+ NO_MERGE(t) */ t SET a = 1", K.WRITE, ["update_without_where"]),
    ("INSERT /*+ SET_VAR(sort_buffer_size = 16M) */ INTO t VALUES (1)", K.WRITE, []),
    ("DELETE /*+ MAX_EXECUTION_TIME(1000) */ FROM t WHERE id = 1", K.WRITE, []),
    # A version comment is code whether or not sqlglot parses the rest, and wherever it appears.
    ("/*!50000 DROP TABLE t */ SELECT 1", K.UNKNOWN, []),
    ("SELECT 1 /*!50000 , 2 */", K.UNKNOWN, []),
    ("SELECT 1; /*!50000 DELETE FROM t */", K.UNKNOWN, []),
    ("SELECT 1 /*! unterminated", K.UNKNOWN, []),
    ("EXPLAIN /*!50000 ANALYZE */ DELETE FROM t", K.UNKNOWN, []),
    # MariaDB runs `/*M! … */` as code too (M4).
    ("/*M!DELETE FROM t WHERE*/ (SELECT 1)", K.UNKNOWN, []),
    ("/*M!100100 DELETE FROM t */ SELECT 1", K.UNKNOWN, []),
    ("SELECT 1 /*M! , 2 */", K.UNKNOWN, []),
    ("SELECT 1 /*m! , 2 */", K.READ, []),  # the prefix is case-sensitive, as in the server
    # RESET acts on the server in MySQL, never on the session (M8).
    ("RESET MASTER", K.UNKNOWN, []),
    ("RESET BINARY LOGS AND GTIDS", K.UNKNOWN, []),
    ("RESET REPLICA", K.UNKNOWN, []),
    ("RESET PERSIST", K.UNKNOWN, []),
    # INTO OUTFILE follows more than SELECT.
    ("TABLE t INTO OUTFILE '/tmp/x'", K.WRITE, []),
    ("VALUES ROW(1) INTO DUMPFILE '/tmp/x'", K.WRITE, []),
    ("(SELECT 1) INTO OUTFILE '/tmp/x'", K.WRITE, []),
    ("WITH c AS (SELECT 1) SELECT * FROM c INTO OUTFILE '/tmp/x'", K.WRITE, []),
    ("SELECT 1 INTO @a; SELECT 2 INTO OUTFILE '/tmp/x'", K.WRITE, []),
    # DESCRIBE and DESC are EXPLAIN's synonyms (the statement they analyse really runs); `DESC t` is a plain read.
    ("DESCRIBE ANALYZE DELETE FROM t", K.WRITE, ["delete_without_where"]),
    ("DESC ANALYZE DELETE FROM t", K.WRITE, ["delete_without_where"]),
    ("DESCRIBE ANALYZE FORMAT=TREE DELETE t FROM t JOIN u ON t.id = u.id", K.WRITE, ["delete_without_where"]),
    ("DESC FORMAT=TREE ANALYZE UPDATE t SET a = 1", K.WRITE, ["update_without_where"]),
    ("DESCRIBE ANALYZE SELECT 1", K.READ, []),
    ("DESCRIBE SELECT 1", K.READ, []),
    ("DESCRIBE t", K.READ, []),
    ("DESC t", K.READ, []),
    ("DESC t col", K.READ, []),
    ("DESCRIBE `analyze`", K.READ, []),
    # A `/*+ … */` hint is a plain comment between the words of EXPLAIN; a `/*!` is code and still decides.
    ("EXPLAIN /*+ x */ ANALYZE DELETE FROM t", K.WRITE, ["delete_without_where"]),
    ("EXPLAIN FORMAT=TREE /*+ x */ ANALYZE DELETE FROM t", K.WRITE, ["delete_without_where"]),
    ("EXPLAIN ANALYZE /*+ x */ DELETE FROM t", K.WRITE, ["delete_without_where"]),
    ("/*+ x */ EXPLAIN ANALYZE DELETE FROM t", K.WRITE, ["delete_without_where"]),
    ("DESCRIBE /*+ x */ ANALYZE DELETE FROM t", K.WRITE, ["delete_without_where"]),
    ("EXPLAIN /*+ x */ SELECT 1", K.READ, []),
    ("EXPLAIN FORMAT=TREE /*!50000 ANALYZE */ DELETE FROM t", K.UNKNOWN, []),
    # Only these start a transaction statement in MySQL; any other START (REPLICA, SLAVE, GROUP_REPLICATION, …) and
    # the PostgreSQL/SQLite spellings END and ABORT are unknown (R-03a-T4), whichever path (sqlglot, keywords) saw them.
    ("START REPLICA", K.UNKNOWN, []),
    ("START SLAVE", K.UNKNOWN, []),
    ("START GROUP_REPLICATION", K.UNKNOWN, []),
    ("START REPLICA UNTIL SOURCE_LOG_FILE = 'x', SOURCE_LOG_POS = 4", K.UNKNOWN, []),
    ("START REPLICA IO_THREAD FOR CHANNEL 'c'", K.UNKNOWN, []),
    ("/* c */ start replica", K.UNKNOWN, []),
    ("START", K.UNKNOWN, []),
    ("END", K.UNKNOWN, []),
    ("ABORT", K.UNKNOWN, []),
    ("RELEASE a", K.UNKNOWN, []),
    ("XA START 'a'", K.UNKNOWN, []),
    ("BEGIN", K.TRANSACTION, []),
    ("BEGIN WORK", K.TRANSACTION, []),
    ("START TRANSACTION", K.TRANSACTION, []),
    ("start transaction", K.TRANSACTION, []),
    ("START TRANSACTION READ ONLY", K.TRANSACTION, []),
    ("START TRANSACTION READ WRITE", K.TRANSACTION, []),
    ("START TRANSACTION WITH CONSISTENT SNAPSHOT", K.TRANSACTION, []),
    ("/* c */ BEGIN", K.TRANSACTION, []),
    ("COMMIT", K.TRANSACTION, []),
    ("COMMIT AND CHAIN", K.TRANSACTION, []),
    ("ROLLBACK", K.TRANSACTION, []),
    ("ROLLBACK TO SAVEPOINT a", K.TRANSACTION, []),
    ("SAVEPOINT a", K.TRANSACTION, []),
    ("RELEASE SAVEPOINT a", K.TRANSACTION, []),
    # A SET whose value takes row locks is a locking read, like any SELECT … FOR UPDATE.
    ("SET @x = (SELECT id FROM t FOR UPDATE)", K.WRITE, []),
    ("SET @x := (SELECT id FROM t LOCK IN SHARE MODE)", K.WRITE, []),
    ("SET @x = (SELECT id FROM t FOR SHARE), @y = 1", K.WRITE, []),
    ("SET @x = (SELECT id FROM t FOR UPDATE SKIP LOCKED)", K.WRITE, []),
    ("SET @x = (SELECT MAX(id) FROM t), @y = 2", K.SESSION, []),
    ("SET @x = INSERT('abc', 1, 2, 'x')", K.SESSION, []),
    ("SET autocommit = 0", K.SESSION, []),
    # Text that only looks like one of the rules.
    ("SELECT 'INTO OUTFILE', 'LOCK IN SHARE MODE' FROM t", K.READ, []),
    ("SELECT 1 /* INTO OUTFILE */", K.READ, []),
    ("SELECT `into`, `outfile` FROM t", K.READ, []),
    ("SELECT 1 -- /*!50000 DROP TABLE t */", K.READ, []),
    ("SELECT '/*!50000 DROP TABLE t */'", K.READ, []),
])
def test_mysql(sql, kind, reasons):
    result = classify(sql, MY)
    assert (result.kind, list(result.dangerous_reasons)) == (kind, reasons)


# R-03a-F1: dropping or emptying partitions, and the primary key, are as destructive as DROP INDEX.
@pytest.mark.parametrize(("sql", "reasons"), [
    ("ALTER TABLE t DROP PARTITION p0", ["alter_drop"]),  # sqlglot 30.23: ParseError, so the keyword rules
    ("ALTER TABLE t DROP PARTITION p0, p1", ["alter_drop"]),
    ("alter table t drop partition p0", ["alter_drop"]),
    ("ALTER TABLE t TRUNCATE PARTITION p0", ["truncate"]),  # sqlglot 30.23: a Command
    ("ALTER TABLE t TRUNCATE PARTITION ALL", ["truncate"]),
    ("ALTER TABLE t DROP PRIMARY KEY", ["alter_drop"]),  # sqlglot: DropPrimaryKey, which is not an exp.Drop
    ("ALTER TABLE t ADD COLUMN a int, DROP PRIMARY KEY", ["alter_drop"]),
    ("ALTER TABLE t DROP INDEX i", ["alter_drop"]),
    ("ALTER TABLE t DROP FOREIGN KEY fk", ["alter_drop"]),
    ("ALTER TABLE t DROP CHECK c", ["alter_drop"]),
    ("ALTER TABLE t ALTER COLUMN a DROP DEFAULT", []),
    ("ALTER TABLE t ADD PARTITION (PARTITION p3 VALUES LESS THAN (2030))", []),
    ("ALTER TABLE t REORGANIZE PARTITION p0 INTO (PARTITION p1 VALUES LESS THAN (10))", []),
    ("ALTER TABLE t COMMENT = 'drop partition'", []),
    ("ALTER TABLE `drop` ADD COLUMN `partition` int", []),
])
def test_mysql_alters_that_lose_data_are_dangerous(sql, reasons):
    result = classify(sql, MY)
    assert (result.kind, list(result.dangerous_reasons)) == (K.DDL, reasons)


@pytest.mark.parametrize(("sql", "reasons"), [
    ("ALTER TABLE t DROP PARTITION p0", ["alter_drop"]), ("ALTER TABLE t TRUNCATE PARTITION p0", ["truncate"]),
    ("ALTER TABLE t DROP PRIMARY KEY", ["alter_drop"]),
])
@pytest.mark.parametrize("sqlglot_says", [None, [Classification(kind=K.DDL)]], ids=["keyword rules", "sqlglot"])
def test_alter_drop_reasons_hold_whichever_rules_decide(sql, reasons, sqlglot_says, monkeypatch):
    # The keyword rules (sqlglot cannot parse it) or a syntax tree that says DDL without the reason (a future sqlglot
    # parsing the partition forms into nodes the tree rules do not know): the reason is there either way.
    monkeypatch.setattr(classifier, "_from_sqlglot", lambda text, dialect: sqlglot_says)
    assert classify(sql, MY).dangerous_reasons == reasons


def test_the_tree_rules_count_a_dropped_primary_key():
    from dbaguy_backend.sql import sqlglot_rules

    [node] = sqlglot_rules.parse("ALTER TABLE t DROP PRIMARY KEY", MY)
    assert sqlglot_rules.classify_expression(node, MY).dangerous_reasons == [DangerReason.ALTER_DROP]


def test_reset_stays_a_session_statement_elsewhere():
    assert classify("RESET ALL", PG).kind == K.SESSION
    assert classify("RESET search_path", PG).kind == K.SESSION


def test_select_into_still_creates_a_table_in_postgres():
    assert classify("SELECT * INTO n FROM t", PG).kind == K.DDL
    assert classify("SELECT * INTO n FROM t", LITE).kind == K.DDL


def test_select_into_creates_a_table_in_tsql():  # INTO_CREATES_A_TABLE: SELECT … INTO #t makes a temp table
    assert classify("SELECT * INTO #t FROM a", Dialect.TSQL) == Classification(kind=K.DDL)
    assert classify("SELECT a INTO dbo.b FROM a", Dialect.TSQL).kind == K.DDL


def test_load_and_handler_mean_a_write_only_in_mysql():
    assert classify("LOAD 'plpgsql'", PG).kind == K.UNKNOWN  # PostgreSQL's LOAD loads a library
    assert classify("HANDLER t OPEN", PG).kind == K.UNKNOWN


@pytest.mark.parametrize("dialect", [PG, LITE])
def test_a_mysql_version_comment_is_just_a_comment_elsewhere(dialect):
    assert classify("/*!50000 DROP TABLE t */ SELECT 1", dialect).kind == K.READ


@pytest.mark.parametrize("sql", [
    "SELECT * FROM t INTO OUTFILE '/tmp/x'", "SELECT * INTO DUMPFILE '/tmp/x' FROM t",
    "SELECT 1 INTO /* c */ OUTFILE '/tmp/x'", "select * from t into\n  outfile '/tmp/x'",
])
def test_into_outfile_is_a_write_even_when_sqlglot_parses_it(sql, monkeypatch):
    # R-03a-P7: sqlglot 30.23 cannot parse it, but a sqlglot that does (as exp.Into, which MySQL's rules ignore)
    # must not turn it into a read.
    monkeypatch.setattr(classifier, "_from_sqlglot", lambda text, dialect: [Classification(kind=K.READ)])
    assert classify(sql, MY).kind == K.WRITE


@pytest.mark.parametrize(("sql", "dialect"), [
    ("START TRANSACTION", PG), ("BEGIN", PG), ("END", PG), ("ABORT", PG), ("RELEASE a", PG), ("SAVEPOINT a", PG),
    ("START TRANSACTION", LITE), ("BEGIN", LITE), ("END", LITE), ("RELEASE a", LITE), ("SAVEPOINT a", LITE),
])
def test_the_mysql_transaction_starts_do_not_narrow_the_other_dialects(sql, dialect):
    assert classify(sql, dialect).kind == K.TRANSACTION


def test_a_start_that_is_not_a_transaction_hides_nowhere_in_one_span(monkeypatch):
    # Defence in depth: the leading words vouch only for the first statement of a span the splitter failed to cut.
    monkeypatch.setattr(classifier, "split", lambda sql, dialect: [StatementSpan(0, len(sql), len(sql), sql)])
    assert classify("START TRANSACTION; START REPLICA", MY).kind == K.UNKNOWN
    assert classify("SELECT 1; START REPLICA", MY).kind == K.UNKNOWN
    assert classify("BEGIN; SELECT 1", PG).kind == K.TRANSACTION  # no restriction outside MySQL


def test_a_routine_that_writes_a_file_is_still_a_ddl_statement():
    # The kind decides whether the schema is refreshed afterwards: CREATE stays DDL.
    assert classify("CREATE PROCEDURE p() SELECT * FROM t INTO OUTFILE '/tmp/x'", MY).kind == K.DDL
    assert classify("CREATE PROCEDURE p() BEGIN SELECT * FROM t INTO OUTFILE '/tmp/x'; END", MY).kind == K.DDL


def test_never_raises_on_garbage():
    for sql in ["", "   ", "SELECT (", "'", "-- only comment", "DELETE FROM"]:
        classify(sql, PG)


def test_survives_nesting_too_deep_for_the_parser():
    deep = "SELECT " + "(" * 5000 + "1" + ")" * 5000
    assert classify(deep, PG).kind == K.READ


def test_sqlglot_does_not_log_statement_text(caplog):
    with caplog.at_level(logging.DEBUG):
        assert classify("CALL secret_proc('hunter2')", PG).kind == K.WRITE  # sqlglot parses CALL as a Command
    assert "hunter2" not in caplog.text


# ---- sqlglot: dialects resolved by explicit import, failures visible (packaged app) ----

def _warnings_while(action):
    messages = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    try:
        result = action()
    finally:
        logger.remove(sink)
    return result, messages


@pytest.mark.parametrize("dialect", [d for d in Dialect if d not in KEYWORD_ONLY_DIALECTS])
def test_every_dialect_parses_through_sqlglot_resolved_by_import(dialect, monkeypatch):
    calls = []
    real = sqlglot.parse

    def spy(sql, read=None, **opts):
        parsed = real(sql, read=read, **opts)
        calls.append((read, parsed))
        return parsed

    monkeypatch.setattr(sqlglot, "parse", spy)
    assert classify("SELECT 1", dialect).kind == K.READ
    [(read, parsed)] = calls
    # A sqlglot dialect class, not a name: sqlglot looks names up with importlib, which PyInstaller cannot follow.
    assert isinstance(read, type) and issubclass(read, SqlglotDialect)
    assert [type(e) for e in parsed] == [exp.Select]


def test_dialects_survive_a_bundle_without_sqlglots_by_name_lookups():
    # What the packaged app saw: importlib.import_module("sqlglot.dialects.postgres") raised ModuleNotFoundError.
    code = textwrap.dedent("""
        import importlib, json
        real = importlib.import_module
        def bundled_only(name, package=None):
            if name.startswith("sqlglot.dialects.") and name != "sqlglot.dialects.dialect":
                raise ModuleNotFoundError(f"No module named {name!r}")
            return real(name, package)
        importlib.import_module = bundled_only
        from dbaguy_backend.sql.classifier import _from_sqlglot
        from dbaguy_backend.sql.dialects import KEYWORD_ONLY_DIALECTS, Dialect
        # sqlglot's own answer (None when it could not run: a TypeError here); the keyword rules give the same reason
        sql = "ALTER TABLE t DROP COLUMN a"
        dialects = [d for d in Dialect if d not in KEYWORD_ONLY_DIALECTS]
        print(json.dumps([[c.dangerous_reasons for c in _from_sqlglot(sql, d)] for d in dialects]))
    """)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                            cwd=Path(__file__).parents[2])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [[["alter_drop"]]] * (len(Dialect) - len(KEYWORD_ONLY_DIALECTS))  # parsed, not the keyword fallback


def test_keyword_only_dialects_never_reach_sqlglot(monkeypatch):
    monkeypatch.setattr(sqlglot, "parse", lambda *a, **k: pytest.fail("sqlglot was asked about SQL++"))
    result, warnings = _warnings_while(lambda: _from_sqlglot("SELECT 1", Dialect.SQLPP))
    assert result is None and warnings == []


def test_an_unexpected_sqlglot_failure_is_a_warning_naming_only_the_exception(monkeypatch):
    def missing(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'sqlglot.dialects.postgres'")

    monkeypatch.setattr(sqlglot, "parse", missing)
    result, warnings = _warnings_while(lambda: classify("DELETE FROM secret_table", PG))
    assert result.kind == K.WRITE and result.dangerous_reasons == ["delete_without_where"]  # the keyword fallback
    assert len(warnings) == 1 and "ModuleNotFoundError" in warnings[0] and "secret_table" not in warnings[0]


def test_parse_errors_stay_quiet():
    _, warnings = _warnings_while(lambda: classify("SELECT (", PG))
    assert warnings == []


def test_reasons_are_typed_but_stay_plain_strings_on_the_wire():
    result = classify("DROP TABLE a; UPDATE t SET x = 1", PG)
    assert all(isinstance(reason, DangerReason) for reason in result.dangerous_reasons)
    assert result.model_dump(mode="json")["dangerous_reasons"] == ["drop", "update_without_where"]


def test_warm_up_parses_once_per_dialect_and_logs_its_duration(monkeypatch):
    calls = []
    real = sqlglot.parse
    monkeypatch.setattr(sqlglot, "parse", lambda sql, read=None, **o: calls.append(read) or real(sql, read=read, **o))
    messages = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="DEBUG")
    try:
        warm_up()
    finally:
        logger.remove(sink)
    assert len(calls) == len(Dialect) - len(KEYWORD_ONLY_DIALECTS)  # warm_up() skips the keyword-only dialects
    assert any("warmed up" in m and " ms" in m for m in messages)


# ---- statements the splitter must not merge, and sqlglot's view of one span ----

def test_column_names_begin_atomic_or_trigger_cannot_hide_a_statement():
    assert (classify("SELECT started AS begin, atomic FROM jobs; DROP TABLE t", PG).kind,
            classify("SELECT started AS begin, atomic FROM jobs; DROP TABLE t", PG).dangerous_reasons) == (K.DDL, ["drop"])
    result = classify("CREATE TABLE t (trigger int, begin int); DELETE FROM t", LITE)
    assert (result.kind, result.dangerous_reasons) == (K.DDL, ["delete_without_where"])


def test_routine_and_trigger_bodies_stay_one_ddl_statement():
    # sqlglot cuts these at their inner `;`: the body's statements must not count as statements of their own.
    fn = classify("CREATE FUNCTION f() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT 1; DELETE FROM t; END", PG)
    trigger = classify("CREATE TEMP TRIGGER tr AFTER INSERT ON t BEGIN UPDATE u SET a = 1; DELETE FROM v; END", LITE)
    assert (fn.kind, fn.dangerous_reasons) == (K.DDL, [])
    assert (trigger.kind, trigger.dangerous_reasons) == (K.DDL, [])


def test_several_statements_sqlglot_finds_in_one_span_are_each_classified(monkeypatch):
    # Defence in depth: if the splitter ever merges two statements, the second one still counts.
    monkeypatch.setattr(classifier, "split", lambda sql, dialect: [StatementSpan(0, len(sql), len(sql), sql)])
    result = classify("SELECT 1; DROP TABLE t; UPDATE u SET a = 1", PG)
    assert (result.kind, result.dangerous_reasons) == (K.DDL, ["drop", "update_without_where"])
    assert classify("SELECT 1; LISTEN x", PG).kind == K.UNKNOWN  # a statement sqlglot cannot judge is not a read


def test_a_single_statement_inside_delimiter_lines_is_classified_without_them():
    routine = "DELIMITER $$\nCREATE PROCEDURE p()\nBEGIN\n  SELECT 1;\n  SELECT 2;\nEND$$\nDELIMITER ;"
    assert classify(routine, MY).kind is K.DDL
    wrapped = "DELIMITER $$\nDELETE FROM t$$\nDELIMITER ;"  # the danger of the statement is not lost to the wrapper
    assert classify(wrapped, MY) == classify("DELETE FROM t", MY)
    assert classify(wrapped, MY).dangerous_reasons == [DangerReason.DELETE_WITHOUT_WHERE]


# ---- T-SQL: every statement of a batch judged, with or without `;` (spec §3.4, plan Ruling 8) ----

TS = Dialect.TSQL


@pytest.mark.parametrize(("sql", "kind", "reasons"), [
    ("DECLARE @x int = 1; SELECT @x", K.READ, []),
    ("DECLARE @x int = 41\nSELECT @x + 1 AS answer", K.READ, []),
    ("DECLARE @t TABLE (a int)", K.READ, []),
    ("SET @x = 1", K.READ, []),
    ("SELECT @x = a FROM t", K.READ, []),
    ("PRINT 'hi'", K.READ, []),
    ("RAISERROR('x', 10, 1) WITH NOWAIT", K.READ, []),
    ("THROW 50000, 'x', 1", K.READ, []),
    ("WAITFOR DELAY '00:00:01'", K.READ, []),
    ("IF EXISTS (SELECT 1 FROM t) PRINT 'y' ELSE PRINT 'n'", K.READ, []),
    ("BEGIN TRY SELECT 1/0 END TRY BEGIN CATCH SELECT ERROR_MESSAGE() END CATCH", K.READ, []),
    ("WHILE @i < 3 BEGIN SET @i = @i + 1 END", K.READ, []),
    ("SELECT TOP 10 * FROM t WITH (NOLOCK)", K.READ, []),
    ("SELECT * FROM t FOR XML PATH('')", K.READ, []),
    ("SELECT 'DELETE FROM t' AS s, [update] FROM t", K.READ, []),
    ("EXEC sp_who", K.UNKNOWN, []),
    ("EXECUTE dbo.p @a = 1", K.UNKNOWN, []),
    ("sp_who", K.UNKNOWN, []),
    ("EXEC sp_executesql N'DELETE FROM t'", K.UNKNOWN, []),
    ("SELECT 1; EXEC('DROP TABLE t')", K.UNKNOWN, []),
    ("DBCC CHECKDB", K.UNKNOWN, []),
    ("BACKUP DATABASE d TO DISK = 'x'", K.UNKNOWN, []),
    ("KILL 55", K.UNKNOWN, []),
    ("SHUTDOWN", K.UNKNOWN, []),
    ("BULK INSERT t FROM 'x'", K.WRITE, []),
    ("DECLARE @x int\nDELETE FROM t", K.WRITE, ["delete_without_where"]),
    ("SELECT 1\nDROP TABLE t", K.DDL, ["drop"]),
    ("SELECT 1\nUPDATE t SET a = 1", K.WRITE, ["update_without_where"]),
    ("UPDATE t SET a = 1 WHERE id = 1", K.WRITE, []),
    ("WITH c AS (SELECT * FROM t) DELETE FROM c", K.WRITE, ["delete_without_where"]),
    ("IF @x = 1 DELETE FROM t WHERE id = 1 ELSE UPDATE t SET a = 1", K.WRITE, ["update_without_where"]),
    ("MERGE t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = s.a;", K.WRITE, []),
    ("SELECT * FROM t WITH (UPDLOCK)", K.WRITE, []),
    ("UPDATE STATISTICS t", K.WRITE, []),
    ("SELECT * INTO #t FROM t", K.DDL, []),
    ("CREATE TABLE #t (a int)", K.DDL, []),
    ("TRUNCATE TABLE t", K.DDL, ["truncate"]),
    ("ALTER TABLE t DROP COLUMN a", K.DDL, ["alter_drop"]),
    ("DENY SELECT ON t TO u", K.DDL, []),
    ("GRANT SELECT, INSERT ON t TO u", K.DDL, []),
    ("CREATE PROCEDURE p AS\nBEGIN\n  DELETE FROM t;\n  EXEC q;\nEND", K.DDL, []),
    ("CREATE OR ALTER VIEW v AS SELECT 1 AS a", K.DDL, []),
    ("ALTER PROCEDURE p AS EXEC q", K.DDL, []),
    ("BEGIN TRAN", K.TRANSACTION, []),
    ("SAVE TRANSACTION s", K.TRANSACTION, []),
    ("BEGIN TRANSACTION; UPDATE t SET a = 1 WHERE id = 1; COMMIT", K.WRITE, []),
    ("SET NOCOUNT ON", K.SESSION, []),
    ("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED", K.SESSION, []),
    ("USE master", K.SESSION, []),
    ("SELECT 1\nGO\nDELETE FROM t", K.WRITE, ["delete_without_where"]),  # every batch judged
    # R-03b-P2: the pre-flight's bypasses (each accepted and run by SQL Server 2022)
    ("DECLARE @p varbinary(16) = (SELECT TEXTPTR(c) FROM dbo.zz_text WHERE id = 1)\nWRITETEXT dbo.zz_text.c @p 'new'",
     K.WRITE, []),
    ("DECLARE @p varbinary(16)\nUPDATETEXT dbo.zz_text.c @p 0 NULL 'x'", K.WRITE, []),
    ("SELECT * INTO t2 FROM t OPTION (USE HINT ('FORCE_LEGACY_CARDINALITY_ESTIMATION'))", K.DDL, []),
    ("SELECT * INTO t2 FROM CHANGETABLE(CHANGES t, 0) c", K.DDL, []),
    ("WITH c AS (SELECT 1 AS a) SELECT * INTO t2 FROM c", K.DDL, []),
    ("(SELECT a INTO t2 FROM t) OPTION (USE HINT ('FORCE_LEGACY_CARDINALITY_ESTIMATION'))", K.DDL, []),  # self-check
    ("(SELECT * INTO t2 FROM CHANGETABLE(CHANGES t, 0) c)", K.DDL, []),
    ("IF 1 = 1 (SELECT * INTO t2 FROM CHANGETABLE(CHANGES t, 0) c)", K.DDL, []),  # it created t2
    ("DECLARE @p varbinary(16)\n((SELECT * INTO t2 FROM CHANGETABLE(CHANGES t, 0) c))", K.DDL, []),
    ("BEGIN TRAN\n(SELECT * INTO t2 FROM CHANGETABLE(CHANGES t, 0) c)", K.DDL, []),
    ("SELECT a FROM t WHERE a IN (SELECT b FROM u) OPTION (USE HINT ('X'))", K.READ, []),
    ("FETCH NEXT FROM c INTO @x", K.READ, []),
    ("DECLARE @h uniqueidentifier\nEND CONVERSATION @h", K.UNKNOWN, []),
    ("DECLARE @h uniqueidentifier\nBEGIN DIALOG CONVERSATION @h FROM SERVICE s TO SERVICE 't' ON CONTRACT c", K.UNKNOWN, []),
    ("BEGIN CONVERSATION TIMER (@h) TIMEOUT = 60", K.UNKNOWN, []),
    ("WAITFOR (RECEIVE TOP (1) * FROM dbo.q), TIMEOUT 1000", K.WRITE, []),
    ("DECLARE @h uniqueidentifier\nSEND ON CONVERSATION @h ('x')", K.UNKNOWN, []),
    ("DECLARE @h uniqueidentifier, @g uniqueidentifier\nMOVE CONVERSATION @h TO @g", K.UNKNOWN, []),
    ("SELECT * FROM t WITH (TABLOCKX)", K.WRITE, []),
    ("FETCH NEXT FROM c INTO @x", K.READ, []),
    # R-03b-P3: danger reasons survive statements run together without `;`
    ("SET NOCOUNT ON\nDELETE FROM t", K.WRITE, ["delete_without_where"]),
    ("SET XACT_ABORT ON\nUPDATE t SET a = 1", K.WRITE, ["update_without_where"]),
    ("ALTER TABLE t ADD c int\nDROP TABLE u", K.DDL, ["drop"]),
    ("GRANT SELECT ON t TO u\nDROP TABLE t", K.DDL, ["drop"]),
    ("DENY SELECT ON t TO u\nTRUNCATE TABLE t", K.DDL, ["truncate"]),
    ("REVOKE SELECT ON t FROM u\nDELETE FROM t", K.DDL, ["delete_without_where"]),
    ("WITH c AS (SELECT * FROM t) UPDATE c SET a = 1", K.WRITE, ["update_without_where"]),
    # Found by the self-check, each run by SQL Server 2022 as described.
    ("SELECT throw INTO t2 FROM t", K.DDL, []),  # THROW is not reserved: a column, and the SELECT creates t2
    ("SELECT t.throw INTO t2 FROM t", K.DDL, []),
    ("IF 1 = 1 DISABLE TRIGGER tr ON t", K.UNKNOWN, []),  # it disabled the trigger
    ("WHILE 1 = 0 ENABLE TRIGGER ALL ON t", K.UNKNOWN, []),
    ("SELECT 1\nADD SENSITIVITY CLASSIFICATION TO dbo.t.a WITH (LABEL = 'x')", K.DDL, []),
    ("IF 1 = 1 ADD SIGNATURE TO dbo.p BY CERTIFICATE c", K.DDL, []),
    ("SELECT NEXT VALUE FOR dbo.s", K.WRITE, []),  # it advances the sequence
    ("pragma x", K.UNKNOWN, []),  # a bare name at the start of a batch runs that procedure
    ("show x", K.UNKNOWN, []),
    ("explain x", K.UNKNOWN, []),
    ("describe x", K.UNKNOWN, []),
    ("SELECT 1 AS after\nDELETE FROM t", K.WRITE, ["delete_without_where"]),
    ("ALTER LOGIN l WITH CHECK_POLICY = ON\nDELETE FROM t", K.DDL, ["delete_without_where"]),
    ("ALTER DATABASE CURRENT SET ANSI_NULLS ON\nUPDATE t SET a = 1", K.DDL, ["update_without_where"]),
    ("ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES u (id) ON UPDATE CASCADE\nSET XACT_ABORT ON\n"
     "DELETE FROM t", K.DDL, ["delete_without_where"]),
    ("ALTER TABLE t ADD c int\nDROP ASSEMBLY a", K.DDL, ["drop"]),
    ("ALTER PARTITION FUNCTION pf() MERGE RANGE (1)\nDROP TABLE t", K.DDL, ["drop"]),
    ("SELECT throw, enable, get, move FROM t", K.READ, []),  # names, not statements
    ("BEGIN TRY SELECT 1 END TRY BEGIN CATCH THROW END CATCH", K.READ, []),
    # SQL Server separates tokens at Unicode spaces: both ran (all rows deleted, the table dropped)
    ("SELECT 1\nDELETE\u00a0FROM t", K.WRITE, ["delete_without_where"]),
    ("DECLARE @x int\u3000DROP TABLE u", K.DDL, ["drop"]),
    ("SELECT a FROM t\u200bDELETE FROM t", K.WRITE, ["delete_without_where"]),
    ("WITH\u200bc\u200bAS\u200b(SELECT\u200b*\u200bFROM\u200bt)\u200bDELETE\u200bFROM\u200bc", K.WRITE,
     ["delete_without_where"]),
    # An unfiltered UPDATE/DELETE keeps its reason when sqlglot cannot parse it (OPTION after a CTE, USE HINT), and a
    # WHERE inside a subquery or a CTE filters nothing.
    ("WITH c AS (SELECT * FROM t) DELETE FROM c OPTION (MAXDOP 1)", K.WRITE, ["delete_without_where"]),
    ("WITH c AS (SELECT * FROM t) UPDATE TOP (1) c SET a = 1", K.WRITE, ["update_without_where"]),
    ("UPDATE t SET a = (SELECT MAX(b) FROM u WHERE u.id = t.id) OPTION (MAXDOP 1, USE HINT ('X'))", K.WRITE,
     ["update_without_where"]),
    ("WITH c AS (SELECT * FROM t WHERE a = 1) DELETE FROM c OPTION (MAXDOP 1)", K.WRITE, ["delete_without_where"]),
    ("WITH c AS (SELECT * FROM t) DELETE FROM c WHERE b = 2 OPTION (MAXDOP 1)", K.WRITE, []),
    ("UPDATE t SET a = 1 WHERE id IN (SELECT id FROM u) OPTION (USE HINT ('X'))", K.WRITE, []),
    ("WITH c AS (SELECT * FROM t) SELECT * FROM c WHERE a = 1 OPTION (MAXDOP 1)", K.READ, []),
    # MERGE as a join or union hint is no MERGE statement
    ("SELECT * FROM t INNER MERGE JOIN u ON u.id = t.id", K.READ, []),
    ("SELECT * FROM t JOIN u ON u.id = t.id OPTION (HASH JOIN, MERGE JOIN, MERGE UNION)", K.READ, []),
    # R-03b-T3b: hints that take or keep locks write, as FOR UPDATE / FOR SHARE do in PostgreSQL and MySQL
    ("SELECT * FROM t WITH (HOLDLOCK)", K.WRITE, []),
    ("SELECT * FROM t WITH (NOLOCK, SERIALIZABLE)", K.WRITE, []),
    ("SELECT a FROM t (REPEATABLEREAD)", K.WRITE, []),
    ("SELECT * FROM t WITH (XLOCK, ROWLOCK)", K.WRITE, []),
    ("SET @x = (SELECT a FROM t WITH (HOLDLOCK))", K.WRITE, []),
    ("DECLARE @x int = (SELECT a FROM t WITH (UPDLOCK))", K.WRITE, []),
    ("SELECT * FROM t OPTION (TABLE HINT (t, HOLDLOCK))", K.WRITE, []),
    ("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE", K.SESSION, []),  # an isolation level, not a hint
    ("SELECT * FROM t WITH (INDEX = level SERIALIZABLE)", K.WRITE, []),  # an index named level: still a hint
    ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", K.SESSION, []),
    # R-03b-T3c: Service Broker words are plain names outside their statements
    ("SELECT a FROM t WHERE conversation = 1", K.READ, []),
    ("SELECT send, receive, conversation, dialog FROM t", K.READ, []),
    ("SELECT receive FROM t", K.READ, []),
    ("SELECT * FROM receive WHERE dialog = 1 ORDER BY send", K.READ, []),
    ("IF 1 = 1 RECEIVE TOP (1) * FROM dbo.q", K.UNKNOWN, []),
    ("RECEIVE * FROM dbo.q", K.UNKNOWN, []),
    ("BEGIN CATCH RECEIVE TOP (1) * FROM dbo.q END CATCH", K.UNKNOWN, []),
    ("WAITFOR (GET CONVERSATION GROUP @g FROM dbo.q), TIMEOUT 1000", K.WRITE, []),
    ("GET CONVERSATION GROUP @g FROM dbo.q", K.UNKNOWN, []),
])
def test_tsql(sql, kind, reasons):
    result = classify(sql, TS)
    assert (result.kind, list(result.dangerous_reasons)) == (kind, reasons)


@pytest.mark.parametrize("sql", ["SHOW x", "EXPLAIN SELECT 1", "DESCRIBE t", "PRAGMA x", "START TRANSACTION",
                                 "RESET x", "LISTEN x", "NOTIFY x", "DISCARD ALL", "RELEASE x", "ABORT"])
def test_words_that_start_no_tsql_statement_are_unknown(sql):  # plan Ruling 8: a leading name is an implicit EXEC
    assert classify(sql, TS).kind == K.UNKNOWN


def test_the_tsql_keyword_overrides_do_not_leak_into_the_other_dialects():
    assert classify("SHOW search_path", PG).kind == K.READ
    assert classify("PRAGMA table_info(t)", LITE).kind == K.READ
    assert classify("PRAGMA journal_mode = WAL", LITE).kind == K.SESSION
    assert classify("DESCRIBE t", MY).kind == K.READ


# --- SQL++ (Couchbase): keyword rules only, nothing from sqlglot ---------------------------------------------------

SQ = Dialect.SQLPP
D = DangerReason


@pytest.mark.parametrize("sql, kind, reasons", [
    ("SELECT * FROM users", K.READ, []),
    ("select raw d.name from ks as d use keys ['a']", K.READ, []),
    ("WITH a AS (SELECT 1 AS x) SELECT a", K.READ, []),
    ("(SELECT 1) UNION (SELECT 2)", K.READ, []),
    ("EXPLAIN DELETE FROM users", K.READ, []),
    ("EXPLAIN FUNCTION f", K.READ, []),
    ("ADVISE SELECT * FROM users WHERE name = 'x'", K.READ, []),
    ('INFER users WITH {"sample_size": 10}', K.READ, []),
    ("PREPARE p FROM DELETE FROM users", K.READ, []),
    ("SELECT NEXT VALUE FOR s1", K.WRITE, []),
    ("SELECT nextval for s1", K.WRITE, []),
    ("SELECT PREV VALUE FOR s1", K.READ, []),
    ("SELECT 1 DELETE FROM users", K.WRITE, []),  # invalid for the server; still never a read
    ("INSERT INTO users (KEY, VALUE) VALUES ('k', {})", K.WRITE, []),
    ("UPSERT INTO users (KEY, VALUE) VALUES ('k', {})", K.WRITE, []),
    ("UPDATE users SET a = 1", K.WRITE, [D.UPDATE_WITHOUT_WHERE]),
    ("UPDATE users USE KEYS 'k' SET a = 1", K.WRITE, []),
    ("UPDATE users SET a = 1 WHERE b = 2", K.WRITE, []),
    ("UPDATE users SET a = (SELECT RAW 1 FROM t WHERE x = 1)", K.WRITE, [D.UPDATE_WITHOUT_WHERE]),
    ("DELETE FROM users", K.WRITE, [D.DELETE_WITHOUT_WHERE]),
    ("DELETE FROM users d USE KEYS ['a', 'b']", K.WRITE, []),
    ("DELETE FROM users d WHERE d.k IN (SELECT RAW o.k FROM orders o WHERE o.x = 1)", K.WRITE, []),
    ("MERGE INTO users u USING orders o ON KEY o.u WHEN MATCHED THEN DELETE", K.WRITE, [D.MERGE_DELETE]),
    ("MERGE INTO users u USING orders o ON KEY o.u WHEN MATCHED THEN UPDATE SET u.n = 1", K.WRITE, []),
    ("DELETE FROM system:active_requests WHERE clientContextID = 'x'", K.WRITE, [D.SYSTEM_KEYSPACE]),
    ("DELETE FROM system:prepareds", K.WRITE, [D.DELETE_WITHOUT_WHERE, D.SYSTEM_KEYSPACE]),
    ("SELECT * FROM system:active_requests", K.READ, []),
    ("UPDATE STATISTICS FOR users(name)", K.WRITE, []),
    ("ANALYZE KEYSPACE users", K.WRITE, []),
    ("CREATE INDEX i ON users(name)", K.DDL, []),
    ("CREATE PRIMARY INDEX ON users", K.DDL, []),
    ("BUILD INDEX ON users(i)", K.DDL, []),
    ('ALTER INDEX i ON users WITH {"action": "move"}', K.DDL, []),
    ("DROP INDEX i ON users", K.DDL, [D.DROP]),
    ("DROP COLLECTION dbaguy.app.users", K.DDL, [D.DROP]),
    ("DROP SCOPE dbaguy.app", K.DDL, [D.DROP]),
    ("CREATE FUNCTION f() { 1 }", K.DDL, []),
    ("CREATE SEQUENCE s1", K.DDL, []),
    ("GRANT query_select ON users TO bob", K.DDL, []),
    ("BEGIN WORK", K.TRANSACTION, []),
    ("begin transaction", K.TRANSACTION, []),
    ("START TRANSACTION", K.TRANSACTION, []),
    ("COMMIT", K.TRANSACTION, []),
    ("ROLLBACK TO SAVEPOINT s", K.TRANSACTION, []),
    ("SAVEPOINT s", K.TRANSACTION, []),
    ("SET TRANSACTION ISOLATION LEVEL READ COMMITTED", K.TRANSACTION, []),
    ("BEGIN", K.UNKNOWN, []),
    ("EXECUTE p", K.UNKNOWN, []),
    ("EXECUTE FUNCTION f()", K.UNKNOWN, []),
    ("SET x = 1", K.UNKNOWN, []),
    ("`users`", K.UNKNOWN, []),
    ("FOO BAR", K.UNKNOWN, []),
])
def test_sqlpp(sql, kind, reasons):
    result = classify(sql, SQ)
    assert (result.kind, result.dangerous_reasons) == (kind, reasons)


@pytest.mark.parametrize("sql, kind, reasons", [
    # FLUSH COLLECTION empties a collection (probed on 7.6.12): confirmed like TRUNCATE, never run unasked
    ("FLUSH COLLECTION dbaguy.app.users", K.DDL, [D.TRUNCATE]),
    ("flush collection `users`", K.DDL, [D.TRUNCATE]),
    # a bare expression starts no recognised statement: UNKNOWN, so read-only blocks it
    ("NEXT VALUE FOR s1", K.UNKNOWN, []),
    ("(NEXTVAL FOR s1)", K.UNKNOWN, []),
    ("1 + 1", K.UNKNOWN, []),
    ("$x", K.UNKNOWN, []),
    ("'DELETE FROM users'", K.UNKNOWN, []),
    ("", K.UNKNOWN, []),
    ("/* SELECT 1 */", K.UNKNOWN, []),
    # a leading parenthesis never hides the statement word, and a DML word wins over the READ word outside it
    ("(DELETE FROM users)", K.WRITE, [D.DELETE_WITHOUT_WHERE]),
    ("((SELECT NEXT VALUE FOR s1))", K.WRITE, []),
    ("(SELECT 1) UNION (SELECT 2) -- ;DELETE FROM users", K.READ, []),
    # nesting: a sequence or DML word anywhere in a SELECT, a subquery's WHERE or USE KEYS filters nothing
    ("SELECT (SELECT NEXT VALUE FOR s1) AS n", K.WRITE, []),
    ("SELECT 1 FROM t WHERE x IN (DELETE FROM u USE KEYS 'k')", K.WRITE, []),
    ("WITH a AS (SELECT NEXTVAL FOR s1 AS n) SELECT a.n", K.WRITE, []),
    ("SELECT * FROM (SELECT 1 AS a) AS t WHERE t.a IN (SELECT RAW 1 FROM s WHERE s.x = 1)", K.READ, []),
    ("UPDATE users SET a = (SELECT RAW 1 FROM x USE KEYS ['k'])", K.WRITE, [D.UPDATE_WITHOUT_WHERE]),
    ("DELETE FROM users LIMIT 1", K.WRITE, [D.DELETE_WITHOUT_WHERE]),
    # R-04a-T3a: a keys filter whose expression holds a subquery may name every document, so it filters nothing; a
    # subquery after the expression (SET, UNSET, LIMIT, RETURNING, WHERE) is something else
    ("DELETE FROM users USE KEYS (SELECT RAW META(x).id FROM users x)", K.WRITE, [D.DELETE_WITHOUT_WHERE]),
    ("DELETE FROM users USE KEYS ARRAY k FOR k IN (SELECT RAW META(x).id FROM users x) END", K.WRITE,
     [D.DELETE_WITHOUT_WHERE]),
    ("DELETE FROM users USE KEYS ['a'] || (SELECT RAW META(x).id FROM users x)", K.WRITE, [D.DELETE_WITHOUT_WHERE]),
    ("DELETE FROM users d USE KEYS ((SELECT RAW 1)) RETURNING d.a", K.WRITE, [D.DELETE_WITHOUT_WHERE]),
    ("UPDATE users USE KEYS (SELECT RAW META(x).id FROM users x) SET a = 1", K.WRITE, [D.UPDATE_WITHOUT_WHERE]),
    ("UPDATE users USE KEYS (SELECT RAW META(x).id FROM users x) UNSET a", K.WRITE, [D.UPDATE_WITHOUT_WHERE]),
    ("DELETE FROM users USE KEYS (SELECT RAW META(x).id FROM users x) WHERE x = 1", K.WRITE, []),
    ("DELETE FROM users USE KEYS ['a'] || (SELECT RAW 1) USE KEYS 'k'", K.WRITE, []),
    ("UPDATE users USE KEYS 'k' SET a = (SELECT RAW 1 FROM t)", K.WRITE, []),
    ("UPDATE users USE KEYS 'k' UNSET a RETURNING (SELECT RAW 1)", K.WRITE, []),
    ("DELETE FROM users USE KEYS ['a'] LIMIT (SELECT RAW 1)", K.WRITE, []),
    ("DELETE FROM users USE KEYS ['a', 'b'] RETURNING (SELECT RAW 1)", K.WRITE, []),
    ("DELETE FROM users USE KEYS $keys", K.WRITE, []),
    ("DELETE FROM users USE KEYS (`SELECT`)", K.WRITE, []),  # a field named select is no subquery
    ("DELETE FROM users WHERE true", K.WRITE, []),  # out of reach: a filter that filters nothing
    # FROM-first selects (R-04a-T3b, probed): READ like SELECT, scanned for sequences and DML words the same way
    ("FROM ks SELECT a", K.READ, []),
    ("from ks as k let x = 1 where k.a = x group by k.b having count(*) > 1 select k.b", K.READ, []),
    ("FROM ks USE KEYS ['a'] SELECT a", K.READ, []),
    ("FROM ks SELECT a UNION ALL FROM ks2 SELECT b", K.READ, []),
    ("(FROM ks SELECT a) UNION (SELECT 1)", K.READ, []),
    ("FROM ks SELECT PREV VALUE FOR s", K.READ, []),
    ("FROM ks SELECT NEXT VALUE FOR s", K.WRITE, []),
    ("FROM ks SELECT NEXTVAL FOR s", K.WRITE, []),
    ("FROM ks DELETE", K.WRITE, []),
    ("FROM ks SELECT a UNION ALL SELECT NEXT VALUE FOR s", K.WRITE, []),
    ("FROM ks WHERE a IN (UPDATE t SET x = 1) SELECT a", K.WRITE, []),
    ("FROM `DELETE` SELECT a", K.READ, []),
    ("FROM", K.READ, []),
    ("DELETE FROM users d USE (KEYS) x", K.WRITE, [D.DELETE_WITHOUT_WHERE]),  # the phrase's words are neighbours
    ("DELETE FROM users -- WHERE x\n", K.WRITE, [D.DELETE_WITHOUT_WHERE]),
    ("DELETE FROM users /* USE KEYS 'k' */ RETURNING 'WHERE', `where`", K.WRITE, [D.DELETE_WITHOUT_WHERE]),
    ("DELETE FROM users USE /* c */ KEYS 'k'", K.WRITE, []),
    # system: written the other ways the server reads it (probed): back-ticked, with escapes, spaced, commented
    ("DELETE FROM system : prepareds WHERE name = 'p'", K.WRITE, [D.SYSTEM_KEYSPACE]),
    ("DELETE FROM system/* c */:/* c */prepareds WHERE name = 'p'", K.WRITE, [D.SYSTEM_KEYSPACE]),
    ("DELETE FROM `system`:prepareds WHERE name = 'p'", K.WRITE, [D.SYSTEM_KEYSPACE]),
    ("DELETE FROM `\\u0073ystem`:prepareds WHERE name = 'p'", K.WRITE, [D.SYSTEM_KEYSPACE]),
    ("DELETE FROM `sys\\u0074em`:prepareds WHERE name = 'p'", K.WRITE, [D.SYSTEM_KEYSPACE]),
    ("DELETE FROM SYSTEM:prepareds WHERE name = 'p'", K.WRITE, [D.SYSTEM_KEYSPACE]),
    ("DELETE FROM default:users WHERE a = 1", K.WRITE, []),
    ("DELETE FROM users WHERE a = 'system:x'", K.WRITE, []),
    ("DELETE FROM `system` WHERE a = 1", K.WRITE, []),  # a keyspace named system, no colon
    ("UPDATE system:prepareds SET a = 1 WHERE b = 2", K.WRITE, [D.SYSTEM_KEYSPACE]),
    ("INSERT INTO system:prepareds (KEY, VALUE) VALUES ('k', {})", K.WRITE, [D.SYSTEM_KEYSPACE]),
    ("EXPLAIN DELETE FROM system:prepareds", K.READ, []),
    # MERGE … DELETE however it is spelt
    ("merge into users u using orders o on key o.u when matched then /* c */ delete where u.n = 1", K.WRITE,
     [D.MERGE_DELETE]),
    ("MERGE INTO users u USING (SELECT * FROM orders) o ON KEY o.u WHEN NOT MATCHED THEN INSERT (o.x)", K.WRITE, []),
    # a transaction word is not one inside something else, nor with a back-ticked word
    ("SELECT 1 /* BEGIN WORK */", K.READ, []),
    ("`BEGIN` WORK", K.UNKNOWN, []),
    ("BEGIN /* c */ WORK", K.TRANSACTION, []),
    ("BEGIN\tTRAN", K.TRANSACTION, []),
    ("START WORK", K.TRANSACTION, []),
    ("COMMIT TRANSACTION", K.TRANSACTION, []),
    ("START", K.UNKNOWN, []),
    ("END", K.UNKNOWN, []),
    ("RELEASE SAVEPOINT s", K.UNKNOWN, []),
    ("SET TRANSACTION", K.TRANSACTION, []),
    ("SET `TRANSACTION` ISOLATION LEVEL READ COMMITTED", K.UNKNOWN, []),
    ("EXECUTE", K.UNKNOWN, []),
    ("execute /* c */ function f()", K.UNKNOWN, []),
])
def test_sqlpp_bypass_attempts_and_edges(sql, kind, reasons):
    result = classify(sql, SQ)
    assert (result.kind, result.dangerous_reasons) == (kind, reasons)


@pytest.mark.parametrize("statement", [
    "DELETE FROM users", "UPSERT INTO users (KEY, VALUE) VALUES ('k', {})", "SELECT NEXT VALUE FOR s1",
    "SELECT NEXTVAL FOR s1", "MERGE INTO users u USING orders o ON KEY o.u WHEN MATCHED THEN DELETE",
    "EXECUTE p", "EXECUTE FUNCTION f()", "CREATE INDEX i ON users(name)", "BEGIN WORK",
    "UPDATE users SET a = 1 WHERE b = 2",
])
@pytest.mark.parametrize(
    "gap", [" ", "  ", "\t", "\n", "\r\n", " /* c */ ", "/**/", "-- c\n", " -- c\r", "/* ; DELETE */"]
)
def test_sqlpp_the_kind_survives_comments_and_whitespace_between_the_words(statement, gap):
    plain = classify(statement, SQ)
    spread = classify(gap.join(statement.split(" ")), SQ)
    assert plain.kind != K.READ
    assert (spread.kind, spread.dangerous_reasons) == (plain.kind, plain.dangerous_reasons)


def test_sqlpp_comments_strings_and_names_are_not_words():
    assert classify("/* DELETE */ SELECT 'DELETE', `delete` FROM ks", SQ).kind == K.READ
    assert classify("SELECT 1 -- ;DELETE\n", SQ).kind == K.READ
    assert classify("SELECT 1; DELETE FROM users", SQ).dangerous_reasons == [D.DELETE_WITHOUT_WHERE]  # a script
    assert classify("SELECT 'NEXT VALUE FOR s', `NEXT VALUE FOR` FROM ks", SQ).kind == K.READ
    assert classify("SELECT \"x\" AS `update`, 'it''s ; DELETE FROM users' FROM ks", SQ).kind == K.READ


def test_sqlpp_a_script_is_judged_statement_by_statement():
    assert classify("SELECT 1; EXECUTE p", SQ).kind == K.UNKNOWN
    assert classify("SELECT 1; SELECT NEXT VALUE FOR s1", SQ).kind == K.WRITE
    assert classify("SELECT 1; DELETE FROM users USE KEYS 'k'; BEGIN WORK", SQ).kind == K.WRITE
    assert classify("BEGIN WORK; SELECT 1", SQ).kind == K.TRANSACTION
    assert classify("SELECT 1;; SELECT 2;", SQ).kind == K.READ
    assert classify("SELECT 1; DROP INDEX i ON users", SQ).dangerous_reasons == [D.DROP]


@pytest.mark.parametrize("sql", [
    "SELECT 'unterminated", "SELECT `unterminated", "SELECT /* unterminated", "DELETE FROM `x\\", "UPDATE `\\u00", "`",
    "(" * 5000 + "SELECT 1", "DELETE FROM users WHERE " + "(" * 2000,
])
def test_sqlpp_never_raises_on_broken_text(sql):
    assert classify(sql, SQ).kind in set(K)


def test_is_transaction_statement():
    assert is_transaction_statement("  begin work", SQ)
    assert is_transaction_statement("/* x */ COMMIT", SQ)
    assert not is_transaction_statement("SELECT 1", SQ)
    assert not is_transaction_statement("BEGIN", SQ)
    assert is_transaction_statement("BEGIN", Dialect.POSTGRES)  # a dialect without a table: the classifier decides


@pytest.mark.parametrize("sql", [
    "BEGIN WORK", "begin tran", "BEGIN TRANSACTION", "START TRANSACTION", "START WORK", "COMMIT", "COMMIT WORK",
    "ROLLBACK", "ROLLBACK TO SAVEPOINT s", "SAVEPOINT s", "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
    "BEGIN /* c */ WORK", "(BEGIN WORK)",
])
def test_is_transaction_statement_agrees_with_the_classifier_for_sqlpp(sql):
    assert is_transaction_statement(sql, SQ)
    assert classify(sql, SQ).kind == K.TRANSACTION


def test_tsql_unfiltered_dml_is_unchanged():  # the helper was generalised for SQL++
    assert classify("DELETE FROM t", Dialect.TSQL).dangerous_reasons == [D.DELETE_WITHOUT_WHERE]
    assert classify("DELETE FROM t WHERE id IN (SELECT id FROM u)", Dialect.TSQL).dangerous_reasons == []
    assert classify("WITH c AS (SELECT 1 AS id) DELETE FROM t", Dialect.TSQL).dangerous_reasons == [D.DELETE_WITHOUT_WHERE]
    assert classify("UPDATE t SET a = 1 WHERE id = 1", Dialect.TSQL).dangerous_reasons == []
    # a CTE named like a SQL++ statement word is still a CTE, and USE KEYS is no T-SQL filter
    for sql in ("WITH upsert AS (SELECT 1 AS id) DELETE FROM t", "DELETE FROM t OUTPUT deleted.* USE KEYS"):
        assert classify(sql, Dialect.TSQL).dangerous_reasons == [D.DELETE_WITHOUT_WHERE]


# --- D-06-4: a large INSERT is judged by its first word ---------------------------------------------------------------

_ROWS = ", ".join(f"({i}, 'name {i} it''s', {i}.5, NULL)" for i in range(2_000))  # about 64 KiB and more
LARGE_INSERTS = [
    (PG, f"INSERT INTO t (a, b, c, d) VALUES {_ROWS}"),
    (PG, f"INSERT INTO t (a, b, c, d) VALUES {_ROWS} ON CONFLICT (a) DO UPDATE SET b = excluded.b"),
    (MY, f"INSERT INTO t (a, b, c, d) VALUES {_ROWS} ON DUPLICATE KEY UPDATE b = VALUES(b)"),
    (MY, f"REPLACE INTO t (a, b, c, d) VALUES {_ROWS}"),
    (LITE, f"INSERT OR ABORT INTO t (a, b, c, d) VALUES {_ROWS}"),
    (Dialect.TSQL, f"INSERT INTO dbo.t (a, b, c, d) VALUES {_ROWS}"),
    (PG, f"INSERT INTO t SELECT * FROM u WHERE b IN ({_ROWS.replace('NULL', '0')})"),
]


@pytest.mark.parametrize(("dialect", "sql"), LARGE_INSERTS)
def test_a_large_insert_gets_the_answer_sqlglot_gives(monkeypatch, dialect, sql):
    assert len(sql) > classifier.LARGE_INSERT_CHARS
    capped = classify(sql, dialect)
    monkeypatch.setattr(classifier, "LARGE_INSERT_CHARS", 10**12)
    assert classify(sql, dialect) == capped == Classification(kind=K.WRITE)


def test_a_large_insert_never_hides_a_second_statement():
    drop = f"DELIMITER $$\nINSERT INTO t VALUES {_ROWS}; DROP TABLE t$$"  # one span under the custom delimiter
    result = classify(drop, MY)
    assert (result.kind, result.dangerous_reasons) == (K.DDL, [DangerReason.DROP])
    hinted = f"INSERT /*+ SET_VAR(sort_buffer_size = 16M) */ INTO t VALUES {_ROWS}"  # an executable comment: full rules
    assert classify(hinted, MY).kind == K.WRITE
    versioned = f"/*!40000 INSERT INTO t VALUES {_ROWS} */"
    assert classify(versioned, MY).kind == K.UNKNOWN


def test_a_large_insert_is_classified_without_its_tokens_in_memory():
    import tracemalloc

    sql = f"INSERT INTO t (a, b, c, d) VALUES {', '.join([_ROWS] * 8)}"  # about 0.6 MB
    tracemalloc.start()
    try:
        assert classify(sql, PG) == Classification(kind=K.WRITE)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * len(sql), f"peak {peak:,} bytes for a {len(sql):,}-character INSERT"  # sqlglot: about 113 times


def test_a_large_insert_with_an_executable_comment_after_its_first_word_is_judged_by_the_full_rules():
    versioned = f"INSERT INTO t VALUES {_ROWS} /*!50000 ON DUPLICATE KEY UPDATE b = 1 */"
    assert len(versioned) > classifier.LARGE_INSERT_CHARS
    assert classify(versioned, MY).kind == K.UNKNOWN


def test_only_an_insert_takes_the_shortcut():
    filler = "/* " + "x" * (classifier.LARGE_INSERT_CHARS + 10) + " */"  # long text inside the statement, not a row
    assert classify(f"DELETE {filler} FROM t", PG).dangerous_reasons == [DangerReason.DELETE_WITHOUT_WHERE]
    assert classify(f"UPDATE {filler} t SET a = 1", PG).dangerous_reasons == [DangerReason.UPDATE_WITHOUT_WHERE]
    assert classify(f"SELECT {filler} 1", PG).kind == K.READ
    for dialect in (PG, MY, LITE, Dialect.TSQL):  # nothing to judge is not a write
        assert classify(filler + "\n" * 10, dialect).kind != K.WRITE, dialect
