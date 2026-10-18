import pytest

from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.lexer import INSIGNIFICANT, tokenize
from dbaguy_backend.sql.tsql import statements


def cut(sql):
    tokens = [t for t in tokenize(sql, Dialect.TSQL) if t.kind not in INSIGNIFICANT]
    return [sql[piece[0].start:piece[-1].end] for piece in statements(tokens)]


@pytest.mark.parametrize("sql, pieces", [
    ("DECLARE @x INT\nDELETE FROM t", ["DECLARE @x INT", "DELETE FROM t"]),
    ("DECLARE @x int = 1; SELECT @x", ["DECLARE @x int = 1", "SELECT @x"]),
    ("SELECT 1\nDROP TABLE t", ["SELECT 1", "DROP TABLE t"]),
    ("UPDATE t SET a = 1 WHERE id = 2 SELECT 1", ["UPDATE t SET a = 1 WHERE id = 2", "SELECT 1"]),
    ("UPDATE t SET a = (SELECT MAX(b) FROM u) FROM t JOIN u ON u.id = t.id", None),
    ("UPDATE t SET a = CASE WHEN b = 1 THEN 2 ELSE 3 END WHERE id = 1 SET NOCOUNT OFF",
     ["UPDATE t SET a = CASE WHEN b = 1 THEN 2 ELSE 3 END WHERE id = 1", "SET NOCOUNT OFF"]),
    ("INSERT INTO t (a) SELECT b FROM u WHERE c = 1", None),
    ("INSERT INTO t EXEC dbo.p", None),
    ("INSERT INTO t SELECT 1 SELECT 2", ["INSERT INTO t SELECT 1", "SELECT 2"]),
    ("WITH c AS (SELECT 1 AS a) DELETE FROM c", None),
    ("WITH c AS (SELECT 1 AS a) SELECT * FROM c DELETE FROM t", ["WITH c AS (SELECT 1 AS a) SELECT * FROM c", "DELETE FROM t"]),
    ("WITH c AS (SELECT 1 a), d AS (SELECT 2 b) SELECT * FROM c, d", None),
    ("MERGE t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = s.a WHEN NOT MATCHED THEN INSERT (id) VALUES (s.id);SELECT 1",
     ["MERGE t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = s.a WHEN NOT MATCHED THEN INSERT (id) VALUES (s.id)",
      "SELECT 1"]),
    ("IF EXISTS (SELECT 1 FROM t) DELETE FROM t ELSE UPDATE t SET a = 1",
     ["IF EXISTS (SELECT 1 FROM t)", "DELETE FROM t", "ELSE", "UPDATE t SET a = 1"]),
    ("BEGIN TRY SELECT 1/0 END TRY BEGIN CATCH SELECT ERROR_MESSAGE() END CATCH",
     ["BEGIN TRY", "SELECT 1/0", "END TRY", "BEGIN CATCH", "SELECT ERROR_MESSAGE()", "END CATCH"]),
    ("SELECT CASE WHEN a = 1 THEN 'x' ELSE 'y' END AS c FROM t", None),
    ("DROP TABLE IF EXISTS #t CREATE TABLE #t (a int)", ["DROP TABLE IF EXISTS #t", "CREATE TABLE #t (a int)"]),
    ("GRANT SELECT, INSERT, UPDATE ON t TO u", None),
    ("ALTER TABLE t DROP COLUMN a", None),
    ("ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES u (id) ON DELETE SET NULL ON UPDATE CASCADE", None),
    ("SELECT a FROM t ORDER BY a OFFSET 10 ROWS FETCH NEXT 5 ROWS ONLY", None),
    ("DECLARE c CURSOR FOR SELECT a FROM t FOR UPDATE OF a OPEN c FETCH NEXT FROM c INTO @x CLOSE c DEALLOCATE c",
     ["DECLARE c CURSOR FOR SELECT a FROM t FOR UPDATE OF a", "OPEN c", "FETCH NEXT FROM c INTO @x", "CLOSE c",
      "DEALLOCATE c"]),
    ("SELECT 1 UNION ALL SELECT 2 EXCEPT SELECT 3", None),
    ("SELECT a FROM t1 INNER MERGE JOIN t2 ON t1.id = t2.id", None),
    ("SELECT 'DELETE FROM t' AS s, [update] FROM t", None),
    ("DELETE FROM t OUTPUT deleted.* INTO @log WHERE id = 1", None),
    ("RAISERROR('x', 10, 1) WITH NOWAIT WAITFOR DELAY '00:00:01'", ["RAISERROR('x', 10, 1) WITH NOWAIT", "WAITFOR DELAY '00:00:01'"]),
    ("BULK INSERT t FROM 'x'", None),
    # R-03b-P3: narrow merges keep the next statement (and its danger reason) apart
    ("SET NOCOUNT ON\nDELETE FROM t", ["SET NOCOUNT ON", "DELETE FROM t"]),
    ("SET XACT_ABORT ON UPDATE t SET a = 1", ["SET XACT_ABORT ON", "UPDATE t SET a = 1"]),
    ("ALTER TABLE t ADD c int DROP TABLE u", ["ALTER TABLE t ADD c int", "DROP TABLE u"]),
    ("ALTER TABLE t ALTER COLUMN a bigint", None),
    ("GRANT SELECT ON t TO u DROP TABLE t", ["GRANT SELECT ON t TO u", "DROP TABLE t"]),
    ("REVOKE SELECT ON t FROM u DELETE FROM t", ["REVOKE SELECT ON t FROM u", "DELETE FROM t"]),
    ("GRANT SELECT ON t TO u WITH GRANT OPTION", None),
    ("WITH c AS (SELECT * FROM t) UPDATE c SET a = 1", None),
    # R-03b-P2: write statements the pre-flight slipped past
    ("DECLARE @p varbinary(16)\nWRITETEXT dbo.zz_text.c @p 'new'", ["DECLARE @p varbinary(16)", "WRITETEXT dbo.zz_text.c @p 'new'"]),
    ("DECLARE @h uniqueidentifier\nSEND ON CONVERSATION @h ('x')", ["DECLARE @h uniqueidentifier", "SEND ON CONVERSATION @h ('x')"]),
    # Found by the self-check, each run by SQL Server 2022 as described.
    # THROW, MOVE, GET, ENABLE and DISABLE are not reserved: they start a statement only before their own syntax.
    ("SELECT throw INTO t2 FROM t", None),
    ("SELECT a AS get, move FROM t", None),
    ("SELECT t.throw, t.send INTO t2 FROM t", None),  # a word after a dot is a name
    ("IF @x = 1 THROW 50000, 'x', 1", ["IF @x = 1", "THROW 50000, 'x', 1"]),
    ("BEGIN CATCH THROW END CATCH", ["BEGIN CATCH", "THROW", "END CATCH"]),
    ("IF 1 = 1 DISABLE TRIGGER tr ON t ELSE ENABLE TRIGGER ALL ON t",
     ["IF 1 = 1", "DISABLE TRIGGER tr ON t", "ELSE", "ENABLE TRIGGER ALL ON t"]),
    ("SELECT enable, disable FROM t", None),
    # AFTER is not reserved either: an alias named after does not join the next statement to the SELECT.
    ("SELECT 1 AS after\nDELETE FROM t", ["SELECT 1 AS after", "DELETE FROM t"]),
    # ON joins INSERT/UPDATE/DELETE to a CREATE/ALTER only as a foreign key's action; SET joins only an UPDATE.
    ("ALTER LOGIN l WITH CHECK_POLICY = ON\nDELETE FROM t", ["ALTER LOGIN l WITH CHECK_POLICY = ON", "DELETE FROM t"]),
    ("ALTER DATABASE CURRENT SET ANSI_NULLS ON\nUPDATE t SET a = 1",
     ["ALTER DATABASE CURRENT", "SET ANSI_NULLS ON", "UPDATE t SET a = 1"]),
    ("ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES u (id) ON UPDATE CASCADE\nSET XACT_ABORT ON\nDELETE FROM t",
     ["ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES u (id) ON UPDATE CASCADE", "SET XACT_ABORT ON",
      "DELETE FROM t"]),
    ("ALTER TABLE t ADD c int\nDROP ASSEMBLY a", ["ALTER TABLE t ADD c int", "DROP ASSEMBLY a"]),
    ("ALTER TABLE t DROP CONSTRAINT fk, COLUMN c", None),
    ("ALTER PARTITION FUNCTION pf() MERGE RANGE (1)\nDROP TABLE t", ["ALTER PARTITION FUNCTION pf() MERGE RANGE (1)", "DROP TABLE t"]),
    ("INSERT INTO t VALUES (1)\nSELECT * INTO t2 FROM t", ["INSERT INTO t VALUES (1)", "SELECT * INTO t2 FROM t"]),
    # R-03b-T3c: SEND, RECEIVE, MOVE and GET start a statement only in their own form
    ("SELECT receive, send FROM t WHERE conversation = 1 AND dialog = 2 ORDER BY move, get", None),
    ("SELECT * FROM t AS receive JOIN u AS send ON send.id = receive.id", None),
    ("IF 1 = 1 RECEIVE * FROM q", ["IF 1 = 1", "RECEIVE * FROM q"]),
    ("BEGIN RECEIVE * FROM q END", ["BEGIN", "RECEIVE * FROM q", "END"]),
    ("IF 1 = 1 SEND ON CONVERSATION @h ('x') ELSE GET CONVERSATION GROUP @g FROM q",
     ["IF 1 = 1", "SEND ON CONVERSATION @h ('x')", "ELSE", "GET CONVERSATION GROUP @g FROM q"]),
])
def test_statement_cuts(sql, pieces):
    assert cut(sql) == (pieces if pieces is not None else [sql])
