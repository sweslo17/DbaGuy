import pytest

from dbaguy_backend.domain.classification import Classification, DangerReason, GuardReason, StatementKind
from dbaguy_backend.domain.profiles import SqliteProfileDraft
from dbaguy_backend.services.safety import GuardAction as A, SafetyPolicy
from dbaguy_backend.sql.dialects import Dialect

PG = Dialect.POSTGRES
policy = SafetyPolicy()


def decide(sql, level="standard", read_only=False, confirmed=False):
    decision = policy.check(sql, PG, level, read_only, confirmed)
    return decision.action, decision.reasons


@pytest.mark.parametrize(("sql", "level", "expected"), [
    ("SELECT 1", "standard", (A.RUN, [])),
    ("UPDATE t SET x = 1", "standard", (A.CONFIRM, ["update_without_where"])),
    ("UPDATE t SET x = 1 WHERE id = 1", "standard", (A.RUN, [])),
    ("DELETE FROM t", "standard", (A.CONFIRM, ["delete_without_where"])),
    ("DROP TABLE t", "standard", (A.CONFIRM, ["drop"])),
    ("DROP TABLE t", "relaxed", (A.RUN, [])),
    ("INSERT INTO t VALUES (1)", "standard", (A.RUN, [])),
    ("INSERT INTO t VALUES (1)", "strict", (A.CONFIRM, ["strict_write"])),
    ("DROP TABLE t", "strict", (A.CONFIRM, ["drop"])),
    ("frobnicate the database", "strict", (A.CONFIRM, ["strict_unknown"])),
    ("SELECT 1", "strict", (A.RUN, [])),
])
def test_levels(sql, level, expected):
    assert decide(sql, level) == expected


def test_confirmation_lets_it_run():
    assert decide("DROP TABLE t", confirmed=True) == (A.RUN, [])


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)", "DROP TABLE t", "SET default_transaction_read_only = off", "RESET ALL",
    "BEGIN READ WRITE", "START TRANSACTION READ WRITE", "EXPLAIN ANALYZE DELETE FROM t",
    "SELECT * FROM t FOR UPDATE", "frobnicate",
    "SELECT set_config('default_transaction_read_only', 'off', false)",
    "WITH x AS (SELECT 1) SELECT set_config('default_transaction_read_only','off',false)",
    "SELECT pg_catalog.set_config('a.b', 'c', false)",
    'SELECT "set_config"(\'a.b\',\'c\',false)',
    "EXPLAIN ANALYZE SELECT set_config('a.b','c',false)",
    "SELECT * FROM (SELECT set_config('a.b','c',false)) s",
])
def test_read_only_blocks_everything_that_can_write(sql):
    assert decide(sql, "relaxed", read_only=True, confirmed=True) == (A.BLOCK, ["read_only"])


@pytest.mark.parametrize("sql", ["SELECT 1", "BEGIN", "COMMIT", "ROLLBACK", "EXPLAIN SELECT 1", "SHOW search_path"])
def test_read_only_allows_reads_and_plain_transactions(sql):
    assert decide(sql, "standard", read_only=True)[0] == A.RUN


def test_strict_read_only_runs_reads_and_confirms_transactions():
    assert decide("SELECT 1", "strict", read_only=True) == (A.RUN, [])
    assert decide("BEGIN", "strict", read_only=True) == (A.CONFIRM, ["strict_transaction"])


def test_set_config_runs_when_the_profile_is_not_read_only():
    assert decide("SELECT set_config('a.b', 'c', false)") == (A.RUN, [])


def test_profiles_default_to_standard():
    assert SqliteProfileDraft(engine="sqlite", name="x", path="/tmp/x.db").safety == "standard"


def test_guard_reasons_are_the_danger_reasons_plus_the_strict_and_read_only_ones():
    strict = {"strict_write", "strict_ddl", "strict_transaction", "strict_session", "strict_unknown"}
    assert {r.value for r in GuardReason} == {r.value for r in DangerReason} | strict | {"read_only"}


