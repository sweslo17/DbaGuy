"""Dump → Run SQL file, on the dev databases (spec ⑥ §1 success criteria, A7): each engine's seed (`dbaguy`, and
`analytics`) dumped through the job with `drop_first` and run twice into a scratch database `zz_06_restore_*` through
the run job (SQLite: files in tmp_path), then compared through the independent inventories (tests/pg_inventory.py,
mysql_inventory.py, mssql_inventory.py, sqlite_inventory.py: the catalogs as the administrator reads them, every
table's rows by count and hash, every counter's next value) — never through the dump's own reader. The seeds are only
read. Also: a table with no column a dump writes, a filtered one-table dump, the plan and Show in Finder routes, and
the adapters reading no rows ahead of a job's fetch (pre-flight M12)."""

import sqlite3
from pathlib import Path

import pytest

from dbaguy_backend.adapters.base import RunOptions
from dbaguy_backend.domain.data_jobs import DumpObjectKinds, DumpScope
from dbaguy_backend.domain.execution import DumpCommand, RunFileCommand
from dbaguy_backend.sessions.jobs.context import NO_DESCRIBE
from dbaguy_backend.sql.dump_header import comment_safe, parse_header
from tests import mssql_inventory, mysql_inventory, pg_inventory, sqlite_inventory
from tests.data_support import DataApp, Ran, scratch_database, scratch_name
from tests.edit_support import scratch_db
from tests.support import requires_mssql, requires_mysql, requires_pg


def dump(app: DataApp, profile, database: str | None, path: Path, scope: DumpScope, **fields) -> Ran:
    tab = app.job_session(profile, database)
    return tab.run(lambda rid: DumpCommand(request_id=rid, path=str(path), scope=scope, **fields), timeout=300)


def restore(app: DataApp, profile, database: str | None, path: Path, mode: str) -> Ran:
    tab = app.job_session(profile, database)
    return tab.run(lambda rid: RunFileCommand(request_id=rid, path=str(path), mode=mode), timeout=300)


def restored_twice(app: DataApp, profile, database: str | None, path: Path, mode: str) -> list[Ran]:
    """The file run twice into the same database (a `drop_first` file replaces what the first run made, H7), each
    asserted complete without a skipped error."""
    runs = []
    for _ in range(2):
        ran = restore(app, profile, database, path, mode)
        assert (ran.outcome.outcome, ran.outcome.error_count, ran.error) == ("completed", 0, None), (
            ran.outcome.errors, ran.error, ran.outcome.stopped_at)
        runs.append(ran)
        app.close_sessions()
    return runs


def sections(path: Path) -> set[tuple[str, str]]:
    """The `-- kind: name` comment the file writes before each object."""
    found = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("-- ") and ": " in line and not line.startswith(("-- Data: ", "-- DbaGuy")):
            kind, _, name = line[3:].partition(": ")
            found.add((kind, name))
    return found


def plan_matches_the_file(app: DataApp, profile, database: str | None, scope: DumpScope, path: Path) -> None:
    """The plan's objects are objects the dump wrote, and its Not included list is the header's (DumpPlan)."""
    plan = app.container.data.dump_plan(profile, database, scope, "both", DumpObjectKinds(), None)
    written = sections(path)
    assert plan.objects and {(o.kind, o.name) for o in plan.objects} <= written
    header = parse_header(path.read_text(encoding="utf-8"))
    assert comment_safe("-- Not included: " + " · ".join(plan.not_included)) in header.lines


@requires_pg
@pytest.mark.parametrize("seed", ["dbaguy", "analytics"])
def test_postgresqls_seed_dumps_and_restores_twice_all_or_nothing(tmp_path, seed):
    db = scratch_db("postgres", tmp_path)
    app = DataApp(tmp_path)
    try:
        profile = app.add_profile(db.profile(database=seed))
        path = tmp_path / "pg.sql"
        scope = DumpScope(kind="database")
        dumped = dump(app, profile, seed, path, scope, drop_first=True)
        assert (dumped.outcome.outcome, dumped.error) == ("completed", None)
        assert parse_header(path.read_text()).scope == f"database {seed}"
        plan_matches_the_file(app, profile, seed, scope, path)
        with scratch_database("postgres", "restore") as (target, admin):
            restored = app.add_profile(db.profile(database=target))
            runs = restored_twice(app, restored, target, path, "all_or_nothing")
            # every INSERT's rows, and a materialized view's: CREATE … AS SELECT computes them (SELECT n)
            computed = sum(count for (count,) in admin.rows(seed, (
                "SELECT (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', schemaname, "
                "matviewname), false, true, '')))[1]::text::bigint FROM pg_matviews WHERE ispopulated")))
            assert [ran.outcome.rows for ran in runs] == [dumped.outcome.rows + computed] * 2
            assert pg_inventory.differences(pg_inventory.inventory(seed), pg_inventory.inventory(target)) == {}
    finally:
        app.close()
        db.close()


