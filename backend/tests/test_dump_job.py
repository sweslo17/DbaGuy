"""The dump job (spec ⑥ §3.3, R-06-12, CR-4) on a fake connection: the file it writes, the snapshot it opens and ends,
and every way it stops — cancelled mid-table, a full disk (or nearly full: pre-flight M14), a failing read, a bad or
dangerous path (M13), text that is not Unicode (L15) — leaving no file and any old one as it was; how it reads (M12:
sized by the rows' text), what it says (M11: a row too long to restore; a table changed under it) and what it writes
for a table it cannot read columns of (rows of defaults) or a data-only dump (parents first, M9). The live engines
follow in test_dump.py."""

import errno
import threading
import tracemalloc
from pathlib import Path

import pytest

from dbaguy_backend.adapters.base import AdapterCapabilities, Dumping, StatementCancelled, StatementError
from dbaguy_backend.adapters.columns import ROWVERSION, TableColumn, not_copied
from dbaguy_backend.adapters.dump import DumpCatalog, DumpObject, DumpSession, DumpTable
from dbaguy_backend.domain.data_jobs import DumpScope
from dbaguy_backend.domain.execution import DumpCommand
from dbaguy_backend.domain.profiles import Engine
from dbaguy_backend.sessions.jobs import dump as dump_module
from dbaguy_backend.sessions.jobs.context import JobHooks
from dbaguy_backend.sessions.jobs.dump import FETCH_CHARS, FIRST_FETCH, FK_CYCLE, SELF_REFERENCE, DumpJob
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.dump_header import parse_header
from dbaguy_backend.sql.splitter import split
from tests.fake_db import FakeConnection, FakeLease, FakeResult
from tests.test_sessions import Recorder, request_events


def column(name: str, type_name: str = "TEXT", kind: str = "text", **fields) -> TableColumn:
    return TableColumn(name=name, type_name=type_name, family=fields.pop("family", "text"), nullable=True,
                       has_default=False, kind=kind, compare="equals", writable=True, **fields)


COLUMNS = (column("id", "INTEGER", "number", family="int64"), column("v"))
TABLE = DumpTable(schema="main", table="t", name="t", columns=COLUMNS)


class Rows(FakeResult):
    """`count` rows (id, value(id)); with `stop_at`, waits once `stop_at` rows were read until a cancel, then
    StatementCancelled. With `fails_at`, a short chunk ends at that row and the next fetch raises the connection's
    `late_error`, as SQL Server's result does with an error later in the set. Every `n` asked for is kept in the
    connection's `fetches`."""

    def __init__(self, count: int, conn: "DumpFake") -> None:
        super().__init__(0)
        self._next, self._count, self._conn = 0, count, conn

    def fetch(self, n):
        self._conn.fetches.append(n)
        if self._conn.stop_at is not None and self._next >= self._conn.stop_at:
            self._conn.blocked.set()
            assert self._conn._unblock.wait(10), "never cancelled"
            raise StatementCancelled()
        if self._conn.fails_at is not None and self._next >= self._conn.fails_at:
            raise self._conn.late_error
        end = min(self._next + n, self._count, self._conn.fails_at or self._count)
        rows = [[i, self._conn.value(i)] for i in range(self._next, end)]
        self._next = end
        return rows