@pytest.mark.parametrize("kind", [k for k in StatementKind if k != StatementKind.READ])
def test_strict_confirms_every_kind_but_read_with_a_typed_reason(kind):
    decision = policy.decide("x", Classification(kind=kind), PG, "strict", read_only=False)
    assert decision.action == A.CONFIRM and decision.reasons == [GuardReason(f"strict_{kind}")]
    assert all(isinstance(reason, GuardReason) for reason in decision.reasons)


def test_guard_reasons_are_plain_strings_on_the_wire():
    decision = policy.check("UPDATE t SET x = 1", PG, "standard", read_only=False)
    assert decision.model_dump(mode="json")["reasons"] == ["update_without_where"]
    assert isinstance(decision.reasons[0], GuardReason)


# ---- read-only PostgreSQL connections may still change harmless settings ----

HARMLESS_SETTINGS = [
    "SET search_path TO app, public", "SET search_path = app", 'SET "search_path" TO public', "set Search_Path to x",
    "SET SESSION statement_timeout = '5s'", "SET LOCAL lock_timeout TO 1000",
    "SET idle_in_transaction_session_timeout = 0", "SET timezone = 'UTC'", "SET TIME ZONE 'Asia/Taipei'",
    "SET LOCAL TIME ZONE LOCAL", "SET SESSION TIME ZONE DEFAULT", "SET datestyle TO ISO, YMD",
    "SET intervalstyle = 'iso_8601'", "SET work_mem = '64MB'", "SET application_name = 'report'",
    "SET client_min_messages TO warning", "SET extra_float_digits = 3", "SET bytea_output = 'hex'",
    "/* for the report */ SET search_path TO reporting;", "RESET search_path", "RESET statement_timeout",
    "RESET TIME ZONE", 'RESET "work_mem"', "reset timezone",
]


@pytest.mark.parametrize("sql", HARMLESS_SETTINGS)
def test_read_only_allows_harmless_postgres_settings(sql):
    assert decide(sql, "standard", read_only=True) == (A.RUN, [])


@pytest.mark.parametrize("sql", [
    "SET TRANSACTION READ WRITE", "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE", "SET ROLE admin",
    "SET SESSION AUTHORIZATION admin", "RESET ALL", "DISCARD ALL", "RESET ROLE", "RESET SESSION AUTHORIZATION",
    "SET transaction_read_only = off", "SET default_transaction_read_only = off",
    "SET LOCAL default_transaction_read_only TO off", "RESET default_transaction_read_only",
    "SET session_replication_role = replica", "SET myext.search_path = x", "SET search_path FROM CURRENT",
    "SET SCHEMA 'app'", "SET NAMES 'UTF8'", "SET search_path", "RESET", "SET TIME ZONE",
    "SET search_path = x; SET transaction_read_only = off", "RESET search_path; RESET ALL",
    "SET search_path = x; SELECT 1",
])
def test_read_only_still_blocks_every_other_setting(sql):
    assert decide(sql, "standard", read_only=True) == (A.BLOCK, ["read_only"])


def test_the_setting_allow_list_is_postgres_only():
    decision = policy.check("SET search_path TO x", Dialect.SQLITE, "standard", read_only=True)
    assert (decision.action, decision.reasons) == (A.BLOCK, ["read_only"])


# ---- read-only MySQL connections: harmless session statements only ----

MY = Dialect.MYSQL


def decide_my(sql, level="standard", read_only=False):
    decision = policy.check(sql, MY, level, read_only)
    return decision.action, decision.reasons


