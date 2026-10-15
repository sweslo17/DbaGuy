import itertools
import os
import re
from collections import deque
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

import psycopg
from loguru import logger
from psycopg import postgres, pq

from ...domain.errors import ConnectionFailed, ValidationFailed
from ...domain.execution import ColumnMeta, ErrorPosition, MessageSeverity, ServerMessage
from ...domain.metadata import ObjectDetail, ObjectNode, PathSegment, ServerInfo
from ...domain.profiles import AnyProfile, Engine, ServerProfile, ServerProfileDraft, SslConfig
from ..base import (
    AdapterCapabilities,
    DatabaseAdapter,
    DatabaseConnection,
    Endpoint,
    ParsedUrl,
    StatementCancelled,
    StatementError,
    StatementResult,
)
from ..positions import offset_to_line_col
from ..sql_text import first_keyword
from . import ddl, introspection
from .loaders import category_for_oid, configure_user_loaders

_ROW_KEYWORDS = frozenset({"select", "with", "values", "table", "show", "explain", "fetch", "("})
_RETURNING = re.compile(r"\breturning\b", re.IGNORECASE)
_STREAM_CHUNK = 200
# Statements DECLARE CURSOR accepts, judged conservatively: a wrong "no" only means the result is read in full,
# a wrong "yes" would make DECLARE fail and abort the user's transaction.
_DECLARABLE_KEYWORDS = frozenset({"select", "values", "table", "(", "with"})
# Data-modifying statements and SELECT ... INTO, which DECLARE rejects. Matched anywhere (literals and comments too).
_NOT_DECLARABLE = re.compile(r"\b(?:insert|update|delete|merge|into)\b", re.IGNORECASE)
_CURSOR_PREFIX = "dbaguy_cursor_"
# COPY through the client needs psycopg's copy() API; plain execute() leaves the connection stuck in COPY mode.
_COPY_CLIENT_STREAM = re.compile(r"\b(?:stdin|stdout)\b", re.IGNORECASE)
_UNSUPPORTED_COPY = "COPY FROM STDIN / TO STDOUT is not supported in the console yet"
_SSL_QUERY_KEYS = {"sslmode": "mode", "sslrootcert": "ca_file", "sslcert": "cert_file", "sslkey": "key_file"}
_URL_CONNECTION_KEYS = ("user", "password", "host", "port", "dbname")  # query-string spellings of URL parts
# libpq keys that pick the target or carry secrets: they come from the profile fields and the keychain,
# never from free-form `options` (they could redirect the connection or leak a password into profiles.json).
_RESERVED_OPTION_KEYS = frozenset(
    {*_URL_CONNECTION_KEYS, "hostaddr", "service", "passfile", "sslpassword"}
)
_DEFAULT_CONNECT_PARAMS: dict[str, Any] = {
    "connect_timeout": 10,
    "application_name": "DbaGuy",
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}
_CONNECTION_LOST = "connection_lost"
# SQLSTATEs that mean the server dropped us: connection exceptions and admin/crash shutdown.
_CONNECTION_LOST_STATES = frozenset({"57P01", "57P02", "57P03"})

# libpq's chunked streaming uses the extended protocol, which accepts one statement per query.
_MULTI_STATEMENT_ERROR = "cannot insert multiple commands into a prepared statement"

_CUSTOM_TYPES_SQL = """
SELECT t.oid, format_type(t.oid, NULL)
FROM pg_type t
WHERE t.typnamespace NOT IN ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
  AND t.typtype IN ('b', 'c', 'd', 'e', 'm', 'r')
  AND (t.typtype <> 'c' OR (SELECT c.relkind FROM pg_class c WHERE c.oid = t.typrelid) = 'c')
LIMIT 20000
"""


def _is_connection_lost(exc: psycopg.Error, conn: psycopg.Connection | None) -> bool:
    """True when the connection is gone: psycopg knows it is closed/broken, or the server said it is dropping us.

    A missing SQLSTATE alone proves nothing: client-side errors such as "another command is already in progress"
    have none either, and the connection is still alive.
    """
    if conn is not None and (conn.closed or conn.broken):
        return True
    sqlstate = exc.diag.sqlstate
    return sqlstate is not None and (sqlstate.startswith("08") or sqlstate in _CONNECTION_LOST_STATES)