class DumpFake(FakeConnection, Dumping):
    """A Dumping connection: one table `t` (or `tables`), `rows` rows each; SELECTs are kept in `runs`."""

    def __init__(self, rows: int = 2500, stop_at: int | None = None, fail_select: StatementError | None = None,
                 tables: tuple[DumpTable, ...] = (TABLE,), session: DumpSession | None = None, value=None,
                 fails_at: int | None = None, late_error: StatementError | None = None,
                 catalog_error: StatementError | None = None, **catalog) -> None:
        super().__init__()
        self.rows, self.stop_at, self.fail_select = rows, stop_at, fail_select
        self.fails_at, self.late_error, self.catalog_error = fails_at, late_error, catalog_error
        self.tables, self.catalog = tables, catalog
        self.session = session or DumpSession(setup=("snapshot on",), teardown=("snapshot off",),
                                              consistency="snapshot")
        self.value = value or (lambda i: f"v{i}")
        self.fetches: list[int] = []
        self.hold_session: tuple[threading.Event, threading.Event] | None = None  # (reached, go)

    def dump_session(self, scope):
        if self.hold_session is not None:
            reached, go = self.hold_session
            reached.set()
            assert go.wait(10)
        return self.session

    def dump_catalog(self, scope, kinds, *, ddl=True):
        if self.catalog_error is not None:
            raise self.catalog_error
        return DumpCatalog(
            server="SQLite 3", scope="schema main", consistency="snapshot",
            pre=(DumpObject("table", "t", ("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)",),
                            "DROP TABLE IF EXISTS t"),),
            tables=self.tables, post=(DumpObject("index", "t_v", ("CREATE INDEX t_v ON t (v)",)),),
            warnings=("a warning",), **self.catalog,
        )

    def _interrupt(self, kind: str) -> None:
        super()._interrupt(kind)
        if self.blocked.is_set():  # a cancel reaches a fetch, as it reaches the driver's read in the adapters
            self._unblock.set()

    def statement(self, sql, options):
        if sql.startswith("SELECT COUNT(*)"):
            self.runs.append(sql)
            return _Count(self.rows)
        if sql.startswith("SELECT"):
            self.runs.append(sql)
            if self.fail_select is not None:
                raise self.fail_select
            return Rows(self.rows, self)
        return super().statement(sql, options)


class _Count(FakeResult):
    def __init__(self, count: int) -> None:
        super().__init__(0)
        self._rows = [[count]]

    def fetch(self, n):
        taken, self._rows = self._rows[:n], self._rows[n:]
        return taken


class Env:
    def __init__(self, conn: DumpFake, dialect: Dialect, engine: Engine, source: Path | None, gate) -> None:
        self.conn = conn
        lease = FakeLease(conn)
        self.worker = SessionWorker("dump", lambda: lease)
        self.worker.start()
        self.gated: list[str] = []

        def gating(sql):
            self.gated.append(sql)
            return gate(sql) if gate is not None else JobHooks().gate(sql)

        jobs = JobHooks(build=lambda command: DumpJob(command, "Fake", engine, source_file=source), gate=gating)
        self.worker.configure(WorkerHooks(dialect=dialect, capabilities=AdapterCapabilities(dump=True), jobs=jobs))
        self.recorder = Recorder()
        self.worker.set_emitter(self.recorder)

    def submit(self, path: Path, **fields) -> None:
        fields.setdefault("scope", DumpScope(kind="schema", schema_name="main"))
        self.worker.submit(DumpCommand(request_id="d", path=str(path), **fields))

    def run(self, path: Path, **fields) -> list:
        self.submit(path, **fields)
        return request_events(self.recorder, "d")


@pytest.fixture
def env():
    made: list[Env] = []

    def make(conn: DumpFake | None = None, dialect: Dialect = Dialect.SQLITE, engine: Engine = Engine.SQLITE,
             source: Path | None = None, gate=None) -> Env:
        made.append(Env(conn or DumpFake(), dialect, engine, source, gate))
        return made[-1]

    yield make
    for e in made:
        e.worker.close()


def outcome(events):
    [found] = [e for e in events if e.type == "job_outcome"]  # exactly one
    return found


def error(events):
    return next((e for e in events if e.type == "error"), None)


def statements(path: Path, dialect: Dialect = Dialect.SQLITE) -> list[str]:
    return [s.text for s in split(path.read_text(encoding="utf-8"), dialect)]