@pytest.mark.parametrize("sql", [
    "USE analytics", "USE `my db`", "SET NAMES utf8mb4", "SET NAMES 'utf8mb4' COLLATE 'utf8mb4_bin'",
    "SET CHARACTER SET utf8mb4", "SET CHARSET utf8mb4", "SET @x = 1", "SET @x := (SELECT MAX(id) FROM t), @y = 2",
    "SET @'odd name' = 1", "SET sql_mode = ''", "SET SESSION sql_mode = 'ANSI'", "SET LOCAL time_zone = 'UTC'",
    "SET @@sql_mode = 'TRADITIONAL'", "SET @@session.time_zone = '+00:00'", "SET @@local.sql_mode := ''",
    "SELECT a INTO @x FROM t", "START TRANSACTION", "START TRANSACTION READ ONLY",
    "SELECT /*+ MAX_EXECUTION_TIME(1000) */ * FROM t",
    "set @x = 1;", "/* report */ SET SESSION SQL_MODE = ''", "SET @x = (SELECT 1, 2)",
    # Variables that shape how statements run or look, never whether they may write.
    "SET autocommit = 0", "SET @x = 1, autocommit = 0", "SET SESSION autocommit = 1", "SET @@autocommit := 1",
    "SET group_concat_max_len = 1000000", "SET max_execution_time = 1000", "SET SESSION sql_select_limit = 10",
    "SET lc_time_names = 'en_US'",
    # A read-only characteristic can only make things stricter.
    "SET SESSION TRANSACTION READ ONLY", "SET TRANSACTION READ ONLY", "SET LOCAL TRANSACTION READ ONLY",
    "set session transaction read only;",
    "DELIMITER $$\nSET @x = 1$$\nDELIMITER ;",
])
def test_mysql_read_only_allows(sql):
    assert decide_my(sql, read_only=True) == (A.RUN, [])


@pytest.mark.parametrize("sql", [
    "SET SESSION TRANSACTION READ WRITE", "SET TRANSACTION READ WRITE",
    "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED", "SET SESSION transaction_read_only = 0",
    "SET transaction_read_only = OFF", "SET @@session.transaction_read_only = 0", "SET GLOBAL sql_mode = ''",
    "SET @@global.time_zone = '+00:00'", "SET PERSIST sql_mode = ''",
    "SET NAMES", "SET sql_mode = ''; DELETE FROM t", "START TRANSACTION READ WRITE",
    "INSERT INTO t VALUES (1)", "SELECT * FROM t INTO OUTFILE '/tmp/x'", "SELECT * INTO DUMPFILE '/tmp/x' FROM t",
    "/*!40000 DELETE FROM t */", "/*!40101 SET NAMES utf8 */", "LOAD DATA INFILE '/x' INTO TABLE t",
    "HANDLER t OPEN", "DO SLEEP(1)", "CALL p()", "DELIMITER $$",
    # Nothing that is not plainly a harmless variable, and nothing hidden next to one.
    "SET ROLE admin", "SET PASSWORD = 'x'", "SET DEFAULT ROLE ALL TO u", "SET @x = 1; SET @y = 2",
    "SET @x := 1, @@global.sql_mode = ''", "SET sql_mode", "SET sql_mode =", "SET CHARACTER SET", "SET @x = 1,",
    "SET @x = /*!50000 1 */", "SET SESSION transaction_isolation = 'READ-COMMITTED'",
    "USE", "USE a b", "USE analytics; USE other", "USE (a)", "SET search_path TO public", "RESET sql_mode",
    # R-03a-F3: results are always decoded as UTF-8, so their charset is not harmless.
    "SET character_set_results = utf16", "SET character_set_results = NULL",
    "SET @@session.character_set_results = 'latin1'",
])
def test_mysql_read_only_blocks(sql):
    assert decide_my(sql, read_only=True) == (A.BLOCK, ["read_only"])