def _uses_client_copy(sql: str) -> bool:
    """A statement starting with COPY that reads STDIN or writes STDOUT, anywhere in the script."""
    return any(first_keyword(part) == "copy" and _COPY_CLIENT_STREAM.search(part) for part in sql.split(";"))


def translate_error(
    exc: psycopg.Error, sql: str, conn: psycopg.Connection | None = None, offset: int = 0
) -> StatementError:
    """`conn` tells a dropped connection from a live one; `offset`: characters the driver sent in front of `sql`
    (a DECLARE prefix), removed from the error position."""
    diag = exc.diag
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return StatementCancelled(diag.message_primary or "Query cancelled")
    if _is_connection_lost(exc, conn):
        reason = (diag.message_primary or str(exc)).strip()
        return StatementError(f"Connection to the server was lost: {reason}", code=_CONNECTION_LOST)
    message = diag.message_primary or str(exc).strip()
    if diag.message_detail:
        message += f"\nDETAIL: {diag.message_detail}"
    if diag.message_hint:
        message += f"\nHINT: {diag.message_hint}"
    position = None
    if diag.statement_position and int(diag.statement_position) > offset:
        line, column = offset_to_line_col(sql, int(diag.statement_position) - offset)
        position = ErrorPosition(line=line, column=column)
    return StatementError(message, code=diag.sqlstate, position=position)


def _is_multi_statement_rejection(exc: StatementError) -> bool:
    """The server refused a script at parse time, so nothing was executed."""
    return exc.code == "42601" and _MULTI_STATEMENT_ERROR in exc.message


def _likely_returns_rows(sql: str) -> bool:
    return first_keyword(sql) in _ROW_KEYWORDS or bool(_RETURNING.search(sql))


def _single_statement(sql: str) -> bool:
    """No ';' before the end. Literals are not parsed, so a ';' inside one errs on the safe (buffered) side."""
    return ";" not in sql.rstrip().rstrip(";")


def _declarable(sql: str) -> bool:
    return first_keyword(sql) in _DECLARABLE_KEYWORDS and not _NOT_DECLARABLE.search(sql) and _single_statement(sql)


def _without_reserved_options(options: Mapping[str, str]) -> dict[str, str]:
    kept = {key: value for key, value in options.items() if key.lower() not in _RESERVED_OPTION_KEYS}
    if len(kept) != len(options):
        ignored = ", ".join(sorted(set(options) - set(kept)))
        logger.warning("Ignoring reserved PostgreSQL connection options: {}", ignored)  # names only, never values
    return kept


