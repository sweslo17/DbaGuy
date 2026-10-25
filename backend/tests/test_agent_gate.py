"""The agent gate (spec ⑧ D-08-2, CR-08-1, CR-08-4, CR-08-6): appendix A's G1–G10, H1–H3 and the Datadog variants, per
dialect, as regression tests. Pure."""

import pytest

from dbaguy_backend.services.agent_gate import AgentRefusal, agent_read
from dbaguy_backend.sql.dialects import Dialect

PG, MY, LITE, TS, PP = Dialect.POSTGRES, Dialect.MYSQL, Dialect.SQLITE, Dialect.TSQL, Dialect.SQLPP
R = AgentRefusal

ALLOWED = [
    (PG, "SELECT 1"), (PG, "select * from public.customers where id = 1;"),
    (PG, "WITH x AS (SELECT 1) SELECT * FROM x"),
    (PG, "VALUES (1), (2)"), (PG, "TABLE public.customers"), (PG, "SHOW search_path"),
    (PG, 'SELECT "PG_TERMINATE_BACKEND"(1)'),  # a quoted name keeps its case: another function, which does not exist
    (PG, "SELECT 'pg_terminate_backend(1)'"), (PG, "SELECT $$pg_read_file('x')$$"),  # text, never a call
    (PG, "SELECT pg_terminate_backend_note FROM t"),  # a column, not a call
    (PG, "SELECT /* nested /* comment */ ; */ 1"), (PG, "SELECT E'a\\'; DROP TABLE t; --'"),
    (MY, "SELECT * FROM customers"), (MY, "SHOW TABLES"), (MY, "TABLE customers"), (MY, "VALUES ROW(1)"),
    (MY, "SELECT `into` FROM t"), (MY, "SELECT 'a\\' INTO @x -- '"),  # INTO inside the string, as the server reads it
    (LITE, "SELECT * FROM sqlite_master"), (LITE, "VALUES (1)"),
    (LITE, "WITH RECURSIVE c(n) AS (SELECT 1) SELECT n FROM c"),
    (TS, "SELECT TOP 5 * FROM dbo.customers"), (TS, "WITH c AS (SELECT 1 AS a) SELECT * FROM c"),
    (PP, "SELECT * FROM `travel-sample`.inventory.airline LIMIT 5"), (PP, "WITH a AS (1) SELECT a"),
    (PP, 'SELECT META(d).id FROM app.users AS d USE KEYS "u1"'),
]


@pytest.mark.parametrize(("dialect", "sql"), ALLOWED)
def test_one_read_on_the_allow_list_passes(dialect, sql):
    verdict = agent_read(sql, dialect)
    assert verdict.allowed, verdict.message()