def test_a_dump_writes_the_file_and_ends_its_snapshot(env, tmp_path):
    e = env()
    target = tmp_path / "out.sql"
    events = e.run(target)
    done = outcome(events)
    assert (done.outcome, done.rows, done.path, done.bytes, done.warnings) == (
        "completed", 2500, str(target), target.stat().st_size, ["a warning"])
    assert [(o.kind, o.name, o.rows) for o in done.objects] == [("table", "t", 2500)]
    assert e.conn.runs == ["snapshot on", "SELECT id, v FROM main.t", "snapshot off"]
    text = target.read_text()
    assert parse_header(text).engine == Engine.SQLITE and text.endswith("-- Dump complete: 2,500 rows in 1 tables\n")
    found = statements(target)
    assert found[:3] == ["PRAGMA foreign_keys = OFF", "PRAGMA defer_foreign_keys = ON",
                         "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"]
    inserts = [s for s in found if s.startswith("INSERT")]
    assert [s.count("\n") for s in inserts] == [1000, 1000, 500]  # 1,000 rows a statement, whatever was fetched
    assert found[-2:] == ["CREATE INDEX t_v ON t (v)", "PRAGMA foreign_keys = ON"]
    progress = [ev for ev in events if ev.type == "job_progress"]
    assert [p.stage for p in progress][:2] == ["schema", "data"] and progress[1].object == "t"
    assert progress[-1].stage == "post_data" and progress[-1].rows == 2500
    assert e.conn.fetches[0] == FIRST_FETCH and max(e.conn.fetches) == 1000  # short rows: 1,000 at a time
    assert all(options.describe is False for options in e.conn.options)  # no read-ahead, no describe (M12)


def test_a_cancelled_dump_leaves_no_file_and_the_old_one_as_it_was(env, tmp_path):
    e = env(DumpFake(rows=5000, stop_at=2000))
    target = tmp_path / "out.sql"
    target.write_text("the old dump")
    e.submit(target)
    assert e.conn.blocked.wait(5)
    e.worker.cancel("d")
    events = request_events(e.recorder, "d")
    done = outcome(events)
    assert (done.outcome, done.cancelled, done.rows, error(events).code) == ("failed", True, 0, "cancelled")
    assert target.read_text() == "the old dump" and sorted(p.name for p in tmp_path.iterdir()) == ["out.sql"]
    assert e.conn.runs[-1] == "snapshot off"  # the snapshot is ended all the same


def _full_disk(monkeypatch, failure: OSError) -> None:
    """The dump's file raises `failure` once 4 KiB were written (as a real writer would at ENOSPC)."""
    real = dump_module.atomic_text_file

    class Full:
        def __init__(self, file):
            self._file, self._written = file, 0

        def write(self, text):
            self._written += len(text)
            if self._written > 4096:
                raise failure
            return self._file.write(text)

    def full_disk(target):
        context = real(target)

        class Wrapped:
            def __enter__(self):
                return Full(context.__enter__())

            def __exit__(self, *exc):
                return context.__exit__(*exc)

        return Wrapped()

    monkeypatch.setattr(dump_module, "atomic_text_file", full_disk)


def test_a_full_disk_says_so_and_leaves_nothing(env, tmp_path, monkeypatch):  # CR-4
    _full_disk(monkeypatch, OSError(errno.ENOSPC, "No space left on device"))
    target = tmp_path / "out.sql"
    events = env().run(target)
    assert (outcome(events).outcome, error(events).code) == ("failed", "dump_io")
    assert error(events).message == f"Cannot write {target}: Disk full. Nothing was written"
    assert list(tmp_path.iterdir()) == []


def test_any_other_write_error_gives_the_systems_reason(env, tmp_path, monkeypatch):
    _full_disk(monkeypatch, OSError(errno.EIO, "Input/output error"))
    old = tmp_path / "out.sql"
    old.write_text("old")
    events = env().run(old)
    assert error(events).message == f"Cannot write {old}: Input/output error. Nothing was written"
    assert [p.name for p in tmp_path.iterdir()] == ["out.sql"] and old.read_text() == "old"


def test_a_disk_close_to_full_stops_the_dump_before_it_fills_it(env, tmp_path, monkeypatch):  # pre-flight M14
    free = [10 << 30]  # plenty at first: the check at the first write passes
    checked: list[int] = []

    class Usage:
        def __init__(self, folder):
            checked.append(1)
            self.free = free[0]

    monkeypatch.setattr(dump_module.shutil, "disk_usage", Usage)
    monkeypatch.setattr(dump_module, "DISK_CHECK_CHARS", 10_000)  # look every 10,000 characters written
    e = env(DumpFake(rows=20_000))
    target = tmp_path / "out.sql"
    e.submit(target)
    e.recorder.wait_for(lambda ev: ev.type == "job_progress" and ev.stage == "data")
    free[0] = 100 << 20  # 100 MiB: below the 256 MiB floor
    events = request_events(e.recorder, "d")
    assert (outcome(events).outcome, error(events).code) == ("failed", "dump_io")
    assert error(events).message == (f"Cannot write {target}: Disk almost full (less than 256 MiB free), so the dump "
                                     "stopped before filling it. Nothing was written")
    assert list(tmp_path.iterdir()) == [] and len(checked) > 1