@requires_mysql
@pytest.mark.parametrize("seed", ["dbaguy", "analytics"])
def test_mysqls_seed_dumps_and_restores_twice_statement_by_statement(tmp_path, seed):
    """MySQL commits around DDL, so its dump restores statement by statement (S6); all or nothing refuses it before
    sending the first DROP or CREATE, after the header's four settings (H1)."""
    db = scratch_db("mysql", tmp_path)
    app = DataApp(tmp_path)
    try:
        profile = app.add_profile(db.profile(database=seed))
        path = tmp_path / "my.sql"
        scope = DumpScope(kind="schema", schema_name=seed)
        dumped = dump(app, profile, None, path, scope, drop_first=True)
        assert (dumped.outcome.outcome, dumped.error) == ("completed", None)
        plan_matches_the_file(app, profile, None, scope, path)
        with scratch_database("mysql", "restore") as (target, _):
            restored = app.add_profile(db.profile(database=target))
            runs = restored_twice(app, restored, target, path, "statements")
            assert [ran.outcome.rows for ran in runs] == [dumped.outcome.rows] * 2
            source, back = mysql_inventory.inventory(seed), mysql_inventory.inventory(target)
            assert mysql_inventory.differences(source, back) == []
            ran = restore(app, restored, target, path, "all_or_nothing")
            assert (ran.outcome.outcome, ran.error.code, ran.outcome.stopped_at.statement) == (
                "rolled_back", "implicit_commit", 5)
            assert mysql_inventory.differences(source, mysql_inventory.inventory(target)) == []
    finally:
        app.close()
        db.close()


@requires_mssql
@pytest.mark.parametrize("seed", ["dbaguy", "analytics"])
def test_sql_servers_seed_dumps_and_restores_twice_all_or_nothing(tmp_path, seed):
    db = scratch_db("mssql", tmp_path)
    app = DataApp(tmp_path)
    try:
        profile = app.add_profile(db.profile(database=seed))
        path = tmp_path / "ms.sql"
        scope = DumpScope(kind="database")
        dumped = dump(app, profile, seed, path, scope, drop_first=True)
        assert (dumped.outcome.outcome, dumped.error) == ("completed", None)
        text = path.read_text(encoding="utf-8")
        assert "-- Snapshot: not one consistent snapshot" in text  # A S9: the dev databases do not allow SNAPSHOT
        if seed == "dbaguy":
            assert "the values of sql_variant column dbo.types_demo.anything" in text
        plan_matches_the_file(app, profile, seed, scope, path)
        with scratch_database("mssql", "restore") as (target, _):
            restored = app.add_profile(db.profile(database=target))
            runs = restored_twice(app, restored, target, path, "all_or_nothing")
            # every INSERT reports its rows: no SET NOCOUNT ON (S21)
            assert [ran.outcome.rows for ran in runs] == [dumped.outcome.rows] * 2
            assert mssql_inventory.differences(mssql_inventory.inventory(seed), mssql_inventory.inventory(target)) == {}
            assert mssql_inventory.data(seed) == mssql_inventory.data(target)
            if seed == "dbaguy":  # what the header says it leaves out
                assert mssql_inventory.sa_rows(target, "SELECT COUNT(*) FROM dbo.types_demo WHERE anything IS NOT "
                                                       "NULL") == [(0,)]
    finally:
        app.close()
        db.close()