REFUSED = [
    # G1, G2, G6: stacked statements (Datadog: SELECT 1; COMMIT; DROP …), however they hide
    (PG, "SELECT 1; COMMIT; DROP TABLE zz_08_t", R.SEVERAL_STATEMENTS),
    (PG, "SELECT $$;$$; COMMIT", R.SEVERAL_STATEMENTS),
    (PG, "SELECT 1;\nCOMMIT", R.SEVERAL_STATEMENTS),
    (MY, "SELECT 1; COMMIT; DROP TABLE zz_08_t", R.SEVERAL_STATEMENTS),
    (MY, "DELIMITER $$\nSELECT 1$$", R.SEVERAL_STATEMENTS),
    (LITE, "SELECT 1; DELETE FROM t", R.SEVERAL_STATEMENTS),
    (PP, "SELECT 1; DELETE FROM app.users", R.SEVERAL_STATEMENTS),
    # G4, G5, T1: T-SQL needs no semicolon
    (TS, "SELECT 1 SELECT 2", R.SEVERAL_STATEMENTS),
    (TS, "SELECT 1 COMMIT", R.SEVERAL_STATEMENTS),
    (TS, "SELECT 1; COMMIT; DROP TABLE dbo.zz_08_victim", R.SEVERAL_STATEMENTS),
    (TS, "SELECT 1\nGO", R.SEVERAL_STATEMENTS),
    (TS, "SELECT 1\nGO 5", R.SEVERAL_STATEMENTS),
    # CR-08-6, H3: read-classified statements that are not queries
    (TS, "DECLARE zz_08_c CURSOR GLOBAL FOR SELECT 1", R.NOT_ALLOWED_START),
    (TS, "WAITFOR DELAY '00:00:05'", R.NOT_ALLOWED_START),
    (TS, "PRINT 'x'", R.NOT_ALLOWED_START),
    (TS, "THROW 50000, 'x', 1", R.NOT_ALLOWED_START),
    (TS, "OPEN zz_08_c", R.NOT_ALLOWED_START),
    (TS, "FETCH NEXT FROM zz_08_c", R.NOT_ALLOWED_START),
    (TS, "RAISERROR('x', 10, 1) WITH LOG", R.NOT_ALLOWED_START),
    (TS, "EXEC sp_who", R.NOT_ALLOWED_START),
    (LITE, "PRAGMA wal_checkpoint(TRUNCATE)", R.NOT_ALLOWED_START),
    (LITE, "PRAGMA query_only = 0", R.NOT_ALLOWED_START),
    (LITE, "ATTACH 'x.db' AS x", R.NOT_ALLOWED_START),
    (LITE, "VACUUM INTO '/tmp/zz_08.db'", R.NOT_ALLOWED_START),
    (PP, "INFER app.users", R.NOT_ALLOWED_START),
    (PP, "PREPARE zz_08_p FROM SELECT 1", R.NOT_ALLOWED_START),
    (PP, "PREPARE SELECT 1", R.NOT_ALLOWED_START), (PP, "PREPARE zz_08_p AS SELECT 1", R.NOT_ALLOWED_START),
    (PP, "EXECUTE zz_08_p", R.NOT_ALLOWED_START), (PP, "EXECUTE zz_08_p USING [1]", R.NOT_ALLOWED_START),
    (PP, "ADVISE SELECT * FROM app.users", R.NOT_ALLOWED_START),
    (PG, "COPY (SELECT 1) TO '/dev/null'", R.NOT_ALLOWED_START),
    (PG, "COPY (SELECT 1) TO PROGRAM 'true'", R.NOT_ALLOWED_START),
    (PG, "SET default_transaction_read_only = off", R.NOT_ALLOWED_START),
    (PG, "DO $$BEGIN PERFORM 1; END$$", R.NOT_ALLOWED_START),
    (PG, "CALL zz_08_p()", R.NOT_ALLOWED_START),
    (MY, "CALL zz_08_p()", R.NOT_ALLOWED_START),
    (PG, "(SELECT 1)", R.NOT_ALLOWED_START),
    (PG, "EXPLAIN SELECT 1", R.EXPLAIN_ADDED), (MY, "DESCRIBE customers", R.EXPLAIN_ADDED),
    (MY, "DESC customers", R.EXPLAIN_ADDED), (PP, "EXPLAIN SELECT 1", R.EXPLAIN_ADDED),
    # H3-a: data-modifying CTEs are not reads
    (PG, "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", R.NOT_A_READ),
    (PG, "WITH d AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM d", R.NOT_A_READ),
    (MY, "WITH c AS (SELECT 1) DELETE FROM t", R.NOT_A_READ),
    (LITE, "WITH c AS (SELECT 1) INSERT INTO t SELECT * FROM c", R.NOT_A_READ),
    (TS, "WITH c AS (SELECT 1 AS a) DELETE FROM t", R.NOT_A_READ),
    (TS, "SELECT * INTO dbo.zz_08_new FROM dbo.customers", R.NOT_A_READ),
    (PP, "WITH x AS (1) DELETE FROM app.users", R.NOT_A_READ),
    (PG, "SELECT * FROM t FOR UPDATE", R.NOT_A_READ), (MY, "SELECT * FROM t LOCK IN SHARE MODE", R.NOT_A_READ),
    (TS, "SELECT NEXT VALUE FOR dbo.zz_08_seq", R.NOT_A_READ),  # T3: a sequence is not rolled back
    (TS, "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'SELECT 1')", R.DENIED_FUNCTION),  # fix round 1 I1
    # fix round 1: the deny-list runs before the classifier, so this says what it does (was "not a read (read)")
    (PG, "SELECT set_config('default_transaction_read_only', 'off', false)", R.DENIED_FUNCTION),
    # G3, Q1-P, CR-08-1: what a read-only transaction does not stop
    (PG, "SELECT pg_terminate_backend(1)", R.DENIED_FUNCTION),
    (PG, "SELECT pg_cancel_backend(pid) FROM pg_stat_activity", R.DENIED_FUNCTION),
    (PG, "SELECT pg_advisory_lock(808)", R.DENIED_FUNCTION),
    (PG, "SELECT pg_try_advisory_xact_lock(808)", R.DENIED_FUNCTION),
    (PG, "SELECT lo_export(0, '/tmp/zz_08')", R.DENIED_FUNCTION),
    (PG, "SELECT lo_import('/etc/hostname')", R.DENIED_FUNCTION),
    (PG, "SELECT pg_read_file('/etc/hostname')", R.DENIED_FUNCTION),
    (PG, "SELECT pg_read_binary_file('/etc/hostname')", R.DENIED_FUNCTION),
    (PG, "SELECT * FROM pg_ls_dir('.')", R.DENIED_FUNCTION),
    (PG, "SELECT pg_ls_logdir()", R.DENIED_FUNCTION),
    (PG, "SELECT pg_stat_file('/etc/hostname')", R.DENIED_FUNCTION),
    (PG, "SELECT dblink_exec('x', 'DROP TABLE t')", R.DENIED_FUNCTION),
    (PG, "SELECT pg_reload_conf()", R.DENIED_FUNCTION),
    (PG, "SELECT pg_create_restore_point('x')", R.DENIED_FUNCTION),
    (PG, "SELECT pg_catalog.pg_terminate_backend(1)", R.DENIED_FUNCTION),
    (PG, 'SELECT "pg_catalog"."pg_terminate_backend"(1)', R.DENIED_FUNCTION),
    (PG, "SELECT PG_TERMINATE_BACKEND (1)", R.DENIED_FUNCTION),
    (PG, "SELECT pg_terminate_backend/* x */(1)", R.DENIED_FUNCTION),
    (PG, "SELECT 1 WHERE EXISTS (SELECT pg_terminate_backend(pid) FROM pg_stat_activity)", R.DENIED_FUNCTION),
    # H1, CR-08-4: U& names (with UESCAPE) and SQL run from text
    (PG, 'SELECT U&"pg\\005fterminate\\005fbackend"(1)', R.DENIED_FUNCTION),
    (PG, 'SELECT u&"pg\\005fterminate\\005fbackend"(1)', R.DENIED_FUNCTION),
    (PG, "SELECT U&\"pg!005fterminate!005fbackend\" UESCAPE '!' (1)", R.DENIED_FUNCTION),
    (PG, 'SELECT U&"pg\\+00005fterminate\\005fbackend"(1)', R.DENIED_FUNCTION),
    (PG, 'SELECT U&"set\\005fconfig"(\'a\', \'b\', false)', R.DENIED_FUNCTION),
    (PG, 'SELECT U&"pg\\zzzzterminate"(1)', R.UNREADABLE_NAME),
    (PG, "SELECT query_to_xml('SELECT pg_terminate_backend(1)', true, true, '')", R.DENIED_FUNCTION),
    (PG, "SELECT query_to_xmlschema('SELECT 1', true, true, '')", R.DENIED_FUNCTION),
    (PG, "SELECT query_to_xml_and_xmlschema('SELECT 1', true, true, '')", R.DENIED_FUNCTION),
    (PG, "SELECT cursor_to_xml('c', 1, true, true, '')", R.DENIED_FUNCTION),
    (PG, "SELECT * FROM ts_stat('SELECT to_tsvector(''simple'', 1::text)')", R.DENIED_FUNCTION),
    (PG, "SELECT ts_rewrite('a'::tsquery, 'SELECT 1')", R.DENIED_FUNCTION),
    # G8, M5, M6, H2: MySQL locks, files, INTO
    (MY, "SELECT GET_LOCK('zz_08', 1)", R.DENIED_FUNCTION),
    (MY, "SELECT release_all_locks()", R.DENIED_FUNCTION),
    (MY, "SELECT LOAD_FILE('/etc/hostname')", R.DENIED_FUNCTION),
    (MY, "SELECT `get_lock`('zz_08', 1)", R.DENIED_FUNCTION),
    (MY, "SELECT 1 INTO @zz_08", R.INTO),
    (MY, "SELECT 1 INTO OUTFILE '/tmp/zz_08'", R.NOT_A_READ),
    (MY, "SELECT 1 FROM dual INTO DUMPFILE '/tmp/zz_08'", R.NOT_A_READ),
    (MY, "SELECT * FROM customers LIMIT 1 INTO @a, @b, @c, @d, @e, @f", R.INTO),
    (MY, "SELECT 1 /*!50000 , GET_LOCK('a', 1) */", R.EXECUTABLE_COMMENT),
    (MY, "SELECT /*+ SET_VAR(max_execution_time=0) */ 1", R.EXECUTABLE_COMMENT),
    (MY, "/*!50000 DROP TABLE t */", R.NOT_ALLOWED_START),
    # G9, S6: SQLite
    (LITE, "SELECT load_extension('x')", R.DENIED_FUNCTION),
    # G10, C3, C4, H3-d: SQL++
    (PP, "SELECT CURL('http://127.0.0.1')", R.DENIED_FUNCTION),
    (PP, 'SELECT ADVISOR({"action": "start", "duration": "2s"})', R.DENIED_FUNCTION),
    (PP, "UPDATE STATISTICS FOR app.users(name)", R.NOT_ALLOWED_START),
    (PP, "EXECUTE FUNCTION zz_08_f()", R.NOT_ALLOWED_START),
    (PP, "SELECT NEXT VALUE FOR zz_08_seq", R.NOT_A_READ),
    # D-08-2 ①: text a driver would cut or cannot send
    (PG, "SELECT 1\x00; DROP TABLE t", R.INVALID_TEXT), (MY, "SELECT '\ud800'", R.INVALID_TEXT),
    (PG, "", R.EMPTY), (PG, " -- only a comment\n", R.EMPTY), (TS, ";", R.EMPTY),
]