def test_a_failing_read_and_a_bad_path(env, tmp_path):
    e = env(DumpFake(fail_select=StatementError("no such table: t", code="SQLITE_ERROR")))
    events = e.run(tmp_path / "out.sql")
    assert (outcome(events).outcome, error(events).message) == ("failed", "no such table: t")
    assert list(tmp_path.iterdir()) == [] and e.conn.runs[-1] == "snapshot off"
    for path in (tmp_path, tmp_path / "missing" / "out.sql", Path("relative.sql")):
        e = env()
        events = e.run(path)
        assert (outcome(events).outcome, error(events).code) == ("not_started", "invalid_path")
        assert e.conn.runs == []  # nothing was sent


def test_a_dump_never_replaces_a_sqlite_database_or_the_file_it_reads(env, tmp_path):  # pre-flight M13
    database = tmp_path / "app.db"
    database.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    source = tmp_path / "source.sqlite"
    source.write_bytes(b"")  # the dumped file, still empty: no header to know it by
    for target, given in ((database, None), (tmp_path / "app.db-wal", None), (tmp_path / "x-journal", None),
                          (source, source)):
        e = env(source=given)
        events = e.run(target)
        assert (outcome(events).outcome, error(events).code) == ("not_started", "invalid_path"), target
        assert error(events).message == f"{target} is a SQLite database file: choose another file to dump into"
        assert e.conn.runs == []
    assert database.read_bytes().startswith(b"SQLite format 3\x00") and source.read_bytes() == b""


def test_a_filter_reads_only_the_matching_rows_as_the_tab_reads_them(env, tmp_path):  # pre-flight L11
    session = DumpSession(setup=("snapshot on",), teardown=("snapshot off",), consistency="snapshot",
                          filter_setup=("SET search_path TO DEFAULT",))
    one = DumpScope(kind="tables", schema_name="main", names=["t"])
    e = env(DumpFake(session=session))
    e.run(tmp_path / "out.sql", scope=one, filter="id < 10")
    assert e.conn.runs[1:3] == ["SET search_path TO DEFAULT", "SELECT id, v FROM main.t\nWHERE id < 10"]
    e = env(DumpFake(session=session))
    e.run(tmp_path / "all.sql", scope=one)
    assert "SET search_path TO DEFAULT" not in e.conn.runs  # without a filter the rows are read as the catalog was
    e = env()
    events = e.run(tmp_path / "bad.sql", scope=one, filter="1; DROP TABLE t")
    assert (outcome(events).outcome, error(events).code, error(events).message) == (
        "not_started", "invalid_filter", "A filter must be a single expression")
    assert e.conn.runs == []


@pytest.mark.parametrize("dialect, condition", [
    (Dialect.MYSQL, "1=1 INTO OUTFILE '/var/lib/mysql-files/zz_06_never.txt'"),
    (Dialect.MYSQL, "1=1 FOR UPDATE"), (Dialect.MYSQL, "1=1 LOCK IN SHARE MODE"),
    (Dialect.POSTGRES, "set_config('default_transaction_read_only', 'off', false) IS NOT NULL"),
    (Dialect.POSTGRES, "1=1 FOR SHARE"),
])
def test_a_filter_that_does_more_than_read_is_never_sent(env, tmp_path, dialect, condition):  # Task 9 fix M2
    """Whatever the profile (this one is writable): the SELECT the filter makes must be a plain read."""
    e = env(dialect=dialect)
    events = e.run(tmp_path / "out.sql", scope=DumpScope(kind="tables", schema_name="main", names=["t"]),
                   filter=condition)
    assert (outcome(events).outcome, error(events).code) == ("not_started", "invalid_filter")
    assert error(events).message == dump_module.FILTER_NOT_A_CONDITION and condition not in error(events).message
    assert e.conn.runs == [] and list(tmp_path.iterdir()) == []