SQLITE_SHAPES = """
    CREATE TABLE parent (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, loose, ratio REAL, data BLOB);
    CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent (id),
                        twice INTEGER GENERATED ALWAYS AS (parent_id * 2) VIRTUAL);
    CREATE TABLE strict_t (k TEXT PRIMARY KEY, n INTEGER, anything ANY) STRICT, WITHOUT ROWID;
    CREATE TABLE "Odd ""Name"" t" ("a b" TEXT, "中文" INTEGER);
    CREATE INDEX child_parent ON child (parent_id);
    CREATE VIEW labels AS SELECT label FROM parent;
    CREATE TRIGGER child_ai AFTER INSERT ON child BEGIN UPDATE parent SET label = label WHERE id = new.parent_id; END;
    INSERT INTO parent (label, loose, ratio, data) VALUES ('it''s', '007', 9e999, X'00ff'), ('中文😀', 7, -0.0, NULL),
                                                         ('gone', 1.10, 1.5, NULL), ('', NULL, -9e999, X'');
    DELETE FROM parent WHERE label = 'gone';
    INSERT INTO child (id, parent_id) VALUES (1, 1), (2, 2), (3, NULL);
    INSERT INTO strict_t VALUES ('a', 9223372036854775807, 'text'), ('b', -9223372036854775808, 1.5),
                                ('c', NULL, X'01'), ('d', 0, 42);
    INSERT INTO "Odd ""Name"" t" VALUES ('x
y', 1), (NULL, NULL);
    PRAGMA user_version = 7;
"""


def test_sqlite_dumps_and_restores_types_counters_and_objects_twice(tmp_path):
    source = tmp_path / "source.db"
    conn = sqlite3.connect(source, isolation_level=None)
    conn.executescript(SQLITE_SHAPES)
    conn.close()
    app = DataApp(tmp_path)
    try:
        profile = app.add_profile({"profile": {"engine": "sqlite", "name": "source", "path": str(source)}})
        path = tmp_path / "lite.sql"
        scope = DumpScope(kind="schema", schema_name="main")
        dumped = dump(app, profile, None, path, scope, drop_first=True)
        assert (dumped.outcome.outcome, dumped.error, dumped.outcome.rows) == ("completed", None, 12)
        plan_matches_the_file(app, profile, None, scope, path)
        target = tmp_path / "target.db"
        sqlite3.connect(target).close()
        restored = app.add_profile({"profile": {"engine": "sqlite", "name": "target", "path": str(target)}})
        restored_twice(app, restored, None, path, "all_or_nothing")
        assert sqlite_inventory.differences(sqlite_inventory.inventory(source), sqlite_inventory.inventory(target)) \
            == []
    finally:
        app.close()


def _bare_tables(engine: str, admin, database: str) -> tuple[str, str]:
    """A table of no column a dump writes, with three rows, in `database`: (its name, the COUNT on it)."""
    name = scratch_name("bare")
    statements = {
        "postgres": [f'CREATE TABLE public."{name}" ()', f'INSERT INTO public."{name}" DEFAULT VALUES',
                     f'INSERT INTO public."{name}" SELECT FROM generate_series(1, 2)'],
        "mysql": [f"CREATE TABLE `{name}` (g INT AS (1), s INT AS (2) STORED)",
                  f"INSERT INTO `{name}` () VALUES (), (), ()"],
        "mssql": [f"CREATE TABLE dbo.[{name}] (stamp rowversion, c AS (1))",
                  f"INSERT INTO dbo.[{name}] DEFAULT VALUES; INSERT INTO dbo.[{name}] DEFAULT VALUES; "
                  f"INSERT INTO dbo.[{name}] DEFAULT VALUES"],
    }[engine]
    admin.run_in(database, *statements)
    quoted = {"postgres": f'public."{name}"', "mysql": f"`{name}`", "mssql": f"dbo.[{name}]"}[engine]
    return name, f"SELECT COUNT(*) FROM {quoted}"


