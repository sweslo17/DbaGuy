import logging

import pytest

from dbaguy_backend.sql.classifier import StatementKind as K
from dbaguy_backend.sql.classifier import classify
from dbaguy_backend.sql.dialects import Dialect

PG, LITE = Dialect.POSTGRES, Dialect.SQLITE


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
