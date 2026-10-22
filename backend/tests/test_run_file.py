"""Run SQL file on the dev databases (spec ⑥ §3.4, R-06-7, §6): both modes, the outcomes a server can force (an
error, a cancel, MySQL's DDL commit, the file's own COMMIT), a read-only connection, the inspection, and files other
tools wrote (A8: pg_dump --inserts and its COPY form, mysqldump with and without --hex-blob, sqlite3 .dump, an SSMS
script). Scratch objects are `zz_06_*`, dropped by exact name; Couchbase writes only `zz_06_run_*` keys."""

import time
from datetime import UTC, datetime
from pathlib import Path

import mysql.connector
import pytest

from dbaguy_backend.domain.errors import ReadOnlyProfile
from dbaguy_backend.domain.execution import QueryContext, RunFileCommand
from dbaguy_backend.sessions.jobs.run_file import PARTLY_APPLIED
from dbaguy_backend.sql.dialects import Dialect, script_text
from tests.data_support import DataApp, dropping, scratch_name
from tests.edit_support import ENGINES, ScratchDb, scratch_db
from tests.support import COUCHBASE_PASSWORD, couchbase_query, make_couchbase_profile, mysql_driver_kwargs, \
    requires_couchbase, requires_mssql, requires_mysql, requires_pg, unique_name, wait_until

FIXTURES = Path(__file__).parent / "fixtures" / "run_file"
TOOL_TABLE = "zz_06_tool_tbl"  # the table name the tool fixtures were written with
SLEEP = {  # a statement that runs until it is cancelled
    "postgres": "SELECT pg_sleep(30)", "mysql": "SELECT SLEEP(30)", "mssql": "WAITFOR DELAY '00:00:30'",
    "sqlite": "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 1000000000) "
              "SELECT count(*) FROM n",
}


@pytest.fixture(params=ENGINES)
def env(request, tmp_path):
    db = scratch_db(request.param, tmp_path)
    app = DataApp(tmp_path)
    try:
        yield db, app, app.add_profile(db.profile())
    finally:
        app.close()
        db.close()


def write(tmp_path: Path, db: ScratchDb, statements: list[str], name: str = "run.sql") -> Path:
    """`statements` as one file of the engine: `;` after each, or a GO line after each T-SQL batch."""
    path = tmp_path / name
    path.write_text(script_text(db.dialect, statements) + "\n", encoding="utf-8")
    return path


def make_table(db: ScratchDb, made: list) -> str:
    name = scratch_name("run")
    made.append(("TABLE", name))
    db.execute(f"CREATE TABLE {db.table(name)} (id INT PRIMARY KEY, label VARCHAR(50)){db.table_options}")
    return name


def inserts(db: ScratchDb, table: str, *ids: int) -> list[str]:
    return [f"INSERT INTO {db.table(table)} VALUES ({i}, 'row {i}')" for i in ids]


def ids(db: ScratchDb, table: str) -> list[int]:
    return [row[0] for row in db.rows(f"SELECT id FROM {db.table(table)} ORDER BY id")]


def command(path: Path, mode: str, **fields):
    return lambda rid: RunFileCommand(request_id=rid, path=str(path), mode=mode, **fields)


def line_of(path: Path, text: str) -> int:
    return next(i for i, line in enumerate(path.read_text().splitlines(), start=1) if text in line)


def test_all_or_nothing_applies_the_whole_file_and_records_it(env, tmp_path):
    db, app, profile = env
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, inserts(db, table, 1, 2, 3))
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        assert (ran.outcome.outcome, ran.outcome.statements, ran.outcome.rows, ran.error) == ("completed", 3, 3, None)
        assert ids(db, table) == [1, 2, 3]
        [record] = app.history()
        assert (record.status, record.rows) == ("ok", 3)
        assert record.sql.startswith(f"-- Run SQL file {path} (0.0 MB), all or nothing")


def test_all_or_nothing_an_error_applies_nothing(env, tmp_path):
    db, app, profile = env
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, inserts(db, table, 1, 2, 1, 4))  # the second 1 is a duplicate key
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        assert (ran.outcome.outcome, ran.outcome.stopped_at.statement) == ("rolled_back", 3)
        assert ran.outcome.stopped_at.line == (5 if db.dialect is Dialect.TSQL else 3)  # T-SQL: a GO line after each
        assert ran.error is not None and ids(db, table) == []
        assert app.history()[0].status == "error"


def test_statement_by_statement_stops_or_skips(env, tmp_path):
    db, app, profile = env
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, inserts(db, table, 1, 2, 1, 4))
        tab = app.job_session(profile)
        stopped = tab.run(command(path, "statements"))
        assert (stopped.outcome.outcome, stopped.outcome.statements, stopped.outcome.stopped_at.statement) == (
            "stopped", 2, 3)
        assert ids(db, table) == [1, 2]
        again = tab.run(command(path, "statements", from_statement=4))  # "run again from statement 4"
        assert (again.outcome.outcome, again.outcome.statements) == ("completed", 1)
        assert ids(db, table) == [1, 2, 4]
        db.execute(f"DELETE FROM {db.table(table)}")
        skipped = tab.run(command(path, "statements", on_error="continue"))
        assert (skipped.outcome.outcome, skipped.outcome.statements, skipped.outcome.error_count) == ("completed", 3, 1)
        assert skipped.outcome.errors[0].statement == 3 and ids(db, table) == [1, 2, 4]


@pytest.mark.parametrize("mode", ["all_or_nothing", "statements"])
def test_a_cancel_rolls_back_or_stops_where_it_reached(env, tmp_path, mode):
    db, app, profile = env
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, [*inserts(db, table, 1), SLEEP[db.engine], *inserts(db, table, 3)])
        tab = app.job_session(profile)
        rid = tab.submit(command(path, mode))
        tab.wait_for_progress(rid, lambda p: p.statements == 1)
        time.sleep(0.5)  # the long statement has started
        tab.worker.cancel(rid)
        ran = tab.wait(rid)
        expected = "rolled_back" if mode == "all_or_nothing" else "stopped"
        # MySQL: the KILL ends the SLEEP(), which returns 1, and the SELECT completes: applied, so the run stops
        # before the next statement (R8)
        at = 3 if db.engine == "mysql" else 2
        assert (ran.outcome.outcome, ran.outcome.cancelled, ran.outcome.stopped_at.statement) == (expected, True, at)
        assert ran.outcome.statements == at - 1
        assert ids(db, table) == ([] if mode == "all_or_nothing" else [1])


def test_the_files_own_commit_stops_an_all_or_nothing_run(env, tmp_path):
    db, app, profile = env
    commit = "COMMIT TRANSACTION" if db.dialect is Dialect.TSQL else "COMMIT"
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, [*inserts(db, table, 1), commit, *inserts(db, table, 2)])
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        assert (ran.outcome.outcome, ran.error.code) == ("rolled_back", "transaction_in_file")
        assert ids(db, table) == []


