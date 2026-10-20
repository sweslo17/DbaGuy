"""Run SQL file on the dev databases (spec ⑥ §3.4, R-06-7, §6): both modes, the outcomes a server can force (an
error, a cancel, MySQL's DDL commit, the file's own COMMIT), a read-only connection, the inspection, and files other
tools wrote (A8: pg_dump --inserts and its COPY form, mysqldump with and without --hex-blob, sqlite3 .dump, an SSMS
script). Scratch objects are `zz_06_*`, dropped by exact name; Couchbase writes only `zz_06_run_*` keys."""

import time
from pathlib import Path

import pytest

from dbaguy_backend.domain.errors import ReadOnlyProfile
from dbaguy_backend.domain.execution import QueryContext, RunFileCommand
from dbaguy_backend.sql.dialects import Dialect, script_text
from tests.data_support import DataApp, dropping, scratch_name
from tests.edit_support import ENGINES, ScratchDb, scratch_db
from tests.support import COUCHBASE_PASSWORD, couchbase_query, make_couchbase_profile, requires_couchbase, \
    requires_mssql, requires_mysql, requires_pg, unique_name, wait_until

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
        assert (ran.outcome.outcome, ran.outcome.cancelled, ran.outcome.stopped_at.statement) == (expected, True, 2)
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


@requires_mysql
def test_a_cancelled_mysql_ddl_inside_the_files_transaction_had_committed_it(tool_env, tmp_path):  # H2 × M8
    """A cancel is not a server error: the adapter does not read MySQL's flags again (R-03a-T9b), and they still say
    the transaction is open. MySQL committed it before the CREATE began."""
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        other = scratch_name("ctas")
        made.append(("TABLE", other))
        path = write(tmp_path, db, ["START TRANSACTION", *inserts(db, table, 1),
                                    f"CREATE TABLE {db.table(other)} AS SELECT SLEEP(30) AS s", "COMMIT"])
        tab = app.job_session(profile)
        rid = tab.submit(command(path, "statements"))
        running = ("SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE ID <> CONNECTION_ID() "
                   f"AND INFO LIKE 'CREATE TABLE %{other}%'")
        assert wait_until(lambda: db.rows(running) == [(1,)], 20)  # the CREATE … SELECT SLEEP has started
        tab.worker.cancel(rid)
        ran = tab.wait(rid)
        assert (ran.outcome.outcome, ran.outcome.cancelled, ran.outcome.statements) == ("stopped", True, 2)
        assert ran.outcome.warnings == ["Line 3 committed the file's transaction from line 1 before it failed "
                                        "(MySQL commits around DDL)"]
        assert ids(db, table) == [1]


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
