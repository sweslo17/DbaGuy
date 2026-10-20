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
from dbaguy_backend.sessions.jobs.dump import FETCH_CHARS, FIRST_FETCH, FK_CYCLE, DumpJob
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
    StatementCancelled. Every `n` asked for is kept in the connection's `fetches`."""

    def __init__(self, count: int, conn: "DumpFake") -> None:
        super().__init__(0)
        self._next, self._count, self._conn = 0, count, conn

    def fetch(self, n):
        self._conn.fetches.append(n)
        if self._conn.stop_at is not None and self._next >= self._conn.stop_at:
            self._conn.blocked.set()
            assert self._conn._unblock.wait(10), "never cancelled"
            raise StatementCancelled()
        end = min(self._next + n, self._count)
        rows = [[i, self._conn.value(i)] for i in range(self._next, end)]
        self._next = end
        return rows


class DumpFake(FakeConnection, Dumping):
    """A Dumping connection: one table `t` (or `tables`), `rows` rows each; SELECTs are kept in `runs`."""

    def __init__(self, rows: int = 2500, stop_at: int | None = None, fail_select: StatementError | None = None,
                 tables: tuple[DumpTable, ...] = (TABLE,), session: DumpSession | None = None, value=None,
                 **catalog) -> None:
        super().__init__()
        self.rows, self.stop_at, self.fail_select = rows, stop_at, fail_select
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
        self._count = count

    def fetch(self, n):
        return [[self._count]]


class Env:
    def __init__(self, conn: DumpFake, dialect: Dialect, engine: Engine, source: Path | None) -> None:
        self.conn = conn
        lease = FakeLease(conn)
        self.worker = SessionWorker("dump", lambda: lease)
        self.worker.start()
        jobs = JobHooks(build=lambda command: DumpJob(command, "Fake", engine, source_file=source))
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
             source: Path | None = None) -> Env:
        made.append(Env(conn or DumpFake(), dialect, engine, source))
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
    assert (outcome(events).outcome, error(events).message) == ("not_started", "A filter must be a single expression")
    assert e.conn.runs == []


def test_a_cancel_between_chunks_stops_the_dump(env, tmp_path):
    e = env(DumpFake(rows=200_000))
    target = tmp_path / "out.sql"
    e.submit(target)
    e.recorder.wait_for(lambda ev: ev.type == "job_progress" and ev.rows > 0)
    threading.Thread(target=e.worker.cancel, args=("d",)).start()
    done = outcome(request_events(e.recorder, "d"))
    assert (done.outcome, done.cancelled) == ("failed", True) and not target.exists()


def test_large_rows_are_read_a_few_at_a_time(env, tmp_path):  # pre-flight M12
    """40 rows of 2 MiB of text: the first read takes FIRST_FETCH, the next ones about FETCH_CHARS of row text, so
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
    assert e.conn.fetches == [FIRST_FETCH, per_fetch, per_fetch]
    assert peak < 60 << 20, peak  # ≈ 30 MiB of rows + a statement's text; all 40 at once would be 80 MiB and more


def test_a_row_too_long_for_the_server_is_said_once_for_its_table(env, tmp_path):  # pre-flight M11
    e = env(DumpFake(rows=400, value=lambda i: "x" * 5000, max_statement_bytes=4096), Dialect.MYSQL, Engine.MYSQL)
    done = outcome(e.run(tmp_path / "out.sql", scope=DumpScope(kind="schema", schema_name="main")))
    assert done.outcome == "completed" and len(statements(tmp_path / "out.sql", Dialect.MYSQL)) > 6  # 2 INSERTs
    assert done.warnings == ["a warning", "t has a row of 1 MiB: MySQL refuses statements over its max_allowed_packet "
                                          "(0.00390625 MiB on the server dumped) when the file is run"]


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
    assert "INSERT INTO main.t DEFAULT VALUES\nGO 1203\n" in text
    assert [(s.text, s.repeat) for s in split(text, Dialect.TSQL) if "DEFAULT VALUES" in s.text] == [
        ("INSERT INTO main.t DEFAULT VALUES", 1203)]


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
    e = env(DumpFake(rows=2, tables=(child, looped)))
    done = outcome(e.run(tmp_path / "cycle.sql", content="data"))
    assert [o.name for o in done.objects] == ["child", "parent"] and done.warnings == ["a warning", FK_CYCLE]


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