@pytest.mark.parametrize(("dialect", "sql", "refusal"), REFUSED)
def test_what_a_read_only_transaction_does_not_stop_is_refused(dialect, sql, refusal):
    verdict = agent_read(sql, dialect)
    assert verdict.refusal == refusal, verdict.message()
    assert verdict.message().startswith("DbaGuy runs exactly one read statement per call: ")


def test_the_messages_say_why():
    assert agent_read("DECLARE c CURSOR FOR SELECT 1", TS).message() == (
        "DbaGuy runs exactly one read statement per call: starts with DECLARE: only SELECT, WITH are run")
    assert agent_read("SELECT pg_ls_dir('.')", PG).message().endswith("pg_ls_dir (server files) is not allowed")
    assert agent_read("SELECT pg_advisory_lock(1)", PG).message().endswith("pg_advisory_lock (locks) is not allowed")
    assert agent_read("SELECT query_to_xml('x', true, true, '')", PG).message().endswith(
        "query_to_xml (runs SQL from text) is not allowed")
    assert agent_read("WITH d AS (DELETE FROM t RETURNING *) SELECT 1", PG).message().endswith("not a read (write)")
    assert agent_read("SELECT 1 INTO @x", MY).message().endswith("INTO is not allowed")
    assert agent_read("SHOW TABLES", LITE).message().endswith("starts with SHOW: only SELECT, WITH, VALUES are run")