@pytest.mark.parametrize("sql", [
    # Not transactions: the server starts or stops replication, which a read-only profile must never do.
    "START REPLICA", "START SLAVE", "START GROUP_REPLICATION", "STOP REPLICA", "STOP GROUP_REPLICATION",
    "START REPLICA UNTIL SOURCE_LOG_FILE = 'x', SOURCE_LOG_POS = 4", "START", "XA START 'a'",
    # EXPLAIN's synonyms and a hint between its words do not hide the statement that really runs.
    "DESCRIBE ANALYZE DELETE FROM t", "DESC ANALYZE DELETE FROM t", "EXPLAIN /*+ x */ ANALYZE DELETE FROM t",
    "EXPLAIN FORMAT=TREE /*+ x */ ANALYZE DELETE FROM t", "DESCRIBE ANALYZE FORMAT=TREE DELETE t FROM t JOIN u ON 1",
    "/*+ x */ EXPLAIN ANALYZE DELETE FROM t",
    # A SET value that takes row locks is a locking read.
    "SET @x = (SELECT id FROM t FOR UPDATE)", "SET @x := (SELECT id FROM t LOCK IN SHARE MODE)",
    "SET @x = (SELECT id FROM t FOR SHARE), @y = 1", "SET sql_mode = (SELECT 1 FOR UPDATE)",
    # Two statements in one span (a custom DELIMITER can make that) are never one harmless SET.
    "DELIMITER $$\nSET @x = 1; SET transaction_read_only = 0$$\nDELIMITER ;",
    "DELIMITER $$\nSET @x = 1; SET @y = 2$$\nDELIMITER ;",
    "DELIMITER $$\nUSE a; USE b$$\nDELIMITER ;",
    # Only a read-only characteristic, on its own, at the session level.
    "SET GLOBAL TRANSACTION READ ONLY", "SET PERSIST TRANSACTION READ ONLY", "SET SESSION TRANSACTION",
    "SET SESSION TRANSACTION READ", "SET SESSION TRANSACTION READ ONLY, READ WRITE",
    "SET SESSION TRANSACTION READ ONLY, ISOLATION LEVEL SERIALIZABLE", "SET TRANSACTION READ ONLY READ WRITE",
    "SET autocommit = 0, transaction_read_only = 0", "SET @@session.autocommit = 0; SET GLOBAL sql_mode = ''",
])
def test_mysql_read_only_blocks_what_the_review_found(sql):
    assert decide_my(sql, read_only=True) == (A.BLOCK, ["read_only"])


@pytest.mark.parametrize("sql", [
    "START TRANSACTION READ ONLY", "START TRANSACTION", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT a",
    "RELEASE SAVEPOINT a", "ROLLBACK TO SAVEPOINT a", "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
])
def test_mysql_read_only_still_allows_transactions(sql):
    assert decide_my(sql, read_only=True) == (A.RUN, [])


@pytest.mark.parametrize("sql", [
    "START TRANSACTION READ WRITE", "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ WRITE",
    "START TRANSACTION READ WRITE, WITH CONSISTENT SNAPSHOT",
])
def test_mysql_read_only_still_blocks_a_read_write_transaction(sql):
    assert decide_my(sql, read_only=True) == (A.BLOCK, ["read_only"])


@pytest.mark.parametrize(("sql", "reasons"), [
    ("ALTER TABLE t DROP PARTITION p0", ["alter_drop"]), ("ALTER TABLE t TRUNCATE PARTITION p0, p1", ["truncate"]),
    ("ALTER TABLE t DROP PRIMARY KEY", ["alter_drop"]), ("ALTER TABLE t DROP INDEX i", ["alter_drop"]),
])
def test_mysql_alters_that_lose_data_are_confirmed_at_the_default_level(sql, reasons):  # R-03a-F1
    assert decide_my(sql) == (A.CONFIRM, reasons)
    assert decide_my(sql, read_only=True) == (A.BLOCK, ["read_only"])


def test_mysql_version_comments_are_unknown_statements():
    assert decide_my("/*!40101 SET NAMES utf8 */", "strict") == (A.CONFIRM, ["strict_unknown"])
    assert decide_my("SELECT /*+ BKA(t) */ * FROM t", "strict") == (A.RUN, [])


def test_the_mysql_allow_list_does_not_leak_into_the_other_engines():
    for dialect in (PG, Dialect.SQLITE):
        for sql in ("SET @x = 1", "USE analytics", "SET NAMES utf8mb4"):
            decision = policy.check(sql, dialect, "standard", read_only=True)
            assert (decision.action, decision.reasons) == (A.BLOCK, ["read_only"])


# ---- read-only SQL Server connections: DbaGuy is the only guard (spec §3.4, plan Ruling 9) ----

TS = Dialect.TSQL


def decide_ts(sql, level="standard", read_only=False):
    decision = policy.check(sql, TS, level, read_only)
    return decision.action, decision.reasons