def test_a_read_only_connection_is_refused_before_anything(env, tmp_path):
    db, app, _ = env
    profile = app.add_profile(db.profile(read_only=True))
    path = write(tmp_path, db, ["SELECT 1"])
    with pytest.raises(ReadOnlyProfile):
        app.inspect(profile, path)
    ran = app.job_session(profile).run(command(path, "statements"))
    assert (ran.outcome.outcome, ran.error.code) == ("not_started", "read_only")


@requires_mysql
def test_mysql_all_or_nothing_refuses_ddl_before_sending_it(tmp_path):  # pre-flight H1 (was: partial after M8)
    db = scratch_db("mysql", tmp_path)
    app = DataApp(tmp_path)
    profile = app.add_profile(db.profile())
    try:
        with dropping(db, app.close_sessions) as made:
            table = make_table(db, made)
            other = scratch_name("ddl")
            made.append(("TABLE", other))
            path = write(tmp_path, db, [*inserts(db, table, 1), f"CREATE TABLE {db.table(other)} (a INT)",
                                        *inserts(db, table, 2)])
            ran = app.job_session(profile).run(command(path, "all_or_nothing"))
            assert (ran.outcome.outcome, ran.outcome.stopped_at.statement, ran.error.code) == (
                "rolled_back", 2, "implicit_commit")
            assert ids(db, table) == [] and not db.exists(other)  # nothing committed: the DDL was never sent
            # a failing DDL would have committed the INSERT before failing (M8, H1 probe 1): never sent either
            failing = write(tmp_path, db, [*inserts(db, table, 1), f"CREATE TABLE {db.table(table)} (a INT)"],
                            name="failing.sql")
            ran = app.job_session(profile).run(command(failing, "all_or_nothing"))
            assert (ran.outcome.outcome, ran.error.code, ids(db, table)) == ("rolled_back", "implicit_commit", [])
    finally:
        app.close()
        db.close()


@requires_mysql
def test_mysql_ddl_inside_the_files_own_transaction_commits_it_before_failing(tool_env, tmp_path):  # H2 × M8
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, ["START TRANSACTION", *inserts(db, table, 1),
                                    f"CREATE TABLE {db.table(table)} (a INT)", *inserts(db, table, 3), "COMMIT"])
        ran = app.job_session(profile).run(command(path, "statements"))
        # MySQL committed the INSERT before the CREATE failed: it is applied, and the outcome says so
        assert (ran.outcome.outcome, ran.outcome.stopped_at.statement, ran.outcome.statements) == ("stopped", 3, 2)
        assert ran.outcome.warnings == ["Line 3 committed the file's transaction from line 1 before it failed "
                                        "(MySQL commits around DDL)"]
        assert ids(db, table) == [1]


def cancel_when(db: ScratchDb, tab, rid: str, sql: str, condition: str) -> None:
    """Cancel the job once MySQL shows `sql` (a LIKE pattern) running with `condition`."""
    query = ("SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE ID <> CONNECTION_ID() "
             f"AND INFO LIKE '{sql}' AND {condition}")
    assert wait_until(lambda: db.rows(query) == [(1,)], 20)
    tab.worker.cancel(rid)


@requires_mysql
def test_a_cancelled_mysql_ddl_inside_the_files_transaction_had_committed_it(tool_env, tmp_path):  # H2 × M8
    """A cancel is not a server error: the adapter does not read MySQL's flags again (R-03a-T9b), and they still say
    the transaction is open. MySQL committed it before the CREATE began; the KILL stopped the CREATE itself."""
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        other = scratch_name("ctas")
        made.append(("TABLE", other))
        path = write(tmp_path, db, ["START TRANSACTION", *inserts(db, table, 1),
                                    "SET SESSION cte_max_recursion_depth = 1000000000",
                                    f"CREATE TABLE {db.table(other)} AS WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL "
                                    "SELECT i + 1 FROM n WHERE i < 1000000000) SELECT COUNT(*) AS c FROM n", "COMMIT"])
        tab = app.job_session(profile)
        rid = tab.submit(command(path, "statements"))
        cancel_when(db, tab, rid, f"CREATE TABLE %{other}%", "TIME >= 1")
        ran = tab.wait(rid)
        assert (ran.outcome.outcome, ran.outcome.cancelled, ran.outcome.statements) == ("stopped", True, 3)
        assert ran.outcome.warnings == ["Line 4 committed the file's transaction from line 1 before it failed "
                                        "(MySQL commits around DDL)"]
        assert ids(db, table) == [1] and not db.exists(other)


@requires_mysql
def test_a_ddl_that_finished_before_its_cancel_counts_as_applied(tool_env, tmp_path):  # R8
    """KILL QUERY interrupts a SLEEP(), which returns 1, and the CREATE … SELECT goes on to its end: the adapter
    reports the cancel as having come after the statement completed, and the run counts it as applied."""
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        other = scratch_name("ctas")
        made.append(("TABLE", other))
        path = write(tmp_path, db, ["START TRANSACTION", *inserts(db, table, 1),
                                    f"CREATE TABLE {db.table(other)} AS SELECT SLEEP(30) AS s", "COMMIT"])
        tab = app.job_session(profile)
        rid = tab.submit(command(path, "statements"))
        cancel_when(db, tab, rid, f"CREATE TABLE %{other}%", "STATE = 'User sleep'")
        ran = tab.wait(rid)
        assert (ran.outcome.outcome, ran.outcome.cancelled, ran.outcome.statements,
                ran.outcome.stopped_at.statement, ran.outcome.warnings) == ("stopped", True, 3, 4, [])
        assert ids(db, table) == [1] and db.exists(other)


@requires_mysql
def test_a_mysqldump_file_all_or_nothing_leaves_the_table_it_would_drop(tool_env, tmp_path):  # H1 probe 2
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        db.execute(*inserts(db, table, 7))
        path = tool_file(tmp_path, "mysqldump.sql", table)
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        assert (ran.outcome.outcome, ran.error.code) == ("rolled_back", "implicit_commit")
        assert ran.outcome.stopped_at.line == line_of(path, "DROP TABLE IF EXISTS")
        assert ids(db, table) == [7]


def own_transaction(db: ScratchDb, table: str) -> list[str]:
    """A file with its own BEGIN … COMMIT around four INSERTs, the third a duplicate key (pre-flight H2's probe)."""
    begin, commit = {Dialect.TSQL: ("BEGIN TRANSACTION", "COMMIT TRANSACTION"),
                     Dialect.MYSQL: ("START TRANSACTION", "COMMIT")}.get(db.dialect, ("BEGIN", "COMMIT"))
    return [begin, *inserts(db, table, 1, 2, 1, 4), commit]