def _port_from_query(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise ValidationFailed(f"Invalid port '{value}' in the PostgreSQL URL") from None


def _ssl_params(ssl: SslConfig) -> dict[str, str]:
    params = {"sslmode": ssl.mode}
    if ssl.ca_file:
        params["sslrootcert"] = os.path.expanduser(ssl.ca_file)
    elif ssl.mode in ("verify-ca", "verify-full"):
        params["sslrootcert"] = "system"  # libpq 16+: use the system trust store
    if ssl.cert_file:
        params["sslcert"] = os.path.expanduser(ssl.cert_file)
    if ssl.key_file:
        params["sslkey"] = os.path.expanduser(ssl.key_file)
    return params


class _BufferedResult(StatementResult):
    """Result of cursor.execute(): small results, DML/DDL, multi-statement scripts."""

    def __init__(self, owner: "PostgresConnection", cursor: psycopg.Cursor, sql: str) -> None:
        self._owner, self._cursor, self._sql = owner, cursor, sql

    def columns(self) -> list[ColumnMeta] | None:
        desc = self._cursor.description
        return self._owner.column_meta(desc) if desc else None

    def fetch(self, n: int) -> list[list[Any]]:
        if self._cursor.description is None:
            return []
        try:
            return [list(r) for r in self._cursor.fetchmany(n)]
        except psycopg.Error as exc:
            raise self._owner.translate_error(exc, self._sql) from exc

    def affected_rows(self) -> int | None:
        if self._cursor.description is None and self._cursor.rowcount >= 0:
            return self._cursor.rowcount
        return None

    def next_result(self) -> bool:
        return bool(self._cursor.nextset())

    def take_messages(self) -> list[ServerMessage]:
        return self._owner.drain_messages()

    def close(self) -> None:
        self._cursor.close()


class _StreamResult(StatementResult):
    """Row-returning statement streamed with bounded memory (libpq chunked mode)."""

    def __init__(self, owner: "PostgresConnection", cursor: psycopg.Cursor, sql: str) -> None:
        self._owner, self._cursor, self._sql = owner, cursor, sql
        self._gen = cursor.stream(sql, size=_STREAM_CHUNK)
        self._pending: list[Any] = []
        self._done = False
        self._closed = False
        self._columns: list[ColumnMeta] | None = None
        try:
            first = next(self._gen, None)
        except psycopg.ProgrammingError as exc:
            if "didn't produce a result" not in str(exc):
                self.close()
                raise owner.translate_error(exc, sql) from exc
            first = None  # e.g. SELECT ... INTO: ran fine, returns no rows
            self._done = True
            return
        except psycopg.Error as exc:
            self.close()
            raise owner.translate_error(exc, sql) from exc
        if first is None:
            self._done = True
            self._columns = owner.describe(sql)
        else:
            self._pending.append(first)
            self._columns = owner.column_meta(cursor.description)

    def columns(self) -> list[ColumnMeta] | None:
        return self._columns

    def fetch(self, n: int) -> list[list[Any]]:
        rows = self._pending[:n]
        self._pending = self._pending[n:]
        while len(rows) < n and not self._done:
            try:
                rows.append(next(self._gen))
            except StopIteration:
                self._done = True
            except psycopg.Error as exc:
                self._done = True
                raise self._owner.translate_error(exc, self._sql) from exc
        return [list(r) for r in rows]

    def affected_rows(self) -> int | None:
        return None

    def next_result(self) -> bool:
        return False

    def take_messages(self) -> list[ServerMessage]:
        return self._owner.drain_messages()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending = []
        try:
            self._gen.close()  # psycopg cancels and drains the rest
        except psycopg.Error as exc:
            logger.warning("Error while closing PostgreSQL stream: {}", exc)
        self._cursor.close()


class _CursorResult(StatementResult):
    """Row-returning statement inside the user's transaction, read through a server-side cursor (DECLARE/FETCH).

    Abandoning a stream makes psycopg cancel the statement, which aborts the surrounding transaction (COMMIT then
    silently rolls back). close() runs CLOSE instead, which leaves the transaction exactly as it was.
    """

    def __init__(self, owner: "PostgresConnection", cursor: psycopg.ServerCursor, sql: str) -> None:
        self._owner, self._cursor, self._sql = owner, cursor, sql
        self._pending: list[Any] = []
        self._done = False
        self._closed = False
        try:
            cursor.execute(sql)  # DECLARE <name> CURSOR FOR <sql>, then describes the portal
        except psycopg.Error as exc:
            self.close()
            # psycopg prefixes the statement with `DECLARE "<name>" CURSOR FOR `; error positions must not count it.
            raise owner.translate_error(exc, sql, offset=len(f'DECLARE "{cursor.name}" CURSOR FOR ')) from exc
        desc = cursor.description
        self._columns = owner.column_meta(desc) if desc else None

    def columns(self) -> list[ColumnMeta] | None:
        return self._columns

    def fetch(self, n: int) -> list[list[Any]]:
        while len(self._pending) < n and not self._done and self._columns is not None:
            want = max(n - len(self._pending), _STREAM_CHUNK)  # read ahead: one FETCH per page, not per call
            try:
                page = self._cursor.fetchmany(want)
            except psycopg.Error as exc:
                self._done = True
                raise self._owner.translate_error(exc, self._sql) from exc
            self._pending += page
            self._done = len(page) < want
        rows, self._pending = self._pending[:n], self._pending[n:]
        return [list(r) for r in rows]

    def affected_rows(self) -> int | None:
        return None

    def next_result(self) -> bool:
        return False

    def take_messages(self) -> list[ServerMessage]:
        return self._owner.drain_messages()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending = []
        try:
            self._cursor.close()  # CLOSE <name>; psycopg skips it once the transaction has failed or ended
        except psycopg.Error as exc:
            logger.warning("Error while closing PostgreSQL cursor: {}", exc)


class PostgresConnection(DatabaseConnection):
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        self._messages: deque[ServerMessage] = deque(maxlen=1000)
        self._cursor_ids = itertools.count(1)
        self._custom_types: dict[int, str] = {int(oid): name for oid, name in conn.execute(_CUSTOM_TYPES_SQL)}
        conn.add_notice_handler(self._on_notice)

    def _on_notice(self, diag: psycopg.errors.Diagnostic) -> None:
        severity = MessageSeverity.WARNING if diag.severity_nonlocalized == "WARNING" else MessageSeverity.INFO
        self._messages.append(ServerMessage(severity=severity, text=diag.message_primary or ""))

    def translate_error(self, exc: psycopg.Error, sql: str, offset: int = 0) -> StatementError:
        return translate_error(exc, sql, self._conn, offset)

    def drain_messages(self) -> list[ServerMessage]:
        messages = list(self._messages)
        self._messages.clear()
        return messages

    def _type_name(self, oid: int, display: str | None = None) -> str:
        if oid in self._custom_types:
            return self._custom_types[oid]
        if display and not display.isdigit():
            return display
        info = postgres.types.get(oid)
        return info.name if info else f"oid {oid}"

    def column_meta(self, description: list[psycopg.Column]) -> list[ColumnMeta]:
        return [
            ColumnMeta(
                name=col.name,
                type_name=self._type_name(col.type_code, getattr(col, "type_display", None)),
                category=category_for_oid(col.type_code),
            )
            for col in description
        ]

    def describe(self, sql: str) -> list[ColumnMeta] | None:
        """Column metadata of a statement without executing it (used for zero-row results)."""
        try:
            pgconn = self._conn.pgconn
            encoding = self._conn.info.encoding
            prepared = pgconn.prepare(b"", sql.encode(encoding))
            if prepared.status != pq.ExecStatus.COMMAND_OK:
                return None
            desc = pgconn.describe_prepared(b"")
            if desc.status != pq.ExecStatus.COMMAND_OK or desc.nfields == 0:
                return None
            return [
                ColumnMeta(
                    name=desc.fname(i).decode(encoding),
                    type_name=self._type_name(desc.ftype(i)),
                    category=category_for_oid(desc.ftype(i)),
                )
                for i in range(desc.nfields)
            ]
        except Exception as exc:  # describing is best-effort
            logger.debug("Could not describe statement: {}", exc)
            return None

    def server_info(self) -> ServerInfo:
        version, database, user = self._conn.execute(
            "SELECT current_setting('server_version'), current_database(), current_user"
        ).fetchone()
        return ServerInfo(engine=Engine.POSTGRES, version=f"PostgreSQL {version}", current_database=database, current_user=user)

    def current_database(self) -> str | None:
        return self._conn.info.dbname

    def _check_database(self, path: list[PathSegment]) -> None:
        if path and path[0].name != self.current_database():
            raise ValidationFailed(f"Connection is bound to database '{self.current_database()}', not '{path[0].name}'")

    def list_children(self, path: list[PathSegment]) -> list[ObjectNode]:
        self._check_database(path)
        return introspection.list_children(self._conn, path)

    def object_detail(self, path: list[PathSegment]) -> ObjectDetail:
        self._check_database(path)
        return ddl.object_detail(self._conn, path)

    def _user_cursor(self) -> psycopg.Cursor:
        cursor = self._conn.cursor()
        configure_user_loaders(cursor.adapters)
        return cursor

    def _server_cursor(self) -> psycopg.ServerCursor:
        cursor = self._conn.cursor(name=f"{_CURSOR_PREFIX}{next(self._cursor_ids)}")  # unique per connection
        configure_user_loaders(cursor.adapters)
        return cursor

    def _run_buffered(self, sql: str) -> StatementResult:
        cursor = self._user_cursor()
        try:
            cursor.execute(sql)
        except psycopg.Error:
            cursor.close()
            raise
        return _BufferedResult(self, cursor, sql)

    def _run_streamed(self, sql: str) -> StatementResult:
        try:
            return _StreamResult(self, self._user_cursor(), sql)  # closes its cursor when it fails
        except StatementError as exc:
            if not _is_multi_statement_rejection(exc):
                raise
            logger.debug("Script has several statements and cannot be streamed; running it buffered")
        return self._run_buffered(sql)

    def run(self, sql: str) -> StatementResult:
        if _uses_client_copy(sql):
            raise StatementError(_UNSUPPORTED_COPY, code="unsupported_copy")
        try:
            if _likely_returns_rows(sql):
                status = self._conn.info.transaction_status
                if status == pq.TransactionStatus.IDLE:
                    return self._run_streamed(sql)  # autocommit: cancelling an abandoned stream is harmless
                if status == pq.TransactionStatus.INTRANS and _declarable(sql):
                    return _CursorResult(self, self._server_cursor(), sql)
                # Anything else inside a transaction (SHOW, EXPLAIN, RETURNING, scripts…) is read in full: abandoning
                # a stream would cancel it and abort the user's transaction.
            return self._run_buffered(sql)
        except psycopg.Error as exc:  # e.g. creating a cursor on a dead connection
            raise self.translate_error(exc, sql) from exc

    def in_transaction(self) -> bool:
        return self._conn.info.transaction_status in (pq.TransactionStatus.INTRANS, pq.TransactionStatus.INERROR)

    def transaction_failed(self) -> bool:
        return self._conn.info.transaction_status == pq.TransactionStatus.INERROR

    def cancel(self) -> None:
        try:
            self._conn.cancel_safe(timeout=3.0)
        except Exception as exc:
            logger.warning("PostgreSQL cancel failed: {}", exc)

    def close(self) -> None:
        self._conn.close()


class PostgresAdapter(DatabaseAdapter):
    engine = Engine.POSTGRES
    url_schemes = ("postgres", "postgresql")
    default_port = 5432
    capabilities = AdapterCapabilities(per_database_connection=True)

    def parse_url(self, url: str) -> ParsedUrl:
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query))
        ssl_fields = {_SSL_QUERY_KEYS[k]: query.pop(k) for k in list(query) if k in _SSL_QUERY_KEYS}
        # Query-string spellings of the URL parts fill in what the URL itself leaves empty.
        part = {key: query.pop(key, None) or None for key in _URL_CONNECTION_KEYS}
        host = parsed.hostname or part["host"]
        if not host:
            raise ValidationFailed("A PostgreSQL URL needs a host, e.g. postgres://user@host:5432/db")
        database = unquote(parsed.path.lstrip("/")) or part["dbname"]
        username = unquote(parsed.username) if parsed.username else part["user"]
        password = unquote(parsed.password) if parsed.password else part["password"]
        port = parsed.port or (_port_from_query(part["port"]) if part["port"] else None)
        draft = ServerProfileDraft(
            engine="postgres",
            name=f"{host}/{database}" if database else host,
            host=host,
            port=port,
            database=database,
            username=username,
            ssl=SslConfig(**ssl_fields),
            options=_without_reserved_options(query),
        )
        return ParsedUrl(profile=draft, password=password)

    def open(
        self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None, database: str | None
    ) -> DatabaseConnection:
        if not isinstance(profile, ServerProfile) or endpoint is None:
            raise ValidationFailed("Not a PostgreSQL server profile")
        # Free-form options only override the defaults; everything that picks the target comes last and wins.
        params: dict[str, Any] = {
            **_DEFAULT_CONNECT_PARAMS,
            **_without_reserved_options(profile.options),
            **_ssl_params(profile.ssl),
            "host": endpoint.tls_server_name or endpoint.host,
            "port": endpoint.port,
            "dbname": database or profile.database or "postgres",
            "user": profile.username,
            "password": password,
        }
        if endpoint.tls_server_name:
            params["hostaddr"] = endpoint.host  # tunnelled: connect locally, verify TLS against the real host
        params = {k: v for k, v in params.items() if v not in (None, "")}
        try:
            conn = psycopg.connect(autocommit=True, **params)
        except psycopg.Error as exc:
            raise ConnectionFailed(str(exc).strip()) from exc
        logger.info("Connected to PostgreSQL {}:{} db={}", params["host"], endpoint.port, params["dbname"])
        return PostgresConnection(conn)