@pytest.mark.parametrize(("dialect", "sql"), [
    (PG, "EXPLAIN SELECT * FROM t"), (MY, "EXPLAIN FORMAT=TREE SELECT * FROM t"), (MY, "EXPLAIN SELECT 1"),
    (LITE, "EXPLAIN QUERY PLAN SELECT * FROM t"), (PP, "EXPLAIN SELECT * FROM app.users"),
])
def test_explain_takes_the_tools_own_head_over_one_allowed_read(dialect, sql):
    assert agent_read(sql, dialect, "explain").allowed


@pytest.mark.parametrize(("dialect", "sql", "refusal"), [
    (PG, "EXPLAIN ANALYZE SELECT 1", R.NOT_ALLOWED_START),  # OP-6: no ANALYZE, no DML
    (PG, "EXPLAIN INSERT INTO t VALUES (1)", R.NOT_ALLOWED_START),
    (PG, "EXPLAIN SELECT 1; DROP TABLE t", R.SEVERAL_STATEMENTS),
    (PG, "SELECT 1", R.NOT_ALLOWED_START),
    (MY, "EXPLAIN SELECT GET_LOCK('a', 1)", R.DENIED_FUNCTION),
    (TS, "SELECT 1", R.NOT_ALLOWED_START),  # T-SQL's explain sends the statement itself, as a query
])
def test_explain_refuses_anything_else(dialect, sql, refusal):
    assert agent_read(sql, dialect, "explain").refusal == refusal