def test_a_read_only_profiles_gate_sees_every_select_a_filter_makes(env, tmp_path):  # Task 9 fix M2
    from dbaguy_backend.domain.classification import Classification, StatementKind
    from dbaguy_backend.services.safety import GuardAction, GuardDecision

    def refuse(sql):
        return GuardDecision(action=GuardAction.BLOCK, classification=Classification(kind=StatementKind.READ))

    e = env(gate=refuse)
    one = DumpScope(kind="tables", schema_name="main", names=["t"])
    events = e.run(tmp_path / "out.sql", scope=one, filter="id < 3")
    assert (outcome(events).outcome, error(events).code) == ("not_started", "read_only") and e.conn.runs == []
    e = env()
    assert outcome(e.run(tmp_path / "ok.sql", scope=one, filter="id < 3")).outcome == "completed"
    assert e.gated == ["SELECT COUNT(*) FROM main.t\nWHERE id < 3", "SELECT id, v FROM main.t\nWHERE id < 3"]
    e = env()
    e.run(tmp_path / "all.sql", scope=one)
    assert e.gated == []  # without a filter every SELECT is DbaGuy's own


def test_a_cancel_between_chunks_stops_the_dump(env, tmp_path):
    e = env(DumpFake(rows=200_000))
    target = tmp_path / "out.sql"
    e.submit(target)
    e.recorder.wait_for(lambda ev: ev.type == "job_progress" and ev.rows > 0)
    threading.Thread(target=e.worker.cancel, args=("d",)).start()
    done = outcome(request_events(e.recorder, "d"))
    assert (done.outcome, done.cancelled) == ("failed", True) and not target.exists()


def test_large_rows_are_read_a_few_at_a_time(env, tmp_path):  # pre-flight M12, Task 9 fix M1
    """40 rows of 2 MiB of text: the first read takes one row, the next ones FETCH_CHARS of row text at that size, so
    the dump never holds every row (80 MiB) at once."""
    big = "x" * (2 << 20)
    e = env(DumpFake(rows=40, value=lambda i: big))
    tracemalloc.start()
    try:
        done = outcome(e.run(tmp_path / "out.sql"))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (done.outcome, done.rows) == ("completed", 40)
    per_fetch = FETCH_CHARS // ((2 << 20) + 2)  # rows of row text (the value's quotes) a fetch is sized to
    assert FIRST_FETCH == 1 and e.conn.fetches == [1, per_fetch, per_fetch, per_fetch, per_fetch]  # … then `[]`
    assert peak < 60 << 20, peak  # ≈ 30 MiB of rows + a statement's text; all 40 at once would be 80 MiB and more


def test_reads_are_sized_by_the_largest_row_seen(env, tmp_path):  # Task 9 fix M1
    """Short rows, then one of 4 MiB: from then on a read takes what fits FETCH_CHARS at 4 MiB a row, even when the
    rows after it are short again (the mean would let a thousand of the next large ones in)."""
    big = "x" * (4 << 20)
    e = env(DumpFake(rows=3000, value=lambda i: big if i == 5 else "s"))
    assert outcome(e.run(tmp_path / "out.sql")).rows == 3000
    small = FETCH_CHARS // ((4 << 20) + 2)
    assert e.conn.fetches[:4] == [1, 1000, small, small] and max(e.conn.fetches[2:]) == small


def test_a_row_too_long_for_the_server_is_said_once_for_its_table(env, tmp_path):  # pre-flight M11
    e = env(DumpFake(rows=400, value=lambda i: "x" * 5000, max_statement_bytes=4096), Dialect.MYSQL, Engine.MYSQL)
    done = outcome(e.run(tmp_path / "out.sql", scope=DumpScope(kind="schema", schema_name="main")))
    assert done.outcome == "completed" and len(statements(tmp_path / "out.sql", Dialect.MYSQL)) > 6  # 2 INSERTs
    [said] = done.warnings[1:]  # once for the table
    assert said.startswith("t has an INSERT statement of 10") and said.endswith(  # a batch of about 1 MiB
        " KiB: MySQL refuses statements over its max_allowed_packet (4 KiB on the server dumped) when the file is run")


