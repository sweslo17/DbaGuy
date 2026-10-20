import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from loguru import logger

from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ...domain.edits import EditTarget
from ...domain.errors import ConnectionFailed, ValidationFailed
from ...domain.execution import ColumnCategory, ColumnMeta, ServerMessage
from ...domain.metadata import ObjectDetail, ObjectNode, PathSegment, ServerInfo
from ...domain.profiles import AnyProfile, Engine, SqliteProfile, SqliteProfileDraft
from ...domain.schema import SchemaSnapshot
from ...sql.dialects import Dialect
from ...sql.lexer import first_keyword
from ..base import (
    AdapterCapabilities,
    DatabaseAdapter,
    DatabaseConnection,
    DataTools,
    Dumping,
    Endpoint,
    ParsedUrl,
    RowEditing,
    RunOptions,
    StatementCancelled,
    StatementError,
    StatementResult,
    transaction_open_error,
)
from ..cancel_window import CancelWindow
from ..columns import TableColumn
from ..dump import DumpCatalog, DumpSession
from . import columns, dump, editing, introspection
from . import schema as sqlite_schema

_PEEK_ROWS = 200
# sqlite3 keeps this many compiled statements per connection (128 by default). Run SQL file's multi-row INSERTs
# never repeat, and 128 compiled 1,000-row INSERTs held 300 MB (⑥ A10): statements are compiled each time instead
_STATEMENT_CACHE = 0
_PROGRESS_OPS = 10_000  # VM instructions between two looks at the cancel flag: ~0.3% slower, stops within ~0.1 ms
# Statements that control the transaction themselves or cannot run inside one: manual mode does not BEGIN for them.
_NO_AUTO_BEGIN = frozenset(
    {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE", "VACUUM", "PRAGMA", "ATTACH", "DETACH"}
)


def _translate(exc: sqlite3.Error) -> StatementError:
    message = str(exc)
    if isinstance(exc, sqlite3.OperationalError) and message == "interrupted":
        return StatementCancelled()
    return StatementError(message, code=getattr(exc, "sqlite_errorname", None))


def _category(value: Any) -> tuple[str, ColumnCategory]:
    if isinstance(value, (int, float)):
        return ("INTEGER" if isinstance(value, int) else "REAL"), ColumnCategory.NUMBER
    if isinstance(value, bytes):
        return "BLOB", ColumnCategory.BINARY
    if isinstance(value, str):
        return "TEXT", ColumnCategory.TEXT
    return "", ColumnCategory.OTHER


class _CancelGate:
    """Makes a cancel reliable. `sqlite3_interrupt()` only stops a statement that is already running: when a statement
    starts while none is, SQLite clears the flag, so an interrupt that lands between `execute()` being entered (while
    the statement is parsed, and the worker thread gives up the GIL) and the first step is lost, and a statement that
    never ends then never stops. A cancel that arrives while the worker thread is inside a driver call (`call()`) also
    raises a flag that SQLite's progress handler turns into an interrupt as soon as the statement runs. The armed window
    before run() (`arm()`, Task 6b) counts like a call: a cancel in it is kept and `statement()` raises before anything
    runs. A cancel that arrives outside any call and outside the window leaves no flag behind, so it can never cancel a
    later statement."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self._in_call = False
        self._requested = False
        self._window = CancelWindow("SQLite")
        conn.set_progress_handler(self._interrupt_if_requested, _PROGRESS_OPS)

    def _interrupt_if_requested(self) -> int:  # called by SQLite on the worker thread, inside a step
        return 1 if self._requested else 0

    def arm(self) -> None:
        """Worker thread, before its last cancel check (`DatabaseConnection.arm_cancel`)."""
        self._window.arm()

    def disarm(self) -> None:
        """Worker thread; idempotent."""
        self._window.disarm()

    @contextmanager
    def statement(self) -> Iterator[None]:
        """run()'s call: once inside it (a cancel from here raises the flag), take the statement over from the armed
        window, raising StatementCancelled when a cancel was kept there; nothing has run yet."""
        with self.call():
            self._window.take_over()
            yield

    @contextmanager
    def call(self) -> Iterator[None]:
        """Around every driver call that can run a statement (`execute`, `fetchmany`); worker thread only."""
        with self._lock:
            self._in_call = True
        try:
            yield
        finally:
            with self._lock:
                self._in_call = False
                self._requested = False

    def cancel(self) -> None:
        """Any thread."""
        if self._window.keep():
            return  # armed: statement() raises before anything runs
        with self._lock:
            if self._in_call:
                self._requested = True
            self._conn.interrupt()  # also stops what the progress handler cannot reach, e.g. a long sort


class SqliteStatementResult(StatementResult):
    def __init__(self, cursor: sqlite3.Cursor, gate: _CancelGate, *, peek: bool = True) -> None:
        """Called by `run()` inside `gate.call()`, right after `execute()`, so reading the first rows is covered too.
        `peek` False (RunOptions.describe False: nobody is shown the columns' types): no rows are read ahead for them
        (a dump sizes its reads by the rows' size, 06a pre-flight M12)."""
        self._cursor = cursor
        self._gate = gate
        self._buffer: list[tuple[Any, ...]] = []
        self._columns: list[ColumnMeta] | None = None
        self._closed = False
        if cursor.description is not None:
            try:
                if peek:
                    self._buffer = cursor.fetchmany(_PEEK_ROWS)
            except sqlite3.Error as exc:
                raise _translate(exc) from exc
            self._columns = [self._column(i, d[0]) for i, d in enumerate(cursor.description)]

    def _column(self, index: int, name: str) -> ColumnMeta:
        sample = next((row[index] for row in self._buffer if row[index] is not None), None)
        type_name, category = _category(sample)
        return ColumnMeta(name=name, type_name=type_name, category=category)

    def columns(self) -> list[ColumnMeta] | None:
        return self._columns

    def fetch(self, n: int) -> list[list[Any]]:
        if self._columns is None:
            return []
        rows = self._buffer[:n]
        self._buffer = self._buffer[n:]
        if len(rows) < n and not self._closed:
            try:
                with self._gate.call():
                    rows.extend(self._cursor.fetchmany(n - len(rows)))
            except sqlite3.Error as exc:
                raise _translate(exc) from exc
        return [list(r) for r in rows]

    def affected_rows(self) -> int | None:
        if self._columns is None and self._cursor.rowcount >= 0:
            return self._cursor.rowcount
        return None

    def next_result(self) -> bool:
        return False

    def take_messages(self) -> list[ServerMessage]:
        return []

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._buffer = []
            self._cursor.close()


class SqliteConnection(DatabaseConnection, RowEditing, DataTools, Dumping):
    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn  # isolation_level=None: the driver never begins a transaction on its own
        self._path = path
        self._manual = False  # manual-commit mode: run() begins a transaction in front of every statement
        self._gate = _CancelGate(conn)

    def server_info(self) -> ServerInfo:
        return ServerInfo(engine=Engine.SQLITE, version=sqlite3.sqlite_version, current_database="main")

    def current_database(self) -> str | None:
        return "main"

    def list_children(self, path: list[PathSegment]) -> list[ObjectNode]:
        try:
            return introspection.list_children(self._conn, path)
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

    def object_detail(self, path: list[PathSegment]) -> ObjectDetail:
        try:
            return introspection.object_detail(self._conn, path)
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

    def edit_target(self, path: list[PathSegment]) -> EditTarget:
        try:
            return editing.edit_target(self._conn, path)
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

    def table_columns(self, path: list[PathSegment]) -> list[TableColumn]:
        try:
            return columns.table_columns(self._conn, path)
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

    def dump_session(self, scope: DumpScope) -> DumpSession:
        return dump.SESSION

    def dump_catalog(self, scope: DumpScope, kinds: DumpObjectKinds, *, ddl: bool = True) -> DumpCatalog:
        try:
            return dump.dump_catalog(self._conn, scope, kinds, ddl=ddl)
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

    def schema_snapshot(self) -> SchemaSnapshot:
        try:
            return sqlite_schema.build_snapshot(self._conn)
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

    def _begin_if_manual(self, sql: str) -> None:
        """Manual mode: open the transaction the statement runs in, as psycopg does for PostgreSQL.

        The driver's own implicit BEGIN only recognises INSERT/UPDATE/DELETE/REPLACE, so a CTE-prefixed write or DDL
        would otherwise autocommit and escape rollback().
        """
        if not self._manual or self._conn.in_transaction or first_keyword(sql, Dialect.SQLITE) in _NO_AUTO_BEGIN:
            return
        try:
            self._conn.execute("BEGIN")
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

    def run(self, sql: str, options: RunOptions | None = None) -> StatementResult:
        with self._gate.statement():  # from here a cancel sticks until the statement runs (see _CancelGate)
            self._begin_if_manual(sql)
            cursor = self._conn.cursor()
            try:
                cursor.execute(sql)
                return SqliteStatementResult(cursor, self._gate, peek=options is None or options.describe)
            except sqlite3.Error as exc:
                cursor.close()
                raise _translate(exc) from exc

    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    def autocommit(self) -> bool:
        return not self._manual

    def set_autocommit(self, enabled: bool) -> None:
        if self.autocommit() == enabled:
            return
        if self._conn.in_transaction:
            raise transaction_open_error()
        self._manual = not enabled
        logger.info("Auto-commit {} on SQLite database {}", "on" if enabled else "off", self._path)

    def commit(self) -> None:
        try:
            self._conn.commit()
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

    def rollback(self) -> None:
        try:
            self._conn.rollback()
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

    def arm_cancel(self) -> None:
        self._gate.arm()

    def disarm_cancel(self) -> None:
        self._gate.disarm()

    def cancel(self) -> None:
        self._gate.cancel()

    def close(self) -> None:
        self._conn.close()
        logger.info("Closed SQLite database {}", self._path)


class SqliteAdapter(DatabaseAdapter):
    engine = Engine.SQLITE
    url_schemes = ("sqlite",)
    default_port = None
    dialect = Dialect.SQLITE
    capabilities = AdapterCapabilities(row_editing=True, run_file=True, dump=True)

    def parse_url(self, url: str) -> ParsedUrl:
        """`sqlite:///abs/path` and `sqlite:////abs/path` (SQLAlchemy's absolute form) are absolute paths, `sqlite://rel`
        and `sqlite:rel` relative ones; `~` is expanded when the file is opened."""
        parsed = urlparse(url)
        path = unquote(parsed.netloc + parsed.path)
        if path.startswith("//"):  # one root, however many slashes: `//abs` would read as a URI authority later
            path = "/" + path.lstrip("/")
        if not path:
            raise ValidationFailed("A SQLite URL needs a file path, e.g. sqlite:///Users/me/app.db")
        name = Path(path).name or "SQLite"
        return ParsedUrl(profile=SqliteProfileDraft(engine="sqlite", name=name, path=path))

    def open(
        self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None, database: str | None
    ) -> DatabaseConnection:
        if not isinstance(profile, SqliteProfile):
            raise ValidationFailed("Not a SQLite profile")
        path = Path(profile.path).expanduser()
        if not path.is_file():
            raise ConnectionFailed(f"SQLite file not found: {path}")
        mode = "ro" if profile.read_only else "rw"
        uri = f"file:{quote(str(path))}?mode={mode}"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=True,
                                   cached_statements=_STATEMENT_CACHE)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()  # fails for non-database files
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise ConnectionFailed(f"Cannot open {path}: {exc}") from exc
        logger.info("Opened SQLite database {} (mode={})", path, mode)
        return SqliteConnection(conn, path)