@pytest.mark.parametrize("engine", [
    pytest.param("postgres", marks=requires_pg), pytest.param("mysql", marks=requires_mysql),
    pytest.param("mssql", marks=requires_mssql),
])
def test_a_table_with_no_column_a_dump_writes_keeps_its_rows(tmp_path, engine):  # 06a Task 7 review
    """A PostgreSQL table of no columns, a MySQL one of generated columns only, a SQL Server one of a rowversion and a
    computed column: restored as many rows of their defaults (both modes MySQL can run: statement by statement)."""
    db = scratch_db(engine, tmp_path)
    app = DataApp(tmp_path)
    try:
        with scratch_database(engine, "dumpcat") as (source, admin), \
                scratch_database(engine, "restore") as (target, _):
            name, count = _bare_tables(engine, admin, source)
            schema = {"postgres": "public", "mysql": source, "mssql": "dbo"}[engine]
            scope = DumpScope(kind="tables", schema_name=schema, names=[name])
            profile = app.add_profile(db.profile(database=source))
            path = tmp_path / "bare.sql"
            dumped = dump(app, profile, source, path, scope)
            assert (dumped.outcome.outcome, dumped.outcome.rows, dumped.error) == ("completed", 3, None)
            restored = app.add_profile(db.profile(database=target))
            ran = restore(app, restored, target, path, "statements" if engine == "mysql" else "all_or_nothing")
            assert (ran.outcome.outcome, ran.outcome.rows, ran.error) == ("completed", 3, None)
            assert admin.rows(target, count) == admin.rows(source, count) == [(3,)]
            app.close_sessions()
    finally:
        app.close()
        db.close()


def test_a_dump_of_one_table_with_a_filter_and_never_into_its_own_file(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    conn = sqlite3.connect(source, isolation_level=None)
    conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); "
                       "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 50) "
                       "INSERT INTO t SELECT i, 'v' || i FROM n;")
    conn.close()
    app = DataApp(tmp_path)
    try:
        profile = app.add_profile({"profile": {"engine": "sqlite", "name": "source", "path": str(source)}})
        path = tmp_path / "t.sql"
        one = DumpScope(kind="tables", schema_name="main", names=["t"])
        ran = dump(app, profile, None, path, one, content="data", filter="id <= 10")
        assert (ran.outcome.outcome, ran.outcome.rows) == ("completed", 10)
        assert "CREATE TABLE" not in path.read_text() and "(10, 'v10')" in path.read_text()
        plan = app.container.data.dump_plan(profile, None, one, "both", DumpObjectKinds(), str(path))
        assert [(o.kind, o.name, o.rows_estimate) for o in plan.objects] == [("table", "t", None)]
        assert plan.warnings == ["While the dump reads, other connections cannot write to this file (it uses a "
                                 "rollback journal, not WAL)"]
        from dbaguy_backend.services import data_service

        class Usage:  # CR-4: little free space where the dump goes is said, never refused
            def __init__(self, folder):
                self.free = 512 << 20

        monkeypatch.setattr(data_service.shutil, "disk_usage", Usage)
        low = app.container.data.dump_plan(profile, None, one, "both", DumpObjectKinds(), str(path))
        assert low.warnings[-1] == "Only 0.5 GB free where the dump is written"
        assert app.container.data.dump_plan(profile, None, one, "schema", DumpObjectKinds(), str(path)).warnings == \
            plan.warnings  # a schema-only dump is small
        monkeypatch.undo()
        refused = dump(app, profile, None, source, one)  # pre-flight M13
        assert (refused.outcome.outcome, refused.error.code) == ("not_started", "invalid_path")
        assert app.container.data.dump_plan(profile, None, one, "both", DumpObjectKinds(), str(source)).warnings[-1] \
            == f"{source} is a SQLite database file: choose another file to dump into"
        assert sqlite3.connect(source).execute("SELECT COUNT(*) FROM t").fetchone() == (50,)
        assert app.history() == []  # a dump is a read: no History record
        assert [(job.job, job.title, job.outcome.outcome) for job in app.container.jobs.list()] == [
            ("dump", f"Dump main.t of source to {source.name}", "not_started"),
            ("dump", "Dump main.t of source to t.sql", "completed")]
    finally:
        app.close()