def test_text_that_is_not_unicode_names_its_table_and_column(env, tmp_path):  # pre-flight L15
    e = env(DumpFake(rows=1500, value=lambda i: "half \ud83d of an emoji" if i == 1200 else f"v{i}"))
    events = e.run(tmp_path / "out.sql")
    assert (outcome(events).outcome, error(events).code) == ("failed", "invalid_text")
    assert error(events).message == ("t.v holds text that is not valid Unicode (half of a surrogate pair), which a "
                                     "UTF-8 file cannot hold. Nothing was written")
    assert list(tmp_path.iterdir()) == []


def test_a_table_with_no_column_a_dump_writes_keeps_its_rows_as_rows_of_defaults(env, tmp_path):  # Task 7 review
    stamp = column("stamp", "rowversion", "binary", family="rowversion", **not_copied(ROWVERSION))
    bare = DumpTable(schema="main", table="t", name="t", columns=(stamp,))
    e = env(DumpFake(rows=1203, tables=(bare,)))
    target = tmp_path / "out.sql"
    done = outcome(e.run(target))
    assert (done.outcome, done.rows, [(o.name, o.rows) for o in done.objects]) == ("completed", 1203, [("t", 1203)])
    assert e.conn.runs == ["snapshot on", "SELECT COUNT(*) FROM main.t", "snapshot off"]
    assert statements(target).count("INSERT INTO t DEFAULT VALUES") == 1203
    target.unlink()
    e = env(DumpFake(rows=1203, tables=(bare,)), Dialect.TSQL, Engine.MSSQL)
    e.run(target)
    text = target.read_text()
    assert "INSERT INTO main.t DEFAULT VALUES\nGO 1000\n" in text  # Task 9 fix L4: as a query tab runs it too
    assert [(s.text, s.repeat) for s in split(text, Dialect.TSQL) if "DEFAULT VALUES" in s.text] == [
        ("INSERT INTO main.t DEFAULT VALUES", 1000), ("INSERT INTO main.t DEFAULT VALUES", 203)]


def test_a_data_only_dump_writes_parents_first_and_says_when_it_cannot(env, tmp_path):  # pre-flight M9
    child = DumpTable(schema="main", table="child", name="child", columns=COLUMNS, references=("parent",))
    parent = DumpTable(schema="main", table="parent", name="parent", columns=COLUMNS)
    e = env(DumpFake(rows=2, tables=(child, parent)))
    done = outcome(e.run(tmp_path / "data.sql", content="data"))
    assert [o.name for o in done.objects] == ["parent", "child"] and FK_CYCLE not in done.warnings
    assert "CREATE TABLE" not in (tmp_path / "data.sql").read_text()
    e = env(DumpFake(rows=2, tables=(child, parent)))
    done = outcome(e.run(tmp_path / "both.sql"))
    assert [o.name for o in done.objects] == ["child", "parent"]  # with the schema: the catalog's order
    looped = DumpTable(schema="main", table="parent", name="parent", columns=COLUMNS, references=("child",))
    emp = DumpTable(schema="main", table="emp", name="emp", columns=COLUMNS, self_referencing=True)
    e = env(DumpFake(rows=2, tables=(child, looped, emp)), Dialect.POSTGRES, Engine.POSTGRES)
    done = outcome(e.run(tmp_path / "cycle.sql", content="data"))
    assert [o.name for o in done.objects] == ["emp", "child", "parent"]  # the cycle keeps the catalog's order
    assert done.warnings == ["a warning", FK_CYCLE, SELF_REFERENCE.format("emp")]  # Task 9 fix M4
    assert SELF_REFERENCE.format("emp") == ("emp references itself: a data-only restore may fail unless its rows load "
                                            "in order")
    e = env(DumpFake(rows=2, tables=(child, looped, emp)))  # SQLite defers the checks to the COMMIT: nothing to say
    assert outcome(e.run(tmp_path / "lite.sql", content="data")).warnings == ["a warning"]
    e = env(DumpFake(rows=2, tables=(emp,)), Dialect.POSTGRES, Engine.POSTGRES)
    assert outcome(e.run(tmp_path / "both.sql")).warnings == ["a warning"]  # with the schema: the keys come after