def test_a_stop_inside_the_files_own_transaction_says_nothing_of_it_is_applied(env, tmp_path):  # H2
    db, app, profile = env
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, own_transaction(db, table))
        ran = app.job_session(profile).run(command(path, "statements"))
        assert (ran.outcome.outcome, ran.outcome.stopped_at.statement, ran.outcome.statements) == ("stopped", 4, 0)
        assert ran.outcome.warnings[0].startswith("The file's transaction from line 1 (statement 1) was still open")
        assert ids(db, table) == []


def test_skipping_errors_inside_the_files_own_transaction(env, tmp_path):  # H2: PostgreSQL aborts it
    db, app, profile = env
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, own_transaction(db, table))
        ran = app.job_session(profile).run(command(path, "statements", on_error="continue"))
        if db.dialect is Dialect.POSTGRES:  # every statement after the error would fail (25P02), COMMIT roll back
            assert (ran.outcome.outcome, ran.outcome.stopped_at.statement, ran.outcome.statements) == ("stopped", 4, 0)
            assert "aborted the file's own transaction" in ran.error.message and ids(db, table) == []
        else:
            assert (ran.outcome.outcome, ran.outcome.statements, ran.outcome.error_count) == ("completed", 5, 1)
            assert ids(db, table) == [1, 2, 4]


@requires_mssql
def test_sql_server_batches_that_end_the_transaction_all_or_nothing(tool_env, tmp_path):  # H3
    db, app, profile = tool_env("mssql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        caught = ("BEGIN TRY\n  INSERT INTO {t} VALUES (2, 'x'); SELECT 1/0\nEND TRY\nBEGIN CATCH\n"
                  "  IF @@TRANCOUNT > 0 ROLLBACK;\n  PRINT 'caught'\nEND CATCH").replace("{t}", db.table(table))
        path = write(tmp_path, db, [*inserts(db, table, 1), caught, *inserts(db, table, 3)], name="catch.sql")
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        # the batch's ROLLBACK would have undone the frame's transaction: never sent, nothing applied
        assert (ran.outcome.outcome, ran.error.code, ran.outcome.stopped_at.line) == (
            "rolled_back", "transaction_in_file", 3)
        assert ids(db, table) == []
        proc = scratch_name("commit")
        made.append(("PROCEDURE", proc))
        db.execute(f"CREATE PROCEDURE {db.table(proc)} AS COMMIT TRANSACTION")
        path = write(tmp_path, db, [*inserts(db, table, 1), f"EXEC {db.table(proc)}", *inserts(db, table, 3)],
                     name="exec.sql")
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        # the procedure committed the frame's transaction (then 266): what ran before it is applied, unknown to us
        assert (ran.outcome.outcome, ran.outcome.stopped_at.statement) == ("unknown", 2)
        assert "The transaction had ended when line 3 failed" in ran.error.message
        assert ids(db, table) == [1]


# --- files other tools wrote (A8) -------------------------------------------------------------------------------


def tool_file(tmp_path: Path, fixture: str, table: str) -> Path:
    path = tmp_path / fixture
    path.write_bytes((FIXTURES / fixture).read_bytes().replace(TOOL_TABLE.encode(), table.encode()))
    return path


@pytest.fixture
def tool_env(tmp_path):
    made_envs = []

    def make(engine: str):
        db = scratch_db(engine, tmp_path)
        app = DataApp(tmp_path)
        made_envs.append((db, app))
        return db, app, app.add_profile(db.profile())

    yield make
    for db, app in made_envs:
        app.close()
        db.close()


@requires_pg
def test_a_pg_dump_inserts_file_runs_and_its_copy_form_stops_at_copy(tool_env, tmp_path):
    db, app, profile = tool_env("postgres")
    with dropping(db, app.close_sessions) as made:
        table = unique_name("zz_06_tool")
        made.append(("TABLE", table))
        path = tool_file(tmp_path, "pg_dump_inserts.sql", table)
        ran = app.job_session(profile).run(command(path, "statements"))
        assert (ran.outcome.outcome, ran.error) == ("completed", None)
        assert [m for m in ran.messages if m.startswith("Skipped line")] == [
            f"Skipped line {line_of(path, '\\restrict')}: \\restrict is a psql command",
            f"Skipped line {line_of(path, '\\unrestrict')}: \\unrestrict is a psql command"]
        assert db.rows(f"SELECT id, label, data, amount::text FROM {db.table(table)} ORDER BY id") == [
            (1, "it's", b"\x00\xff", "12345678901234567890.1234567890"), (2, "two\nlines", None, None),
            (3, "中文😀", b"\xde\xad\xbe\xef", "-1.5000000000")]
        assert db.rows(f"SELECT nextval('public.{table}_id_seq')") == [(4,)]
    with dropping(db, app.close_sessions) as made:
        table = unique_name("zz_06_tool")
        made.append(("TABLE", table))
        path = tool_file(tmp_path, "pg_dump_copy.sql", table)
        assert any("copies data from stdin" in w for w in app.inspect(profile, path).warnings)
        ran = app.job_session(profile).run(command(path, "statements", on_error="continue"))
        assert (ran.outcome.outcome, ran.error.code) == ("stopped", "unsupported_copy")
        assert ran.outcome.stopped_at.line == line_of(path, "COPY public.")
        assert "pg_dump: use --inserts, or run this file with psql" in ran.error.message


@requires_mysql
def test_a_mysqldump_file_runs_statement_by_statement(tool_env, tmp_path):
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = unique_name("zz_06_tool")
        made.append(("TABLE", table))  # its trigger goes with it
        ran = app.job_session(profile).run(command(tool_file(tmp_path, "mysqldump.sql", table), "statements"))
        assert (ran.outcome.outcome, ran.error) == ("completed", None)
        assert db.rows(f"SELECT id, label, HEX(data), CAST(born AS CHAR), note FROM {db.table(table)} ORDER BY id") == [
            (1, "it's \\ back", "00FF", "2026-10-20", None), (2, "中文😀", None, None, "x;y")]
        assert db.rows("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = 'dbaguy' "
                       f"AND EVENT_OBJECT_TABLE = '{table}'") == [(1,)]
        raw = tool_file(tmp_path, "mysqldump_raw_binary.sql", unique_name("zz_06_raw"))
        assert any("not UTF-8" in w and "--hex-blob" in w for w in app.inspect(profile, raw).warnings)
        made.append(("TABLE", raw.read_bytes().split(b"`")[1].decode()))
        stopped = app.job_session(profile).run(command(raw, "statements"))
        assert (stopped.outcome.outcome, stopped.error.code) == ("stopped", "file_not_utf8")
        assert "--hex-blob" in stopped.error.message
        # M3: the statements before the bad line ran (the table exists), and the stop names the INSERT's line
        inserted = next(i for i, line in enumerate(raw.read_bytes().split(b"\n"), start=1) if line.startswith(b"INSERT"))
        assert (stopped.outcome.stopped_at.line, stopped.outcome.statements > 10) == (inserted, True)