def test_the_dump_routes_plan_and_show_only_what_a_dump_wrote(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from dbaguy_backend.app import create_app

    source = tmp_path / "source.db"
    sqlite3.connect(source).executescript("CREATE TABLE t (id INTEGER PRIMARY KEY); CREATE VIEW v AS SELECT id FROM t; "
                                          "PRAGMA user_version = 3;")
    app = DataApp(tmp_path)
    shown: list[Path] = []
    monkeypatch.setattr(app.container.data, "_reveal", shown.append)  # never Finder in a test
    auth = {"Authorization": "Bearer data"}
    try:
        profile = app.add_profile({"profile": {"engine": "sqlite", "name": "source", "path": str(source)}})
        path = tmp_path / "out.sql"
        with TestClient(create_app(app.container.settings, app.container), base_url="http://127.0.0.1") as client:
            plan = client.post("/data/dump/plan", headers=auth, json={
                "profile_id": str(profile), "scope": {"kind": "schema", "schema_name": "main"}, "path": str(path)})
            assert plan.status_code == 200, plan.text
            # the header's user_version is a part of the database, not an object of the list (DumpPlan)
            assert [(o["kind"], o["name"]) for o in plan.json()["objects"]] == [("table", "t"), ("view", "v")]
            assert plan.json()["consistency"] == "snapshot"
            assert plan.json()["not_included"] == ["owners and privileges"]
            assert client.post("/data/reveal", headers=auth, json={"path": str(path)}).status_code == 404
            ran = dump(app, profile, None, path, DumpScope(kind="schema", schema_name="main"))
            assert ran.outcome.outcome == "completed" and "PRAGMA user_version = 3" in path.read_text()
            assert client.post("/data/reveal", headers=auth, json={"path": str(path)}).status_code == 204
            assert shown == [path]
            other = tmp_path / "other.sql"
            other.write_text("x")
            assert client.post("/data/reveal", headers=auth, json={"path": str(other)}).status_code == 404
            engines = {e["engine"]: e["dump"] for e in client.get("/system/engines", headers=auth).json()}
            assert engines == {"postgres": True, "mysql": True, "mssql": True, "couchbase": False, "sqlite": True}
            session = client.post("/sessions", headers=auth, json={"profile_id": str(profile)}).json()["session_id"]
            with client.websocket_connect(f"ws://127.0.0.1/sessions/{session}/ws?token=data") as ws:
                again = tmp_path / "ws.sql"
                ws.send_json({"type": "dump", "request_id": "w", "path": str(again),
                              "scope": {"kind": "schema", "schema_name": "main"}, "content": "schema"})
                events = _until_finished(ws, "w")
                assert [e["type"] for e in events][0] == "started" and events[-1]["type"] == "finished"
                [done] = [e for e in events if e["type"] == "job_outcome"]
                assert (done["outcome"], done["path"], done["rows"]) == ("completed", str(again), 0)
                assert "CREATE TABLE t" in again.read_text() and "INSERT" not in again.read_text()
                ws.send_json({"type": "dump", "request_id": "bad", "path": str(again), "filter": "id = 1",
                              "scope": {"kind": "schema", "schema_name": "main"}})  # a filter needs one table
                events = _until_finished(ws, "bad")
                assert [(e["type"], e.get("code"), e.get("outcome")) for e in events] == [
                    ("error", "invalid_command", None), ("job_outcome", None, "not_started"),
                    ("finished", None, None)]
            couchbase = app.add_profile({"profile": {"engine": "couchbase", "name": "cb", "host": "127.0.0.1",
                                                     "username": "x"}})
            refused = client.post("/data/dump/plan", headers=auth, json={
                "profile_id": str(couchbase), "scope": {"kind": "schema", "schema_name": "app"}})
            assert (refused.status_code, refused.json()["error"]["message"]) == (
                422, "Couchbase connections cannot be dumped")
    finally:
        app.close()


def _until_finished(ws, request_id: str) -> list[dict]:
    """`request_id`'s events up to its `finished` (the session's own `session_state` events left out)."""
    events: list[dict] = []
    while not events or events[-1]["type"] != "finished":
        event = ws.receive_json()
        if event.get("request_id") == request_id and event["type"] != "session_state":
            events.append(event)
    return events


def _read_ahead(connection, sql: str) -> int:
    """How many rows past a job's fetch(1) the adapter holds (RunOptions.describe False: none, pre-flight M12)."""
    result = connection.run(sql, NO_DESCRIBE)
    try:
        assert len(result.fetch(1)) == 1
        return len(getattr(result, "_pending", None) or getattr(result, "_buffer", None) or [])
    finally:
        result.close()


@pytest.mark.parametrize("engine", [
    "sqlite", pytest.param("postgres", marks=requires_pg), pytest.param("mssql", marks=requires_mssql),
])
def test_the_adapters_read_no_rows_ahead_of_a_jobs_fetch(tmp_path, engine):
    """SQLite peeks at 200 rows for a grid's column types, SQL Server at 100 when it does not describe the result,
    PostgreSQL's cursor reads a page ahead: a job's read (describe False) holds only what it asked for, while a grid's
    still reads ahead (SQL Server's grid describes instead)."""
    db = scratch_db(engine, tmp_path)
    conn = db.open()
    try:
        sql = {"sqlite": "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 500) "
                         "SELECT i FROM n",
               "postgres": "SELECT i FROM generate_series(1, 500) i",
               "mssql": "SELECT TOP (500) object_id FROM sys.all_objects"}[engine]
        if engine == "postgres":
            conn.run("BEGIN").close()  # a dump reads inside its snapshot: the cursor path
        assert _read_ahead(conn, sql) == 0
        grid = conn.run(sql, RunOptions())
        try:
            grid.fetch(1)
            held = len(getattr(grid, "_pending", None) or getattr(grid, "_buffer", None) or [])
        finally:
            grid.close()
        assert held == (0 if engine == "mssql" else 199)  # the grid's own reads are as they were
    finally:
        conn.close()
        db.close()


# -- Task 9 fix round 1


@requires_mssql
def test_an_error_later_in_a_sql_server_result_fails_the_dump_and_the_export(tmp_path):  # fix H1
    """SQL Server sends the rows before an error later in the set first (a filter that divides by zero at row 2500):
    the dump fails instead of writing 2,499 rows as the whole table, and so does a CSV export of the same SELECT."""
    from dbaguy_backend.domain.execution import ExportCommand
    from tests.test_sessions import request_events

    db = scratch_db("mssql", tmp_path)
    app = DataApp(tmp_path)
    try:
        with scratch_database("mssql", "mid") as (source, admin):
            admin.run_in(source, "CREATE TABLE dbo.t (id int PRIMARY KEY, v int)",
                         "INSERT INTO dbo.t SELECT TOP (3000) ROW_NUMBER() OVER (ORDER BY (SELECT 1)), 1 "
                         "FROM sys.all_objects a CROSS JOIN sys.all_objects b")
            profile = app.add_profile(db.profile(database=source))
            target = tmp_path / "t.sql"
            target.write_text("the old dump")
            dumped = dump(app, profile, source, target, DumpScope(kind="tables", schema_name="dbo", names=["t"]),
                          filter="10 / (id - 2500) IS NOT NULL")
            assert (dumped.outcome.outcome, dumped.error.code) == ("failed", "8134")
            assert target.read_text() == "the old dump" and not list(tmp_path.glob(".*.part"))
            tab = app.job_session(profile, source)
            csv = tmp_path / "e.csv"
            tab.worker.submit(ExportCommand(request_id="e", sql="SELECT id FROM dbo.t WHERE 10 / (id - 2500) IS NOT "
                                                                "NULL", format="csv", path=str(csv)))
            events = request_events(tab.recorder, "e", 60)
            assert [(e.type, getattr(e, "code", None)) for e in events if e.type in ("error", "finished")] == [
                ("error", "8134"), ("finished", None)]
            assert not csv.exists()
            app.close_sessions()
    finally:
        app.close()
        db.close()


@pytest.mark.parametrize("engine, read_only, condition", [
    pytest.param("mysql", True, "1=1 INTO OUTFILE '/var/lib/mysql-files/zz_06_never.txt'", marks=requires_mysql),
    pytest.param("mysql", False, "1=1 INTO OUTFILE '/var/lib/mysql-files/zz_06_never.txt'", marks=requires_mysql),
    pytest.param("mysql", True, "1=1 FOR UPDATE", marks=requires_mysql),
    pytest.param("postgres", False, "id = 1; COPY t TO STDOUT", marks=requires_pg),
    pytest.param("mssql", False, "1=1 SELECT * INTO dbo.zz_06_never FROM dbo.t", marks=requires_mssql),
    pytest.param("mssql", False, "1=1 EXEC sp_who", marks=requires_mssql),
    # fix round 2: clauses after the condition (a fabricated row, XML as a value, rows into variables)
    pytest.param("postgres", False, "1=1 UNION ALL SELECT 99", marks=requires_pg),
    pytest.param("mssql", False, "1=1 UNION ALL SELECT 99", marks=requires_mssql),
    pytest.param("mssql", False, "1=1 FOR XML PATH", marks=requires_mssql),
    pytest.param("mysql", False, "id = 1 INTO @a", marks=requires_mysql),
])
def test_a_filter_is_a_condition_and_nothing_else(tmp_path, engine, read_only, condition):  # fix M2
    db = scratch_db(engine, tmp_path)
    app = DataApp(tmp_path)
    try:
        with scratch_database(engine, "ro") as (source, admin):
            schema = {"postgres": "public", "mysql": source, "mssql": "dbo"}[engine]
            admin.run_in(source, f"CREATE TABLE {'' if engine == 'mysql' else schema + '.'}t (id int PRIMARY KEY)")
            profile = app.add_profile(db.profile(database=source, read_only=read_only))
            ran = dump(app, profile, source, tmp_path / "o.sql", DumpScope(kind="tables", schema_name=schema,
                                                                          names=["t"]), filter=condition)
            assert (ran.outcome.outcome, ran.error.code) == ("not_started", "invalid_filter")
            assert condition not in ran.error.message and not (tmp_path / "o.sql").exists()
            app.close_sessions()
    finally:
        app.close()
        db.close()


@requires_pg
def test_a_plan_waits_for_a_locked_table_at_most_five_seconds_and_says_why(tmp_path):  # fix M3
    """An uncommitted ALTER in another session: the plan answers in about 5 s with what is wrong, and the tree (on
    the same metadata connection) answers right after it."""
    import threading
    import time

    from fastapi.testclient import TestClient

    from dbaguy_backend.app import create_app
    from dbaguy_backend.domain.metadata import MetadataRequest, PathSegment

    db = scratch_db("postgres", tmp_path)
    app = DataApp(tmp_path)
    try:
        with scratch_database("postgres", "lock") as (source, admin):
            admin.run_in(source, "CREATE TABLE t (id int)", "CREATE TABLE other (id int)")
            profile = app.add_profile(db.profile(database=source))
            tree = MetadataRequest(profile_id=profile, path=[PathSegment(kind="database", name=source),
                                                             PathSegment(kind="schema", name="public")])
            assert app.container.metadata.children(tree)
            hold = pg_inventory.connect(source, autocommit=False)
            try:
                hold.execute("ALTER TABLE t ADD COLUMN x int")  # uncommitted, as in a manual-commit tab
                answered: dict[str, float] = {}

                def tree_later():
                    time.sleep(1)
                    app.container.metadata.children(tree)
                    answered["tree"] = time.monotonic()

                later = threading.Thread(target=tree_later)
                started = time.monotonic()
                later.start()
                with TestClient(create_app(app.container.settings, app.container), base_url="http://127.0.0.1") as c:
                    plan = c.post("/data/dump/plan", headers={"Authorization": "Bearer data"}, json={
                        "profile_id": str(profile), "database": source,
                        "scope": {"kind": "tables", "schema_name": "public", "names": ["other"]}})
                took = time.monotonic() - started
                later.join(10)
                assert plan.status_code == 400 and plan.json()["error"] == {
                    "code": "55P03", "message": "A table in a schema this dump reads is locked by another session (for "
                                                "example an uncommitted ALTER): try again after it commits"}
                assert 4.5 < took < 8 and answered["tree"] - started < took + 1.5
            finally:
                hold.rollback()
                hold.close()
            plan = app.container.data.dump_plan(profile, source, DumpScope(kind="tables", schema_name="public",
                                                                           names=["other"]), "both",
                                                DumpObjectKinds(), None)
            assert [(o.kind, o.name) for o in plan.objects] == [("schema", "public"), ("table", "public.other")]
            app.close_sessions()
    finally:
        app.close()
        db.close()


@requires_pg
def test_a_postgresql_data_only_dump_refreshes_its_views_and_says_which_tables_reference_themselves(tmp_path):
    """Fix L3: a data-only file ends with REFRESH MATERIALIZED VIEW, so a view restored with the schema alone (empty)
    comes back with the rows; the plan lists it. Fix M4: a table that references itself is said in the plan and the
    outcome (its rows here load in order, so this restore works)."""
    db = scratch_db("postgres", tmp_path)
    app = DataApp(tmp_path)
    try:
        with scratch_database("postgres", "src") as (source, admin), \
                scratch_database("postgres", "restore") as (target, _):
            admin.run_in(source, "CREATE TABLE p (id int PRIMARY KEY, v text)",
                         "CREATE TABLE c (id int PRIMARY KEY, p_id int REFERENCES p (id))",
                         "CREATE TABLE emp (id int PRIMARY KEY, boss int REFERENCES emp (id))",
                         "INSERT INTO p SELECT g, 'v' || g FROM generate_series(1, 30) g",
                         "INSERT INTO c SELECT g, g FROM generate_series(1, 30) g",
                         "INSERT INTO emp SELECT g, NULLIF(g - 1, 0) FROM generate_series(1, 50) g",
                         "CREATE MATERIALIZED VIEW mv AS SELECT count(*) AS n FROM p",
                         "CREATE MATERIALIZED VIEW mv_later AS SELECT 1 AS one WITH NO DATA")
            profile = app.add_profile(db.profile(database=source))
            scope = DumpScope(kind="database")
            said = "public.emp references itself: a data-only restore may fail unless its rows load in order"
            plan = app.container.data.dump_plan(profile, source, scope, "data", DumpObjectKinds(), None)
            assert [(o.kind, o.name) for o in plan.objects] == [
                ("table", "public.p"), ("table", "public.c"), ("table", "public.emp"), ("refresh", "public.mv")]
            assert plan.warnings == [said]
            schema_file, data_file = tmp_path / "schema.sql", tmp_path / "data.sql"
            assert dump(app, profile, source, schema_file, scope, content="schema").outcome.outcome == "completed"
            dumped = dump(app, profile, source, data_file, scope, content="data")
            assert (dumped.outcome.outcome, dumped.outcome.warnings) == ("completed", [said])
            assert "REFRESH MATERIALIZED VIEW public.mv;" in data_file.read_text()
            assert "REFRESH" not in schema_file.read_text() and "mv_later" not in data_file.read_text()
            both = tmp_path / "both.sql"
            assert dump(app, profile, source, both, scope).outcome.outcome == "completed"
            assert "REFRESH" not in both.read_text()  # with the schema the view is made with its rows
            restored = app.add_profile(db.profile(database=target))
            for path in (schema_file, data_file):
                ran = restore(app, restored, target, path, "all_or_nothing")
                assert (ran.outcome.outcome, ran.error) == ("completed", None)
            app.close_sessions()
            assert pg_inventory.differences(pg_inventory.inventory(source), pg_inventory.inventory(target)) == {}
    finally:
        app.close()
        db.close()


def test_sqlite_text_that_is_not_utf8_names_its_table_and_column(tmp_path):  # fix L2
    source = tmp_path / "bad.db"
    conn = sqlite3.connect(source, isolation_level=None)
    conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t VALUES (1, 'ok'); "
                       "INSERT INTO t VALUES (2, CAST(X'61FF62' AS TEXT));")
    conn.close()
    app = DataApp(tmp_path)
    try:
        profile = app.add_profile({"profile": {"engine": "sqlite", "name": "bad", "path": str(source)}})
        target = tmp_path / "bad.sql"
        target.write_text("old")
        ran = dump(app, profile, None, target, DumpScope(kind="schema", schema_name="main"))
        assert (ran.outcome.outcome, ran.error.code, ran.error.message) == (
            "failed", "invalid_text", "t.v holds text that is not valid UTF-8, which a UTF-8 file cannot hold. "
                                      "Nothing was written")
        assert target.read_text() == "old"
        # fix round 2 (N2): in a name, the tree says the schema, never the catalog's own column
        named = tmp_path / "badname.db"
        conn = sqlite3.connect(named, isolation_level=None)
        conn.execute("CREATE TABLE ok (id INTEGER)")
        conn.execute("PRAGMA writable_schema = ON")
        name, ddl = "61ff", 'CREATE TABLE "a\xff" (id INTEGER)'.encode("latin-1").hex()  # a name that is not UTF-8
        conn.execute(f"UPDATE sqlite_master SET name = CAST(X'{name}' AS TEXT), tbl_name = CAST(X'{name}' AS TEXT), "
                     f"sql = CAST(X'{ddl}' AS TEXT) WHERE name = 'ok'")
        conn.close()
        from dbaguy_backend.domain.errors import QueryFailed
        from dbaguy_backend.domain.metadata import MetadataRequest, PathSegment

        bad = app.add_profile({"profile": {"engine": "sqlite", "name": "badname", "path": str(named)}})
        tables = [PathSegment(kind="schema", name="main"), PathSegment(kind="folder", name="tables")]
        with pytest.raises(QueryFailed) as refused:
            app.container.metadata.children(MetadataRequest(profile_id=bad, path=tables))
        assert refused.value.message == ("A name or definition in the database's schema holds text that is not "
                                         "valid UTF-8")
    finally:
        app.close()