@pytest.mark.parametrize(("dialect", "sql"), [  # ruling J1: the pre-flight's notes, closed by name
    (PG, "SELECT pg_stat_reset()"), (PG, "SELECT pg_stat_reset_shared('bgwriter')"),
    (PG, "SELECT pg_backup_start('x')"), (PG, "SELECT pg_backup_stop()"),
    (PG, "SELECT pg_wal_replay_pause()"), (PG, "SELECT pg_logical_emit_message(false, 'p', 'x')"),
    (TS, "SELECT * FROM sys.fn_dblog(NULL, NULL)"),
    (TS, "SELECT * FROM [sys].[FN_GET_AUDIT_FILE]('x', DEFAULT, DEFAULT)"),
    (TS, "SELECT * FROM sys.fn_xe_file_target_read_file('x', NULL, NULL, NULL)"),
    (TS, "SELECT * FROM fn_trace_gettable('x', DEFAULT)"),
])
def test_server_side_effects_and_server_files_are_refused(dialect, sql):
    assert agent_read(sql, dialect).refusal == R.DENIED_FUNCTION


@pytest.mark.parametrize(("dialect", "sql"), [  # reads that only look like them stay allowed
    (PG, "SELECT pg_walfile_name(pg_current_wal_lsn())"), (PG, "SELECT * FROM pg_stat_activity"),
    (TS, "SELECT fn_dblog_note FROM dbo.t"),
])
def test_names_that_only_look_alike_pass(dialect, sql):
    assert agent_read(sql, dialect).allowed


@pytest.mark.parametrize(("dialect", "sql"), [  # fix round 1: the gate, not only the classifier, closes these by name
    (TS, "SELECT * FROM OPENQUERY(zz_08_srv, 'SELECT 1')"),
    (TS, "SELECT * FROM OPENDATASOURCE('SQLNCLI', 'Data Source=zz_08').db.dbo.t"),
    (TS, "SELECT * FROM OPENROWSET(BULK '/tmp/zz_08.txt', SINGLE_CLOB) AS x"),
    (LITE, "SELECT fts3_tokenizer('simple')"),
])
def test_remote_and_file_readers_and_code_loaders_are_denied_by_name(dialect, sql):
    assert agent_read(sql, dialect).refusal == R.DENIED_FUNCTION


def test_the_sql_server_readers_say_what_they_reach():
    assert agent_read("SELECT * FROM OPENROWSET(BULK 'x', SINGLE_CLOB) AS x", TS).message().endswith(
        "openrowset (server files) is not allowed")
    assert agent_read("SELECT * FROM OPENQUERY(s, 'SELECT 1')", TS).message().endswith(
        "openquery (opens other connections) is not allowed")