def test_a_sqlite3_dump_runs_statement_by_statement_and_stops_all_or_nothing(tool_env, tmp_path):
    db, app, profile = tool_env("sqlite")
    with dropping(db, app.close_sessions) as made:
        table = unique_name("zz_06_tool")
        made.append(("TABLE", table))
        path = tool_file(tmp_path, "sqlite3_dump.sql", table)
        refused = app.job_session(profile).run(command(path, "all_or_nothing"))
        assert (refused.outcome.outcome, refused.error.code) == ("rolled_back", "transaction_in_file")
        assert not db.exists(table)
        ran = app.job_session(profile).run(command(path, "statements"))
        assert (ran.outcome.outcome, ran.error) == ("completed", None)
        assert db.rows(f'SELECT id, label, data, v FROM "{table}" ORDER BY id') == [
            (1, "it's", b"\x00\xff", "007"), (2, "two\nlines", None, 1.5), (3, "中文😀", None, 0)]


@requires_mssql
def test_an_ssms_style_script_runs(tool_env, tmp_path):
    assert b"\r\n" in (FIXTURES / "ssms_script.sql").read_bytes()  # pre-flight L10: SSMS writes CRLF
    db, app, profile = tool_env("mssql")
    with dropping(db, app.close_sessions) as made:
        table = unique_name("zz_06_tool")
        made.append(("TABLE", table))
        ran = app.job_session(profile).run(command(tool_file(tmp_path, "ssms_script.sql", table), "statements"))
        assert (ran.outcome.outcome, ran.error, ran.outcome.statements) == ("completed", None, 10)
        assert db.rows(f"SELECT id, label, data FROM {db.table(table)} ORDER BY id") == [
            (1, "it's", b"\x00\xff"), (2, "中文😀", None)]
        assert db.rows(f"SELECT IDENT_CURRENT('dbo.{table}')")[0][0] == 2


@requires_couchbase
def test_couchbase_runs_a_file_statement_by_statement_only(tmp_path):
    app = DataApp(tmp_path)
    profile = make_couchbase_profile()
    body = {"profile": profile.model_dump(mode="json", exclude={"id"}), "secrets": {"password": COUCHBASE_PASSWORD}}
    profile_id = app.add_profile(body)
    key = unique_name("zz_06_run")
    path = tmp_path / "docs.sqlpp"
    path.write_text(
        f'UPSERT INTO orders (KEY, VALUE) VALUES ("{key}", {{"n": 1}});\n'
        f'UPDATE orders USE KEYS "{key}" SET n = 2;\n'
        f'SELECT RAW n FROM orders USE KEYS "{key}";\n'
        f'DELETE FROM orders USE KEYS "{key}";\n', encoding="utf-8")
    context = QueryContext(bucket="dbaguy", scope="app")
    try:
        tab = app.job_session(profile_id)
        refused = tab.run(command(path, "all_or_nothing", query_context=context))
        assert (refused.outcome.outcome, refused.error.code) == ("not_started", "not_supported")
        ran = tab.run(command(path, "statements", query_context=context))
        assert (ran.outcome.outcome, ran.outcome.statements, ran.error) == ("completed", 4, None)
    finally:
        couchbase_query(f'DELETE FROM dbaguy.app.orders USE KEYS "{key}"')
        app.close()
    assert couchbase_query(f'SELECT RAW META().id FROM dbaguy.app.orders USE KEYS "{key}"')["results"] == []
    # the collection's count follows a delete a moment later
    assert wait_until(lambda: couchbase_query("SELECT RAW COUNT(*) FROM dbaguy.app.orders")["results"] == [500], 10)


# ---- fix round 1 (rulings R1–R9): each outcome against what the server holds ------------------------------------


@requires_mysql
@pytest.mark.parametrize(("statement", "code"), [
    ("START REPLICA", "implicit_commit"), ("STOP SLAVE", "implicit_commit"),
    ("SET PASSWORD FOR 'zz_06_nobody'@'%' = 'x'", "implicit_commit"), ("SET autocommit = 1", "implicit_commit"),
    ("CALL zz_06_no_such_procedure()", "procedure_call"),
])
def test_mysql_all_or_nothing_never_sends_what_may_commit(tool_env, tmp_path, statement, code):  # R1, R7
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, [*inserts(db, table, 1), statement, *inserts(db, table, 3)])
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        assert (ran.outcome.outcome, ran.error.code, ran.outcome.stopped_at.statement) == ("rolled_back", code, 2)
        assert ids(db, table) == []


@requires_mysql
def test_mysql_start_replica_inside_the_files_transaction_committed_it(tool_env, tmp_path):  # R1, statement mode
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, ["START TRANSACTION", *inserts(db, table, 1), "START REPLICA",
                                    *inserts(db, table, 3), "COMMIT"])
        ran = app.job_session(profile).run(command(path, "statements"))  # 1227 after committing the INSERT
        assert (ran.outcome.outcome, ran.error.code, ran.outcome.statements) == ("stopped", "1227", 2)
        assert ran.outcome.warnings == ["Line 3 committed the file's transaction from line 1 before it failed "
                                        "(MySQL commits around DDL)"]
        assert ids(db, table) == [1]


@requires_mysql
def test_a_mysql_deadlock_victim_holds_nothing_and_says_rolled_back(tool_env, tmp_path):  # R1's exception: 1213
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        heavy = scratch_name("heavy")
        made.append(("TABLE", heavy))
        db.execute(*inserts(db, table, 1, 2), f"CREATE TABLE {db.table(heavy)} (id INT PRIMARY KEY)")
        path = write(tmp_path, db, [f"UPDATE {db.table(table)} SET label = 'run' WHERE id = 1",
                                    f"UPDATE {db.table(table)} SET label = 'run' WHERE id = 2"])
        other = mysql.connector.connect(**mysql_driver_kwargs())
        try:
            cursor = other.cursor()
            cursor.execute("START TRANSACTION")  # heavier than the run's: InnoDB rolls the run back as the victim
            cursor.execute(f"INSERT INTO {db.table(heavy)} WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 "
                           "FROM n WHERE i < 200) SELECT i FROM n")
            cursor.execute(f"UPDATE {db.table(table)} SET label = 'other' WHERE id = 2")
            tab = app.job_session(profile)
            rid = tab.submit(command(path, "all_or_nothing"))
            assert wait_until(lambda: db.waiting(table), 20)  # the run's second UPDATE waits for row 2
            cursor.execute(f"UPDATE {db.table(table)} SET label = 'other' WHERE id = 1")  # the deadlock
            ran = tab.wait(rid)
            other.rollback()
        finally:
            other.close()
        assert (ran.outcome.outcome, ran.error.code) == ("rolled_back", "1213")
        assert db.rows(f"SELECT id, label FROM {db.table(table)} ORDER BY id") == [(1, "row 1"), (2, "row 2")]