@pytest.mark.parametrize("sql", [
    "DECLARE @x int = 1; SELECT @x", "SELECT @x = 1", "PRINT 'x'", "WAITFOR DELAY '00:00:01'", "IF 1 = 1 SELECT 1",
    "SET NOCOUNT ON", "SET NOCOUNT ON; SELECT 1", "SET ANSI_NULLS, QUOTED_IDENTIFIER ON", "SET ANSI_WARNINGS OFF",
    "SET ARITHABORT ON", "SET DATEFORMAT dmy", "SET LANGUAGE us_english", "SET LANGUAGE N'us_english'",
    "SET LOCK_TIMEOUT 1000", "SET STATISTICS IO ON", "SET STATISTICS TIME ON", "SET SHOWPLAN_XML ON",
    "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED", "USE analytics", "USE [my db]", "BEGIN TRAN",
    # Fix round 1: every STATISTICS item, more harmless settings, signed values
    "SET STATISTICS IO, TIME ON", "SET STATISTICS PROFILE ON", "SET STATISTICS XML ON",
    "SET STATISTICS TIME, IO, PROFILE, XML OFF", "SET DATEFIRST 1", "SET TEXTSIZE 65536", "SET DEADLOCK_PRIORITY LOW",
    "SET DEADLOCK_PRIORITY -5", "SET DEADLOCK_PRIORITY @p", "SET CONCAT_NULL_YIELDS_NULL ON", "SET NUMERIC_ROUNDABORT OFF",
    "SET LOCK_TIMEOUT -1", "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE", "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
    # R-03b-T3c: columns and tables named like Service Broker words are plain reads
    "SELECT a FROM t WHERE conversation = 1", "SELECT send, receive, conversation, dialog FROM t", "SELECT receive FROM t",
    "SELECT * FROM receive WHERE dialog = 1", "SELECT a AS dialog, b AS send FROM t ORDER BY receive",
    "SELECT t.send, t.receive FROM t", "DECLARE @c int = 1\nSELECT conversation FROM t WHERE receive = @c",
    "SELECT * FROM t AS x JOIN u ON x.conversation = u.id",
])
def test_tsql_read_only_allows(sql):
    assert decide_ts(sql, read_only=True) == (A.RUN, [])


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)", "EXEC sp_who", "EXECUTE p", "sp_who", "SELECT 1\nDELETE FROM t",
    "DECLARE @t TABLE (a int); INSERT INTO @t VALUES (1)", "SELECT * INTO #t FROM t", "CREATE TABLE #t (a int)",
    "SET IMPLICIT_TRANSACTIONS ON", "SET XACT_ABORT ON", "SET IDENTITY_INSERT t ON",
    "SET ROWCOUNT 10", "SET NOCOUNT ON; SET XACT_ABORT ON", "SET NOCOUNT ON\nUPDATE t SET a = 1",
    "DBCC FREEPROCCACHE", "SELECT * FROM t WITH (UPDLOCK)", "SET NOCOUNT ON\nGO\nSET XACT_ABORT ON",
    # R-03b-P2: the pre-flight's bypasses
    "DECLARE @p varbinary(16) = (SELECT TEXTPTR(c) FROM dbo.zz_text WHERE id = 1)\nWRITETEXT dbo.zz_text.c @p 'new'",
    "DECLARE @p varbinary(16)\nUPDATETEXT dbo.zz_text.c @p 0 NULL 'x'",
    "SELECT * INTO t2 FROM t OPTION (USE HINT ('FORCE_LEGACY_CARDINALITY_ESTIMATION'))",
    "SELECT * INTO t2 FROM CHANGETABLE(CHANGES t, 0) c",
    "DECLARE @h uniqueidentifier\nEND CONVERSATION @h",
    "DECLARE @h uniqueidentifier\nBEGIN DIALOG CONVERSATION @h FROM SERVICE s TO SERVICE 't' ON CONTRACT c",
    "WAITFOR (RECEIVE TOP (1) * FROM dbo.q), TIMEOUT 1000",
    "DECLARE @h uniqueidentifier\nSEND ON CONVERSATION @h ('x')",
    "DECLARE @h uniqueidentifier, @g uniqueidentifier\nMOVE CONVERSATION @h TO @g",
    # Found by the self-check, each run by SQL Server 2022 as described.
    "SELECT throw INTO t2 FROM t", "IF 1 = 1 DISABLE TRIGGER tr ON t",
    "IF 1 = 1 SELECT 1 ELSE DISABLE TRIGGER ALL ON t",
    "SELECT 1\nADD SENSITIVITY CLASSIFICATION TO dbo.t.a WITH (LABEL = 'x')", "SELECT NEXT VALUE FOR dbo.s",
    "pragma x", "show x", "SET ANSI_NULLS ON; SET IMPLICIT_TRANSACTIONS ON",
    "(SELECT a INTO t2 FROM t) OPTION (USE HINT ('FORCE_LEGACY_CARDINALITY_ESTIMATION'))",
    # Fix round 1: ANSI_DEFAULTS turns IMPLICIT_TRANSACTIONS on (R-03b-T3a); locking hints write (R-03b-T3b)
    "SET ANSI_DEFAULTS ON", "SET ANSI_NULLS, ANSI_DEFAULTS ON", "SET STATISTICS ON", "SET STATISTICS IO, TIME",
    "SET STATISTICS IO, SHOWPLAN_XML ON", "SET DEADLOCK_PRIORITY LOW; SET XACT_ABORT ON", "SET TEXTSIZE 10\nDELETE FROM t",
    "SELECT * FROM t WITH (HOLDLOCK)", "SELECT * FROM t WITH (NOLOCK, SERIALIZABLE)", "SELECT a FROM t (REPEATABLEREAD)",
    "SET @x = (SELECT a FROM t WITH (HOLDLOCK))", "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE; SELECT * FROM t WITH (XLOCK)",
    "SELECT * FROM t WITH (INDEX = level SERIALIZABLE)",
    # R-03b-T3c: the Service Broker statements themselves still block
    "IF 1 = 1 RECEIVE TOP (1) * FROM dbo.q", "RECEIVE * FROM dbo.q", "SELECT 1; RECEIVE * FROM dbo.q",
    "WAITFOR (GET CONVERSATION GROUP @g FROM dbo.q), TIMEOUT 1000", "BEGIN CATCH RECEIVE TOP (1) * FROM dbo.q END CATCH",
    "DECLARE @h uniqueidentifier\nBEGIN CONVERSATION TIMER (@h) TIMEOUT = 60",
])
def test_tsql_read_only_blocks(sql):
    assert decide_ts(sql, read_only=True) == (A.BLOCK, ["read_only"])