@pytest.mark.parametrize("sql", [  # final review I1: what keeps or consumes a replication slot outlives the ROLLBACK
    "SELECT pg_copy_physical_replication_slot('zz_08_ff_a', 'zz_08_ff_b')",
    "SELECT * FROM pg_copy_logical_replication_slot('zz_08_ff_a', 'zz_08_ff_b')",
    "SELECT * FROM pg_logical_slot_get_changes('zz_08_ff_a', NULL, NULL)",
    "SELECT * FROM pg_logical_slot_get_binary_changes('zz_08_ff_a', NULL, NULL)",
    "SELECT * FROM pg_logical_slot_peek_changes('zz_08_ff_a', NULL, NULL)",
    "SELECT * FROM pg_logical_slot_peek_binary_changes('zz_08_ff_a', NULL, NULL)",
    "SELECT pg_sync_replication_slots()",
    "SELECT binary_upgrade_replorigin_advance('x', '0/0')", "SELECT pg_stat_statements_reset()",
    "SELECT pg_catalog.pg_copy_physical_replication_slot('zz_08_ff_a', 'zz_08_ff_b')",
    'SELECT "pg_catalog"."pg_logical_slot_get_changes"(\'zz_08_ff_a\', NULL, NULL)',
    "SELECT PG_COPY_LOGICAL_REPLICATION_SLOT('zz_08_ff_a', 'zz_08_ff_b')",
    'SELECT U&"pg\\005fcopy\\005fphysical\\005freplication\\005fslot"(\'zz_08_ff_a\', \'zz_08_ff_b\')',
    "SELECT U&\"pg!005flogical!005fslot!005fget!005fchanges\" UESCAPE '!' ('zz_08_ff_a', NULL, NULL)",
])
def test_postgres_replication_slots_are_denied_however_they_are_named(sql):
    verdict = agent_read(sql, PG)
    assert verdict.refusal == R.DENIED_FUNCTION, verdict.message()


@pytest.mark.parametrize("sql", [  # final review I2: the host's file system, for any login
    "SELECT * FROM sys.dm_os_enumerate_filesystem('/etc', '*')",
    "SELECT * FROM [sys].[dm_os_enumerate_filesystem](N'/etc', N'*')",
    "SELECT file_exists FROM sys.dm_os_file_exists('/etc/passwd')",
    "SELECT * FROM master.sys.DM_OS_FILE_EXISTS(N'/etc/passwd')",
    "SELECT TOP 1 * FROM sys.fn_dblog_xtp(NULL, NULL)", "SELECT TOP 1 * FROM [sys].[fn_full_dblog](NULL, NULL, 5)",
    "SELECT * FROM sys.fn_dump_dblog_xtp(NULL, NULL, N'DISK', 1, N'/tmp/x')",
    "SELECT * FROM sys.fn_db_backup_file_snapshots(N'dbaguy')",
    "SELECT * FROM sys.dm_xcs_enumerate_blobdirectory(N'/etc/', NULL, NULL, 0, 0)",
    "SELECT * FROM sys.fn_xcs_get_file_rowcount(0, NULL, 0, N'/', N'x', NULL, NULL)",
    "SELECT * FROM sys.dm_os_enumerate_fixed_drives",  # a view: no call, its name alone
    "SELECT d.free_space_in_bytes FROM [sys].[DM_OS_ENUMERATE_FIXED_DRIVES] AS d",
    "WITH d AS (SELECT * FROM master.sys.dm_os_enumerate_fixed_drives) SELECT * FROM d",
])
def test_sql_server_file_system_readers_are_denied_however_they_are_named(sql):
    verdict = agent_read(sql, TS)
    assert verdict.refusal == R.DENIED_FUNCTION, verdict.message()


def test_the_new_denials_say_what_they_reach():
    assert agent_read("SELECT pg_copy_logical_replication_slot('a', 'b')", PG).message().endswith(
        "pg_copy_logical_replication_slot (keeps or consumes a replication slot) is not allowed")
    assert agent_read("SELECT * FROM sys.dm_os_file_exists('/x')", TS).message().endswith(
        "dm_os_file_exists (server files) is not allowed")
    assert agent_read("SELECT * FROM sys.dm_os_enumerate_fixed_drives", TS).message().endswith(
        "dm_os_enumerate_fixed_drives (server files) is not allowed")


@pytest.mark.parametrize(("dialect", "sql"), [  # what stays allowed beside them
    (TS, "SELECT * FROM sys.dm_os_host_info"), (TS, "SELECT physical_name FROM sys.database_files"),
    (TS, "SELECT name FROM sys.servers"),
    (PG, "SELECT * FROM pg_replication_slots"), (PG, "SELECT * FROM pg_stat_replication_slots"),
])
def test_catalog_views_beside_them_stay_allowed(dialect, sql):
    assert agent_read(sql, dialect).allowed
