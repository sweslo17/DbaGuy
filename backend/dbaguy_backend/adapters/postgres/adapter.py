import os
import re
from collections import deque
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
from ..values import encode_row
from .loaders import category_for_oid, configure_user_loaders

_ROW_KEYWORDS = frozenset({"select", "with", "values", "table", "show", "explain", "fetch", "("})
_RETURNING = re.compile(r"\breturning\b", re.IGNORECASE)
_STREAM_CHUNK = 200
_SSL_QUERY_KEYS = {"sslmode": "mode", "sslrootcert": "ca_file", "sslcert": "cert_file", "sslkey": "key_file"}

_CUSTOM_TYPES_SQL = """
SELECT t.oid, format_type(t.oid, NULL)
FROM pg_type t
WHERE t.typnamespace NOT IN ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
  AND t.typtype IN ('b', 'c', 'd', 'e', 'm', 'r')
  AND (t.typtype <> 'c' OR (SELECT c.relkind FROM pg_class c WHERE c.oid = t.typrelid) = 'c')
LIMIT 20000
"""


def translate_error(exc: psycopg.Error, sql: str) -> StatementError:
    diag = exc.diag
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return StatementCancelled(diag.message_primary or "Query cancelled")
    message = diag.message_primary or str(exc).strip()
    if diag.message_detail:
        message += f"\nDETAIL: {diag.message_detail}"
    if diag.message_hint:
        message += f"\nHINT: {diag.message_hint}"
    position = None
    if diag.statement_position:
        line, column = offset_to_line_col(sql, int(diag.statement_position))
        position = ErrorPosition(line=line, column=column)
    return StatementError(message, code=diag.sqlstate, position=position)


def _likely_returns_rows(sql: str) -> bool:
    return first_keyword(sql) in _ROW_KEYWORDS or bool(_RETURNING.search(sql))


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
            return [encode_row(r) for r in self._cursor.fetchmany(n)]
        except psycopg.Error as exc:
            raise translate_error(exc, self._sql) from exc

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
                raise translate_error(exc, sql) from exc
            first = None  # e.g. SELECT ... INTO: ran fine, returns no rows
            self._done = True
            return
        except psycopg.Error as exc:
            self.close()
            raise translate_error(exc, sql) from exc
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
                raise translate_error(exc, self._sql) from exc
        return [encode_row(r) for r in rows]

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


class PostgresConnection(DatabaseConnection):
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        self._messages: deque[ServerMessage] = deque(maxlen=1000)
        self._custom_types: dict[int, str] = {int(oid): name for oid, name in conn.execute(_CUSTOM_TYPES_SQL)}
        conn.add_notice_handler(self._on_notice)

    def _on_notice(self, diag: psycopg.errors.Diagnostic) -> None:
        severity = MessageSeverity.WARNING if diag.severity_nonlocalized == "WARNING" else MessageSeverity.INFO
        self._messages.append(ServerMessage(severity=severity, text=diag.message_primary or ""))

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

    def list_children(self, path: list[PathSegment]) -> list[ObjectNode]:
        raise NotImplementedError  # Task 8

    def object_detail(self, path: list[PathSegment]) -> ObjectDetail:
        raise NotImplementedError  # Task 8

    def run(self, sql: str) -> StatementResult:
        cursor = self._conn.cursor()
        configure_user_loaders(cursor.adapters)
        if _likely_returns_rows(sql):
            return _StreamResult(self, cursor, sql)
        try:
            cursor.execute(sql)
        except psycopg.Error as exc:
            cursor.close()
            raise translate_error(exc, sql) from exc
        return _BufferedResult(self, cursor, sql)

    def in_transaction(self) -> bool:
        return self._conn.info.transaction_status in (pq.TransactionStatus.INTRANS, pq.TransactionStatus.INERROR)

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
        if not parsed.hostname:
            raise ValidationFailed("A PostgreSQL URL needs a host, e.g. postgres://user@host:5432/db")
        query = dict(parse_qsl(parsed.query))
        ssl_fields = {_SSL_QUERY_KEYS[k]: query.pop(k) for k in list(query) if k in _SSL_QUERY_KEYS}
        database = unquote(parsed.path.lstrip("/")) or None
        draft = ServerProfileDraft(
            engine="postgres",
            name=f"{parsed.hostname}/{database}" if database else parsed.hostname,
            host=parsed.hostname,
            port=parsed.port,
            database=database,
            username=unquote(parsed.username) if parsed.username else None,
            ssl=SslConfig(**ssl_fields),
            options=query,
        )
        return ParsedUrl(profile=draft, password=unquote(parsed.password) if parsed.password else None)

    def open(
        self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None, database: str | None
    ) -> DatabaseConnection:
        if not isinstance(profile, ServerProfile) or endpoint is None:
            raise ValidationFailed("Not a PostgreSQL server profile")
        params: dict[str, Any] = {
            "host": endpoint.tls_server_name or endpoint.host,
            "port": endpoint.port,
            "dbname": database or profile.database or "postgres",
            "user": profile.username,
            "password": password,
            "connect_timeout": 10,
            "application_name": "DbaGuy",
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
            **_ssl_params(profile.ssl),
            **profile.options,
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