@requires_mssql
def test_a_trigger_that_commits_is_unknown_and_its_rows_stand(tool_env, tmp_path):  # R2 (3609)
    db, app, profile = tool_env("mssql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        trigger = scratch_name("trg")  # dropped with its table
        db.execute(f"CREATE TRIGGER {db.table(trigger)} ON {db.table(table)} AFTER INSERT AS "
                   "IF EXISTS (SELECT 1 FROM inserted WHERE id = 99) COMMIT TRANSACTION")
        path = write(tmp_path, db, [*inserts(db, table, 1, 99, 3)])
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        assert (ran.outcome.outcome, ran.error.code, ran.outcome.stopped_at.statement) == ("unknown", "3609", 2)
        assert ids(db, table) == [1, 99]
        db.execute(f"DELETE FROM {db.table(table)}")
        path = write(tmp_path, db, ["BEGIN TRANSACTION", *inserts(db, table, 1, 99, 3), "COMMIT TRANSACTION"],
                     name="own.sql")
        ran = app.job_session(profile).run(command(path, "statements"))
        assert (ran.outcome.outcome, ran.outcome.statements) == ("unknown", 2)  # not "not applied"
        assert ran.outcome.warnings[0].startswith("Line 5 may have committed or rolled back the file's transaction")
        assert ids(db, table) == [1, 99]


@requires_mysql
@pytest.mark.parametrize("mode", ["all_or_nothing", "statements"])
def test_a_myisam_write_that_a_rollback_cannot_undo_is_unknown(tool_env, tmp_path, mode):  # R3
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        myisam = scratch_name("myisam")
        made.append(("TABLE", myisam))
        db.execute(*inserts(db, table, 1), f"CREATE TABLE {db.table(myisam)} (id INT PRIMARY KEY, label VARCHAR(50)) "
                                           "ENGINE=MyISAM")
        statements = [*inserts(db, myisam, 5), *inserts(db, table, 1)]  # the second is a duplicate key
        path = write(tmp_path, db, statements if mode == "all_or_nothing" else ["START TRANSACTION", *statements])
        ran = app.job_session(profile).run(command(path, mode))
        assert ran.outcome.outcome == "unknown"
        assert ran.outcome.warnings[-1] == "Some tables cannot roll back (non-transactional): their changes stayed"
        assert ids(db, myisam) == [5] and ids(db, table) == [1]


@requires_pg
def test_a_pg_run_from_statement_n_keeps_the_files_search_path(tool_env, tmp_path):  # R4
    db, app, profile = tool_env("postgres")
    schema = scratch_name("s")
    with dropping(db, app.close_sessions) as made:
        table = scratch_name("rs")
        made.append(("TABLE", table))  # public's
        db.execute(f"CREATE TABLE {db.table(table)} (id INT PRIMARY KEY, label VARCHAR(50))", f'CREATE SCHEMA "{schema}"',
                   f'CREATE TABLE "{schema}"."{table}" (id INT PRIMARY KEY, label VARCHAR(50))')
        try:
            path = write(tmp_path, db, [f'SET search_path TO "{schema}"', *[f"INSERT INTO {table} VALUES ({i}, 'x')"
                                                                           for i in (1, 1, 3)]])
            ran = app.job_session(profile).run(command(path, "statements", from_statement=4))
            assert (ran.outcome.outcome, ran.outcome.statements) == ("completed", 1)
            assert ran.outcome.warnings == ["1 session settings before statement 4 were run again (SET, USE, PRAGMA); "
                                            "the other statements before it were skipped"]
            assert db.rows(f'SELECT id FROM "{schema}"."{table}"') == [(3,)] and ids(db, table) == []
        finally:
            db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@requires_mysql
def test_a_mysql_run_from_statement_n_keeps_the_files_time_zone(tool_env, tmp_path):  # R4
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = scratch_name("tz")
        made.append(("TABLE", table))
        db.execute(f"CREATE TABLE {db.table(table)} (id INT PRIMARY KEY, at TIMESTAMP NULL)")
        path = write(tmp_path, db, ["/*!40103 SET TIME_ZONE='+05:00' */", "DO 0",
                                    f"INSERT INTO {db.table(table)} VALUES (1, '2026-10-20 12:00:00')"])
        ran = app.job_session(profile).run(command(path, "statements", from_statement=3))
        assert (ran.outcome.outcome, ran.outcome.statements) == ("completed", 1)
        [(epoch,)] = db.rows(f"SELECT UNIX_TIMESTAMP(at) FROM {db.table(table)}")
        assert int(epoch) == int(datetime(2026, 10, 20, 7, tzinfo=UTC).timestamp())  # 12:00 at +05:00


@requires_mssql
def test_a_sql_server_run_from_statement_n_keeps_identity_insert(tool_env, tmp_path):  # R4
    db, app, profile = tool_env("mssql")
    with dropping(db, app.close_sessions) as made:
        table = scratch_name("ident")
        made.append(("TABLE", table))
        db.execute(f"CREATE TABLE {db.table(table)} (id INT IDENTITY(1,1) PRIMARY KEY, label NVARCHAR(50))")
        path = write(tmp_path, db, [f"SET IDENTITY_INSERT {db.table(table)} ON",
                                    *[f"INSERT INTO {db.table(table)} (id, label) VALUES ({i}, N'x')" for i in (7, 8)]])
        ran = app.job_session(profile).run(command(path, "statements", from_statement=3))
        assert (ran.outcome.outcome, ran.error, ids(db, table)) == ("completed", None, [8])


def test_a_sqlite_run_from_statement_n_keeps_foreign_keys_off(tool_env, tmp_path):  # R4
    db, app, profile = tool_env("sqlite")
    with dropping(db, app.close_sessions) as made:
        parent, child = scratch_name("par"), scratch_name("chi")
        made += [("TABLE", parent), ("TABLE", child)]
        db.execute(f'CREATE TABLE "{parent}" (id INTEGER PRIMARY KEY)',
                   f'CREATE TABLE "{child}" (id INTEGER PRIMARY KEY, p INTEGER REFERENCES "{parent}")')
        path = write(tmp_path, db, ["PRAGMA foreign_keys=OFF", "SELECT 1", f'INSERT INTO "{child}" VALUES (1, 99)'])
        ran = app.job_session(profile).run(command(path, "statements", from_statement=3))
        assert (ran.outcome.outcome, ran.error) == ("completed", None)  # the adapter turns them on at connect
        assert db.rows(f'SELECT id, p FROM "{child}"') == [(1, 99)]


@pytest.mark.parametrize("engine", [pytest.param("postgres", marks=requires_pg),
                                    pytest.param("mysql", marks=requires_mysql)])
def test_a_commit_that_chains_restarts_the_files_transaction_there(tool_env, tmp_path, engine):  # R5
    db, app, profile = tool_env(engine)
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        begin = ["SET SESSION completion_type = CHAIN", "START TRANSACTION"] if engine == "mysql" else ["BEGIN"]
        chain = "COMMIT" if engine == "mysql" else "COMMIT AND CHAIN"
        path = write(tmp_path, db, [*begin, *inserts(db, table, 1), chain, *inserts(db, table, 2, 1)])
        ran = app.job_session(profile).run(command(path, "statements"))
        commit_at = len(begin) + 2
        assert (ran.outcome.outcome, ran.outcome.statements) == ("stopped", commit_at)
        # the chained transaction is tracked from its first statement, after the COMMIT (06b review M1)
        assert ran.outcome.warnings[0].startswith(f"The file's transaction from line {commit_at + 1} (statement "
                                                  f"{commit_at + 1}) was still open")
        assert ran.outcome.resume_at.statement == commit_at + 1
        assert ids(db, table) == [1]


@pytest.mark.parametrize("engine", [pytest.param("postgres", marks=requires_pg),
                                    pytest.param("mysql", marks=requires_mysql)])
def test_a_run_again_after_commit_and_chain_starts_after_it_and_lands_once(tool_env, tmp_path, engine):  # review M1
    db, app, profile = tool_env(engine)
    with dropping(db, app.close_sessions) as made:
        table, gate = make_table(db, made), make_table(db, made)
        db.execute(*inserts(db, gate, 1))
        begin = "START TRANSACTION" if engine == "mysql" else "BEGIN"
        path = write(tmp_path, db, [begin, *inserts(db, table, 1), "COMMIT AND CHAIN", *inserts(db, table, 2),
                                    *inserts(db, gate, 1), *inserts(db, table, 3), "COMMIT"])
        first = app.job_session(profile).run(command(path, "statements")).outcome
        assert (first.outcome, first.resume_at.line, first.resume_at.statement, ids(db, table)) == (
            "stopped", 4, 4, [1])
        db.execute(f"DELETE FROM {db.table(gate)}")
        again = app.job_session(profile).run(command(path, "statements", from_statement=first.resume_at.statement,
                                                     from_line=first.resume_at.line)).outcome
        assert (again.outcome, ids(db, table)) == ("completed", [1, 2, 3])  # PostgreSQL refused a COMMIT AND CHAIN


@requires_pg
def test_a_do_block_that_commits_then_fails_says_part_may_be_applied(tool_env, tmp_path):  # R6
    db, app, profile = tool_env("postgres")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        q = db.table(table)
        block = (f"DO $$ BEGIN INSERT INTO {q} VALUES (1, 'a'); INSERT INTO {q} VALUES (2, 'b'); COMMIT; "
                 f"INSERT INTO {q} VALUES (1, 'dup'); END $$")
        ran = app.job_session(profile).run(command(write(tmp_path, db, [block]), "statements"))
        assert ran.outcome.outcome == "stopped" and ran.outcome.warnings[0].startswith("Line 1 runs several statements")
        assert ids(db, table) == [1, 2]


RESTARTED = ("The transaction was ended and restarted inside the file (a trigger or a procedure): some changes may "
             "stand")


@requires_mssql
def test_a_trigger_that_commits_and_begins_again_is_unknown_at_a_later_failure(tool_env, tmp_path):  # R10
    """COMMIT; BEGIN TRANSACTION inside a trigger keeps @@TRANCOUNT and raises no 3609: only the transaction's id,
    read after the run's BEGIN and again at the failure, tells."""
    db, app, profile = tool_env("mssql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        trigger = scratch_name("trg")  # dropped with its table
        db.execute(f"CREATE TRIGGER {db.table(trigger)} ON {db.table(table)} AFTER INSERT AS IF EXISTS "
                   "(SELECT 1 FROM inserted WHERE id = 99) BEGIN COMMIT TRANSACTION; BEGIN TRANSACTION; END")
        path = write(tmp_path, db, inserts(db, table, 1, 99, 1))  # the third is a duplicate key
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        assert (ran.outcome.outcome, ran.error.code, ran.outcome.stopped_at.statement) == ("unknown", "2627", 3)
        assert ran.outcome.warnings == [RESTARTED]
        assert ids(db, table) == [1, 99]
        db.execute(f"DELETE FROM {db.table(table)}")
        path = write(tmp_path, db, ["BEGIN TRANSACTION", *inserts(db, table, 1, 99, 1), "COMMIT TRANSACTION"],
                     name="own.sql")
        ran = app.job_session(profile).run(command(path, "statements"))  # inside the file's own BEGIN TRAN
        assert (ran.outcome.outcome, ran.outcome.stopped_at.statement, ran.outcome.statements) == ("unknown", 4, 3)
        assert ran.outcome.warnings == ["The file's transaction from line 1 (statement 1) was still open: it was "
                                        "rolled back", RESTARTED]
        assert ids(db, table) == [1, 99]


@requires_mssql
def test_a_procedure_that_commits_and_begins_again_is_unknown(tool_env, tmp_path):  # R7 → R10, SQL Server
    db, app, profile = tool_env("mssql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        proc = scratch_name("reopen")
        made.append(("PROCEDURE", proc))
        db.execute(f"CREATE PROCEDURE {db.table(proc)} AS BEGIN COMMIT TRANSACTION; BEGIN TRANSACTION; END")
        path = write(tmp_path, db, [*inserts(db, table, 1), f"EXEC {db.table(proc)}", *inserts(db, table, 1)])
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        # it used to say rolled_back here: the procedure's COMMIT stood
        assert (ran.outcome.outcome, ran.error.code, ran.outcome.stopped_at.statement) == ("unknown", "2627", 3)
        assert ran.outcome.warnings == [RESTARTED]
        assert ids(db, table) == [1]
        db.execute(f"DELETE FROM {db.table(table)}")
        path = write(tmp_path, db, [*inserts(db, table, 1), f"EXEC {db.table(proc)}", *inserts(db, table, 2)],
                     name="whole.sql")
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))  # nothing failed: all of it committed,
        # but another transaction than the run's is the one its COMMIT ends: unknown (N4, either way it ended)
        assert (ran.outcome.outcome, ran.outcome.warnings, ids(db, table)) == ("unknown", [RESTARTED], [1, 2])


@requires_mssql
def test_a_sql_server_failure_without_a_restart_stays_rolled_back(tool_env, tmp_path):  # R10: same id, no change
    db, app, profile = tool_env("mssql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, ["EXEC sp_executesql N'SELECT 1'", *inserts(db, table, 1, 1)])
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        assert (ran.outcome.outcome, ran.outcome.warnings, ids(db, table)) == ("rolled_back", [], [])


@requires_mysql
def test_a_stopped_mysql_run_leaves_no_table_locked(tool_env, tmp_path):  # R9
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, [f"LOCK TABLES {db.table(table)} WRITE", *inserts(db, table, 1, 1)])
        tab = app.job_session(profile)  # kept open: its locks would last as long as it
        ran = tab.run(command(path, "statements"))
        assert (ran.outcome.outcome, ran.outcome.statements) == ("stopped", 2)
        other = mysql.connector.connect(**mysql_driver_kwargs(autocommit=True))
        try:
            cursor = other.cursor()
            cursor.execute("SET SESSION lock_wait_timeout = 3")
            cursor.execute(f"INSERT INTO {db.table(table)} VALUES (5, 'other')")  # 1205 while the lock is held
        finally:
            other.close()
        assert ids(db, table) == [1, 5]


# ---- fix round 3 (rulings N1–N6), from the re-review's probes ---------------------------------------------------


@requires_mysql
def test_a_mysql_procedure_that_commits_inside_the_files_transaction_is_unknown(tool_env, tmp_path):  # N1
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        proc = scratch_name("reopen")
        made.append(("PROCEDURE", proc))
        db.execute(f"CREATE PROCEDURE {db.table(proc)}() BEGIN COMMIT; START TRANSACTION; END")
        path = write(tmp_path, db, ["START TRANSACTION", *inserts(db, table, 1), f"CALL {db.table(proc)}()",
                                    *inserts(db, table, 2, 1), "COMMIT"])
        ran = app.job_session(profile).run(command(path, "statements"))
        assert (ran.outcome.outcome, ran.outcome.statements, ran.outcome.stopped_at.statement) == ("unknown", 4, 5)
        assert ran.outcome.warnings == ["Line 3 may have committed or rolled back the file's transaction from line 1 "
                                        "(statement 1): the statements from there on may or may not be applied"]
        assert ids(db, table) == [1]


BEGAN_INSIDE = "Line 1 runs several statements and began the transaction inside them: what it did before that may be applied"


@requires_mysql
def test_a_mysql_procedure_that_works_before_its_start_transaction_stays_counted(tool_env, tmp_path):  # N2
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        proc = scratch_name("half")
        made.append(("PROCEDURE", proc))
        db.execute(f"CREATE PROCEDURE {db.table(proc)}() BEGIN INSERT INTO {db.table(table)} VALUES (1, 'p'); "
                   f"START TRANSACTION; INSERT INTO {db.table(table)} VALUES (2, 'p'); END")
        path = write(tmp_path, db, [f"CALL {db.table(proc)}()", *inserts(db, table, 3, 3)])
        ran = app.job_session(profile).run(command(path, "statements"))
        assert (ran.outcome.outcome, ran.outcome.statements, ran.outcome.warnings[-1]) == ("stopped", 1, BEGAN_INSIDE)
        assert ids(db, table) == [1]


@requires_mssql
@pytest.mark.parametrize("batch", ["INSERT INTO {t} VALUES (1, 'a')\nBEGIN TRANSACTION\nINSERT INTO {t} VALUES (2, 'b')",
                                   "BEGIN TRANSACTION\nINSERT INTO {t} VALUES (1, 'a')\nCOMMIT TRANSACTION\n"
                                   "BEGIN TRANSACTION\nINSERT INTO {t} VALUES (2, 'b')"])
def test_a_tsql_batch_that_works_before_its_begin_tran_stays_counted(tool_env, tmp_path, batch):  # N2
    db, app, profile = tool_env("mssql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, [batch.replace("{t}", db.table(table)), *inserts(db, table, 2)])
        ran = app.job_session(profile).run(command(path, "statements"))
        assert (ran.outcome.outcome, ran.outcome.statements, ran.outcome.warnings[-1]) == ("stopped", 1, BEGAN_INSIDE)
        assert ids(db, table) == [1]


@requires_mysql
def test_a_mysql_set_that_calls_a_function_is_not_sent_again(tool_env, tmp_path):  # N3
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        log, function = scratch_name("log"), scratch_name("f")
        made += [("TABLE", log), ("FUNCTION", function)]
        db.execute(f"CREATE TABLE {db.table(log)} (id INT AUTO_INCREMENT PRIMARY KEY, at VARCHAR(20))",
                   f"CREATE FUNCTION {db.table(function)}() RETURNS INT MODIFIES SQL DATA BEGIN "
                   f"INSERT INTO {db.table(log)} (at) VALUES ('called'); RETURN 1; END")
        path = write(tmp_path, db, [f"SET @x = (SELECT {db.table(function)}())", f"SET @y = {db.table(function)}()",
                                    "/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE */", *inserts(db, table, 1, 2)])
        ran = app.job_session(profile).run(command(path, "statements", from_statement=5))
        assert (ran.outcome.outcome, ran.outcome.statements) == ("completed", 1)
        assert ran.outcome.warnings == ["1 session settings before statement 5 were run again (SET, USE, PRAGMA); "
                                        "the other statements before it were skipped"]  # only mysqldump's line
        assert ids(db, table) == [2] and db.rows(f"SELECT COUNT(*) FROM {db.table(log)}") == [(0,)]


@requires_mssql
def test_a_trigger_that_rolls_back_and_begins_again_is_unknown_though_the_run_completes(tool_env, tmp_path):  # N4
    db, app, profile = tool_env("mssql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        trigger = scratch_name("trg")  # dropped with its table
        db.execute(f"CREATE TRIGGER {db.table(trigger)} ON {db.table(table)} AFTER INSERT AS IF EXISTS "
                   "(SELECT 1 FROM inserted WHERE id = 99) BEGIN ROLLBACK TRANSACTION; BEGIN TRANSACTION; END")
        path = write(tmp_path, db, inserts(db, table, 1, 99, 1))  # the third INSERT 1 runs after the rollback
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        assert (ran.outcome.outcome, ran.outcome.warnings) == ("unknown", [RESTARTED])  # it used to say completed
        assert ids(db, table) == [1]


@requires_mysql
def test_a_mysql_deadlock_victim_that_wrote_myisam_is_unknown(tool_env, tmp_path):  # N5
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        heavy, myisam = scratch_name("heavy"), scratch_name("myisam")
        made += [("TABLE", heavy), ("TABLE", myisam)]
        db.execute(*inserts(db, table, 1, 2), f"CREATE TABLE {db.table(heavy)} (id INT PRIMARY KEY)",
                   f"CREATE TABLE {db.table(myisam)} (id INT PRIMARY KEY, label VARCHAR(50)) ENGINE=MyISAM")
        path = write(tmp_path, db, [*inserts(db, myisam, 5), f"UPDATE {db.table(table)} SET label = 'run' WHERE id = 1",
                                    f"UPDATE {db.table(table)} SET label = 'run' WHERE id = 2"])
        other = mysql.connector.connect(**mysql_driver_kwargs())
        try:
            cursor = other.cursor()
            cursor.execute("START TRANSACTION")  # heavier than the run's: InnoDB rolls the run back as the victim
            cursor.execute(f"INSERT INTO {db.table(heavy)} WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 "
                           "FROM n WHERE i < 200) SELECT i FROM n")
            # InnoDB spares a transaction that wrote a non-transactional table: this one writes MyISAM too
            cursor.execute(f"INSERT INTO {db.table(myisam)} VALUES (900, 'other')")
            cursor.execute(f"UPDATE {db.table(table)} SET label = 'other' WHERE id = 2")
            tab = app.job_session(profile)
            rid = tab.submit(command(path, "all_or_nothing"))
            assert wait_until(lambda: db.waiting(table), 20)
            cursor.execute(f"UPDATE {db.table(table)} SET label = 'other' WHERE id = 1")  # the deadlock
            ran = tab.wait(rid)
            other.rollback()
        finally:
            other.close()
        assert (ran.outcome.outcome, ran.error.code) == ("unknown", "1213")
        assert ran.outcome.warnings == ["Some tables cannot roll back (non-transactional): their changes stayed"]
        assert ids(db, myisam) == [5, 900]
        assert db.rows(f"SELECT id, label FROM {db.table(table)} ORDER BY id") == [(1, "row 1"), (2, "row 2")]


@requires_mssql
def test_a_rollback_and_begin_inside_the_file_says_ended_not_committed(tool_env, tmp_path):  # N6
    db, app, profile = tool_env("mssql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        proc = scratch_name("rb")
        made.append(("PROCEDURE", proc))
        db.execute(f"CREATE PROCEDURE {db.table(proc)} AS BEGIN ROLLBACK TRANSACTION; BEGIN TRANSACTION; END")
        path = write(tmp_path, db, [*inserts(db, table, 1), f"EXEC {db.table(proc)}", *inserts(db, table, 2, 2)])
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        assert (ran.outcome.outcome, ran.outcome.warnings) == ("unknown", [RESTARTED])
        assert ids(db, table) == []
        path = write(tmp_path, db, ["BEGIN TRANSACTION", *inserts(db, table, 1),
                                    "ROLLBACK TRANSACTION\nBEGIN TRANSACTION", *inserts(db, table, 2, 2),
                                    "COMMIT TRANSACTION"], name="own.sql")
        ran = app.job_session(profile).run(command(path, "statements"))
        assert (ran.outcome.outcome, ran.outcome.warnings[-1]) == ("unknown", RESTARTED)
        assert ids(db, table) == []


# ---- 06b Task 2, fix round 1: where a run again starts, on the servers that decide it -------------------------------


@requires_mssql
def test_a_batch_skipped_after_xact_abort_rolled_the_files_transaction_back_has_no_resume_point(tool_env,
                                                                                               tmp_path):  # review H1
    db, app, profile = tool_env("mssql")
    with dropping(db, app.close_sessions) as made:
        table, gate, gate2 = make_table(db, made), make_table(db, made), make_table(db, made)
        db.execute(*inserts(db, gate, 1), *inserts(db, gate2, 1))
        batch = "\n".join([*inserts(db, table, 2, 3), *inserts(db, gate2, 1)])  # two INSERTs stand, the third fails
        path = write(tmp_path, db, ["SET XACT_ABORT ON", "BEGIN TRANSACTION", *inserts(db, table, 1),
                                    *inserts(db, gate, 1), "COMMIT TRANSACTION", batch])
        ran = app.job_session(profile).run(command(path, "statements", on_error="continue")).outcome
        # a run again from the BEGIN would add 2 and 3 twice
        assert (ran.outcome, ran.error_count, ran.resume_at, ids(db, table)) == ("completed", 3, None, [2, 3])


@requires_mysql
def test_a_mysql_call_that_commits_begins_again_and_fails_is_unknown(tool_env, tmp_path):  # review H2, N1
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table, gate = make_table(db, made), make_table(db, made)
        db.execute(*inserts(db, gate, 1))
        proc = scratch_name("again")
        made.append(("PROCEDURE", proc))
        db.execute(f"CREATE PROCEDURE {db.table(proc)}() BEGIN {inserts(db, table, 100)[0]}; COMMIT; "
                   f"START TRANSACTION; {inserts(db, table, 101)[0]}; {inserts(db, gate, 1)[0]}; END")
        path = write(tmp_path, db, [*inserts(db, table, 1), "START TRANSACTION", *inserts(db, table, 2),
                                    f"CALL {db.table(proc)}()", *inserts(db, table, 3), "COMMIT"])
        ran = app.job_session(profile).run(command(path, "statements")).outcome
        assert (ran.outcome, ran.stopped_at.statement, ran.resume_at) == ("unknown", 4, None)
        assert ran.warnings == ["Line 4 may have committed or rolled back the file's transaction from line 2 "
                                "(statement 2): the statements from there on may or may not be applied"]
        assert ids(db, table) == [1, 2, 100]  # the CALL's COMMIT kept line 3's row: a run again from 2 would repeat it


@requires_mysql
def test_a_mysql_write_that_fails_midway_on_myisam_has_no_resume_point(tool_env, tmp_path):  # review M2
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        myisam = scratch_name("myisam")
        made.append(("TABLE", myisam))
        db.execute(f"CREATE TABLE {db.table(myisam)} (id INT, k INT UNIQUE) ENGINE=MyISAM",
                   f"INSERT INTO {db.table(myisam)} VALUES (0, 99)")
        path = write(tmp_path, db, [f"INSERT INTO {db.table(myisam)} VALUES (1, NULL), (2, 99), (3, NULL)"])
        ran = app.job_session(profile).run(command(path, "statements")).outcome
        assert (ran.outcome, ran.resume_at, ran.warnings) == ("stopped", None, [PARTLY_APPLIED.format(1)])
        assert ids(db, myisam) == [0, 1]  # the row before the error stayed: a run again would write it twice
        innodb = make_table(db, made)  # the database still has the MyISAM table: any failed write there says so
        db.execute(*inserts(db, innodb, 1))
        path = write(tmp_path, db, inserts(db, innodb, 2, 1), name="innodb.sql")
        ran = app.job_session(profile).run(command(path, "statements")).outcome
        assert (ran.outcome, ran.resume_at) == ("stopped", None)
    with dropping(db, app.close_sessions) as made:  # without it: the place to run again from
        innodb = make_table(db, made)
        db.execute(*inserts(db, innodb, 1))
        path = write(tmp_path, db, inserts(db, innodb, 2, 1), name="clean.sql")
        ran = app.job_session(profile).run(command(path, "statements")).outcome
        assert (ran.outcome, ran.resume_at.statement, ran.warnings) == ("stopped", 2, [])


@requires_pg
def test_a_run_again_from_a_line_that_moved_is_refused(tool_env, tmp_path):  # review M3
    db, app, profile = tool_env("postgres")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, inserts(db, table, 1, 2, 3))
        ran = app.job_session(profile).run(command(path, "statements", from_statement=2, from_line=1))
        assert (ran.outcome.outcome, ran.error.code, ids(db, table)) == ("not_started", "file_changed", [])
        assert ran.error.message == "The file changed: statement 2 now starts at line 2"