def test_a_table_changed_while_the_dump_ran_is_named(env, tmp_path):  # Task 6 review: MySQL 1412
    session = DumpSession(setup=("snapshot on",), teardown=("snapshot off",), consistency="snapshot",
                          changed_codes=frozenset({"1412"}))
    failure = StatementError("Table definition has changed, please retry transaction", code="1412")
    events = env(DumpFake(session=session, fail_select=failure)).run(tmp_path / "out.sql")
    assert (outcome(events).outcome, error(events).code) == ("failed", "1412")
    assert error(events).message == ("t was changed while the dump ran: dump it again\n"
                                     "Table definition has changed, please retry transaction")


def test_a_job_that_breaks_reports_failed_and_leaves_nothing(env, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("a bug")

    monkeypatch.setattr(dump_module.DumpScript, "post", broken)
    target = tmp_path / "out.sql"
    target.write_text("old")
    e = env()
    events = e.run(target)
    assert (outcome(events).outcome, error(events).message) == ("failed", "Internal error: a bug")
    assert target.read_text() == "old" and [p.name for p in tmp_path.iterdir()] == ["out.sql"]
    assert e.conn.runs[-1] == "snapshot off"


def test_a_cancel_before_the_first_setting_is_sent_is_not_started(env, tmp_path):
    conn = DumpFake()
    reached, go = conn.hold_session = (threading.Event(), threading.Event())
    e = env(conn)
    e.submit(tmp_path / "out.sql")
    assert reached.wait(5)
    e.worker.cancel("d")  # nothing in the driver: the job's next statement sees it
    go.set()
    done = outcome(request_events(e.recorder, "d"))
    assert (done.outcome, done.cancelled) == ("not_started", True) and conn.runs == []
    assert list(tmp_path.iterdir()) == []


def test_the_file_reaches_the_disk_before_it_replaces_the_old_one(env, tmp_path, monkeypatch):  # pre-flight L9
    from dbaguy_backend.storage import atomic_file

    order: list[str] = []
    real_fsync, real_replace = atomic_file.os.fsync, atomic_file.os.replace
    monkeypatch.setattr(atomic_file.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(atomic_file.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1])
    assert outcome(env().run(tmp_path / "out.sql")).outcome == "completed"
    assert order == ["fsync", "replace"]


def test_an_error_after_a_short_chunk_fails_the_dump(env, tmp_path):  # Task 9 fix H1
    """SQL Server gives the rows before an error later in the set, then the error on the next fetch: the dump reads
    until `[]`, so it fails instead of writing the cut table as complete."""
    target = tmp_path / "out.sql"
    target.write_text("old")
    late = StatementError("Divide by zero error encountered.", code="8134")
    e = env(DumpFake(rows=3000, fails_at=2499, late_error=late), Dialect.TSQL, Engine.MSSQL)
    events = e.run(target)
    assert (outcome(events).outcome, error(events).code, error(events).message) == (
        "failed", "8134", "Divide by zero error encountered.")
    assert target.read_text() == "old" and [p.name for p in tmp_path.iterdir()] == ["out.sql"]


def test_text_the_driver_cannot_decode_names_its_table_and_column(env, tmp_path):  # Task 9 fix L2
    from dbaguy_backend.adapters.base import InvalidText

    events = env(DumpFake(rows=3000, fails_at=10, late_error=InvalidText("v"))).run(tmp_path / "out.sql")
    assert (outcome(events).outcome, error(events).code, error(events).message) == (
        "failed", "invalid_text", "t.v holds text that is not valid UTF-8, which a UTF-8 file cannot hold. Nothing "
                                  "was written")
    events = env(DumpFake(catalog_error=InvalidText("sql"))).run(tmp_path / "out.sql")
    assert error(events).message == ("A name or definition in this database holds text that is not valid UTF-8, which "
                                     "a UTF-8 file cannot hold. Nothing was written")
    assert list(tmp_path.iterdir()) == []