def test_tsql_levels():
    assert decide_ts("SET NOCOUNT ON", "strict") == (A.CONFIRM, ["strict_session"])
    assert decide_ts("EXEC sp_who", "strict") == (A.CONFIRM, ["strict_unknown"])
    assert decide_ts("SELECT 1\nDELETE FROM t") == (A.CONFIRM, ["delete_without_where"])


@pytest.mark.parametrize("sql, reasons", [  # R-03b-P3: the standard level still asks, with the reason
    ("SET NOCOUNT ON\nDELETE FROM t", ["delete_without_where"]),
    ("SET XACT_ABORT ON\nUPDATE t SET a = 1", ["update_without_where"]),
    ("ALTER TABLE t ADD c int\nDROP TABLE u", ["drop"]),
    ("GRANT SELECT ON t TO u\nDROP TABLE t", ["drop"]),
    ("DENY SELECT ON t TO u\nTRUNCATE TABLE t", ["truncate"]),
    ("REVOKE SELECT ON t FROM u\nDELETE FROM t", ["delete_without_where"]),
    # Found by the self-check: SQL Server ran each DELETE / UPDATE / DROP, which the standard level must confirm.
    ("SELECT 1 AS after\nDELETE FROM t", ["delete_without_where"]),
    ("ALTER LOGIN l WITH CHECK_POLICY = ON\nDELETE FROM t", ["delete_without_where"]),
    ("ALTER DATABASE CURRENT SET ANSI_NULLS ON\nUPDATE t SET a = 1", ["update_without_where"]),
    ("ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES u (id) ON UPDATE CASCADE\nSET XACT_ABORT ON\n"
     "DELETE FROM t", ["delete_without_where"]),
    ("ALTER TABLE t ADD c int\nDROP ASSEMBLY a", ["drop"]),
    ("ALTER PARTITION FUNCTION pf() MERGE RANGE (1)\nDROP TABLE t", ["drop"]),
])
def test_tsql_standard_level_keeps_danger_reasons(sql, reasons):
    assert decide_ts(sql) == (A.CONFIRM, reasons)
