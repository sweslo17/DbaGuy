import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from loguru import logger

from ...domain.errors import ConnectionFailed, ValidationFailed
from ...domain.execution import ColumnCategory, ColumnMeta, ServerMessage
from ...domain.metadata import ObjectDetail, ObjectNode, PathSegment, ServerInfo
from ...domain.profiles import AnyProfile, Engine, SqliteProfile, SqliteProfileDraft
from ...domain.schema import SchemaSnapshot
from ...sql.dialects import Dialect
from ..base import (
    DatabaseAdapter,
    DatabaseConnection,
    Endpoint,
    ParsedUrl,
    StatementCancelled,
    StatementError,
    StatementResult,
)
from . import introspection
from . import schema as sqlite_schema

_PEEK_ROWS = 200


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


class SqliteStatementResult(StatementResult):
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor
        self._buffer: list[tuple[Any, ...]] = []
        self._columns: list[ColumnMeta] | None = None
        self._closed = False
        if cursor.description is not None:
            try:
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


class SqliteConnection(DatabaseConnection):
    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn
        self._path = path

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

    def schema_snapshot(self) -> SchemaSnapshot:
        try:
            return sqlite_schema.build_snapshot(self._conn)
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

    def run(self, sql: str) -> StatementResult:
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql)
        except sqlite3.Error as exc:
            cursor.close()
            raise _translate(exc) from exc
        return SqliteStatementResult(cursor)

    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    def cancel(self) -> None:
        self._conn.interrupt()

    def close(self) -> None:
        self._conn.close()
        logger.info("Closed SQLite database {}", self._path)


class SqliteAdapter(DatabaseAdapter):
    engine = Engine.SQLITE
    url_schemes = ("sqlite",)
    default_port = None
    dialect = Dialect.SQLITE

    def parse_url(self, url: str) -> ParsedUrl:
        parsed = urlparse(url)
        path = unquote(parsed.netloc + parsed.path)
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
            conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=True)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()  # fails for non-database files
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise ConnectionFailed(f"Cannot open {path}: {exc}") from exc
        logger.info("Opened SQLite database {} (mode={})", path, mode)
        return SqliteConnection(conn, path)
