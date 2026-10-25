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

from ...domain.agent import LoginPrivileges
from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ...domain.edits import EditTarget
from ...domain.errors import ConnectionFailed, ValidationFailed
from ...domain.execution import ColumnMeta, ErrorPosition, MessageSeverity, ServerMessage
from ...domain.metadata import ObjectDetail, ObjectNode, PathSegment, ServerInfo
from ...domain.profiles import AnyProfile, Engine, ServerProfile, ServerProfileDraft, SslConfig
from ...domain.schema import SchemaSnapshot
from ...sql.dialects import Dialect
from ...sql.splitter import split
from ..base import (
    FAILED_COMMIT_MESSAGE,
    AdapterCapabilities,
    DatabaseAdapter,
    DatabaseConnection,
    DataTools,
    Dumping,
    Endpoint,
    ParsedUrl,
    ReferenceReading,
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
from ..positions import offset_to_line_col
from ..references import Reference
from ..sql_text import first_keyword
from . import columns, ddl, dump, editing, introspection, references
from . import schema as pg_schema
from .loaders import category_for_oid, configure_user_loaders

_ROW_KEYWORDS = frozenset({"select", "with", "values", "table", "show", "explain", "fetch", "("})
_RETURNING = re.compile(r"\breturning\b", re.IGNORECASE)
_STREAM_CHUNK = 200
PLAN_LOCK_TIMEOUT_MS = 5_000  # a dump plan's catalog read waits at most this long for a table's lock (Task 9 fix M3)
_LOCK_NOT_AVAILABLE = "55P03"
# The plan reads the catalog of the scope's schemas (sizes included): a lock on any table of one of them stops it, and
# none elsewhere does (probed, fix round 2 N3)
LOCKED_FOR_PLAN = ("A table in a schema this dump reads is locked by another session (for example an uncommitted "
                   "ALTER): try again after it commits")
# Statements DECLARE CURSOR accepts, judged conservatively: a wrong "no" only means the result is read in full,
# a wrong "yes" would make DECLARE fail and abort the user's transaction.
_DECLARABLE_KEYWORDS = frozenset({"select", "values", "table", "(", "with"})
# Data-modifying statements and SELECT ... INTO, which DECLARE rejects. Matched anywhere (literals and comments too).
_NOT_DECLARABLE = re.compile(r"\b(?:insert|update|delete|merge|into)\b", re.IGNORECASE)
_CURSOR_PREFIX = "dbaguy_cursor_"
_COMMIT_KEYWORDS = frozenset({"commit", "end"})
# COPY through the client needs psycopg's copy() API; plain execute() leaves the connection stuck in COPY mode.
_COPY_CLIENT_STREAM = re.compile(r"\b(?:stdin|stdout)\b", re.IGNORECASE)
_UNSUPPORTED_COPY = "COPY FROM STDIN / TO STDOUT is not supported in the console yet"
_SSL_QUERY_KEYS = {"sslmode": "mode", "sslrootcert": "ca_file", "sslcert": "cert_file", "sslkey": "key_file"}
_URL_CONNECTION_KEYS = ("user", "password", "host", "port", "dbname")  # query-string spellings of URL parts
# psycopg's own connect() arguments, not libpq keys: `open()` passes autocommit and prepare_threshold itself, and a
# free-form option of the same name would collide with them (TypeError) or reach psycopg as a string (fix wave F1).
_PSYCOPG_CONNECT_KEYS = ("autocommit", "prepare_threshold", "context", "row_factory", "cursor_factory")
# libpq keys that pick the target or carry secrets: they come from the profile fields and the keychain,
# never from free-form `options` (they could redirect the connection or leak a password into profiles.json); and
# psycopg's own arguments.
_RESERVED_OPTION_KEYS = frozenset(
    {*_URL_CONNECTION_KEYS, *_PSYCOPG_CONNECT_KEYS, "hostaddr", "service", "passfile", "sslpassword"}
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
# Read-only profiles set the session default at connect and re-assert it before every statement: a SELECT can call
# set_config() to switch the default off, whether written out, hidden in a string or inside a user function.
_READ_ONLY_GUC = "default_transaction_read_only"
# CR-08-1: the login's superuser bit and the four roles a read-only transaction does not stop (a superuser has all),
# and REPLICATION (final review I1: a slot it copies or reads outlives the ROLLBACK; the gate refuses those functions)
# A role's file rights also come from a direct EXECUTE grant on the file functions (revoked from PUBLIC by default; a
# grant on pg_read_file, pg_ls_dir and the like reaches the data directory, on lo_import and lo_export any file the
# server can): the gate refuses them all (D-08-2), the privileges say the login holds them.
_FILE_READ_FUNCTIONS = ("lo_import(text)", "pg_read_file(text)", "pg_read_binary_file(text)", "pg_ls_dir(text)",
                        "pg_stat_file(text)")
_FILE_WRITE_FUNCTIONS = ("lo_export(oid, text)",)


def _member_or_may_execute(role: str, functions: tuple[str, ...]) -> str:
    executes = " OR ".join(f"has_function_privilege(current_user, 'pg_catalog.{f}', 'EXECUTE')" for f in functions)
    return f"pg_has_role(current_user, '{role}', 'MEMBER') OR {executes}"


_LOGIN_SQL = (
    f"SELECT r.rolsuper, {_member_or_may_execute('pg_read_server_files', _FILE_READ_FUNCTIONS)}, "
    f"{_member_or_may_execute('pg_write_server_files', _FILE_WRITE_FUNCTIONS)}, "
    "pg_has_role(current_user, 'pg_execute_server_program', 'MEMBER'), "
    "pg_has_role(current_user, 'pg_signal_backend', 'MEMBER'), r.rolreplication "
    "FROM pg_roles r WHERE r.rolname = current_user"
)
_LOGIN_GRANTS = ("superuser", "server_files_read", "server_files_write", "server_programs", "signal_backends",
                 "replication")
_READ_ONLY_SESSION = "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"

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
    """Row-returning statement streamed with bounded memory (libpq chunked mode; single-row mode with `size` 1)."""

    def __init__(self, owner: "PostgresConnection", cursor: psycopg.Cursor, sql: str,
                 size: int = _STREAM_CHUNK) -> None:
        self._owner, self._cursor, self._sql = owner, cursor, sql
        self._gen = cursor.stream(sql, size=size)
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

    def __init__(self, owner: "PostgresConnection", cursor: psycopg.ServerCursor, sql: str, *,
                 read_ahead: bool = True) -> None:
        self._owner, self._cursor, self._sql = owner, cursor, sql
        # False (RunOptions.describe False: a job's read): each FETCH takes only the rows asked for, never a page more
        # (a dump sizes its reads by the rows' size, 06a pre-flight M12)
        self._read_ahead = read_ahead
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
            want = n - len(self._pending)
            if self._read_ahead:
                want = max(want, _STREAM_CHUNK)  # read ahead: one FETCH per page, not per call
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


class PostgresConnection(DatabaseConnection, RowEditing, DataTools, ReferenceReading, Dumping):
    def __init__(self, conn: psycopg.Connection, read_only: bool = False) -> None:
        self._conn = conn
        self._read_only = read_only
        if read_only:
            # Manual-commit mode: psycopg's BEGIN must not depend on the session default, which a statement can turn
            # off. (Never set False: that would force READ WRITE over a server-side read-only default.)
            conn.read_only = True
        self._messages: deque[ServerMessage] = deque(maxlen=1000)
        self._cursor_ids = itertools.count(1)
        # A cancel before run() has sent anything is kept here: the server ignores one for an idle backend (Task 6b)
        self._window = CancelWindow("PostgreSQL")
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

    def reported_setting(self, name: str) -> str | None:
        return self._conn.info.parameter_status(name)  # sent by the server at connect and on every change

    def login_privileges(self) -> LoginPrivileges:
        try:
            row = self._conn.execute(_LOGIN_SQL).fetchone()
        except psycopg.Error as exc:
            raise self.translate_error(exc, _LOGIN_SQL) from exc
        return LoginPrivileges(grants=[g for g, held in zip(_LOGIN_GRANTS, row or (), strict=False) if held])

    def _check_database(self, path: list[PathSegment]) -> None:
        if path and path[0].name != self.current_database():
            raise ValidationFailed(f"Connection is bound to database '{self.current_database()}', not '{path[0].name}'")

    def list_children(self, path: list[PathSegment]) -> list[ObjectNode]:
        self._check_database(path)
        return introspection.list_children(self._conn, path)

    def object_detail(self, path: list[PathSegment]) -> ObjectDetail:
        self._check_database(path)
        return ddl.object_detail(self._conn, path)

    def edit_target(self, path: list[PathSegment], *, order: bool = True) -> EditTarget:
        self._check_database(path)
        return editing.edit_target(self._conn, path, order=order)

    def table_columns(self, path: list[PathSegment]) -> list[TableColumn]:
        self._check_database(path)
        return columns.table_columns(self._conn, path)

    def references(self, schema: str, names: list[str], *, rows: bool = False) -> list[Reference]:
        """With `rows`, the size of each referencing table is read, which waits for its lock (an uncommitted ALTER in
        another session): at most PLAN_LOCK_TIMEOUT_MS, in a block of its own, rolled back, as a dump plan's (only on
        an auto-commit connection between statements). Locked: the keys again without the sizes (each table may hold
        rows), logged with the code only."""
        bounded = rows and self._conn.autocommit and self._conn.info.transaction_status == pq.TransactionStatus.IDLE
        try:
            if bounded:
                self._conn.execute("BEGIN")
                self._conn.execute(f"SET LOCAL lock_timeout = {PLAN_LOCK_TIMEOUT_MS}")
            return references.references(self._conn, schema, names, rows=rows)
        except psycopg.Error as exc:
            if not (bounded and exc.sqlstate == _LOCK_NOT_AVAILABLE):
                raise
            logger.info("A copy plan's foreign keys were read without the tables' sizes ({})", exc.sqlstate)
        finally:
            if bounded:
                self._end_block()
        return references.references(self._conn, schema, names)

    def dump_session(self, scope: DumpScope) -> DumpSession:
        return dump.SESSION

    def dump_catalog(self, scope: DumpScope, kinds: DumpObjectKinds, *, ddl: bool = True) -> DumpCatalog:
        """The dump job's read (`ddl`, inside its own snapshot) as it is: it is cancellable. A plan's (on the metadata
        connection) waits at most PLAN_LOCK_TIMEOUT_MS for a lock (an uncommitted ALTER in another session would hold
        the connection, and the tree and completion queued behind it, as long as it lasts: Task 9 fix M3), in a block
        of its own, rolled back: only on an auto-commit connection between statements, as the stable order's EXPLAIN."""
        version, database = self.server_info().version, self.current_database() or ""
        bounded = (not ddl and self._conn.autocommit
                   and self._conn.info.transaction_status == pq.TransactionStatus.IDLE)
        try:
            if bounded:
                self._conn.execute("BEGIN")
                self._conn.execute(f"SET LOCAL lock_timeout = {PLAN_LOCK_TIMEOUT_MS}")
            return dump.dump_catalog(self._conn, scope, kinds, version, database, ddl=ddl)
        except psycopg.Error as exc:
            if bounded and exc.sqlstate == _LOCK_NOT_AVAILABLE:
                raise StatementError(LOCKED_FOR_PLAN, code=_LOCK_NOT_AVAILABLE) from exc
            raise self.translate_error(exc, "dump catalog") from exc
        finally:
            if bounded:
                self._end_block()

    def _end_block(self) -> None:
        """Rolls back a block DbaGuy opened on this connection; one that is gone has nothing to roll back."""
        if self._conn.info.transaction_status == pq.TransactionStatus.IDLE:
            return
        try:
            self._conn.execute("ROLLBACK")
        except psycopg.Error as exc:
            logger.debug("A plan's block was not rolled back: {}", exc.sqlstate or type(exc).__name__)

    def schema_snapshot(self) -> SchemaSnapshot:
        # The connection's own (native-loader) cursors: arrays come back as lists, unlike the text-first user cursors.
        return pg_schema.build_snapshot(self._conn)

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
        if first_keyword(sql) in _COMMIT_KEYWORDS and cursor.statusmessage == "ROLLBACK":
            # COMMIT of an aborted transaction is a silent ROLLBACK: say so, as the Commit button does.
            logger.info("A typed COMMIT rolled back an aborted transaction on db={}", self.current_database())
            self._messages.append(ServerMessage(severity=MessageSeverity.WARNING, text=FAILED_COMMIT_MESSAGE))
        return _BufferedResult(self, cursor, sql)

    def _run_streamed(self, sql: str, size: int = _STREAM_CHUNK) -> StatementResult:
        try:
            return _StreamResult(self, self._user_cursor(), sql, size)  # closes its cursor when it fails
        except StatementError as exc:
            if not _is_multi_statement_rejection(exc):
                raise
            logger.debug("Script has several statements and cannot be streamed; running it buffered")
        return self._run_buffered(sql)

    def _restore_read_only(self) -> None:
        """Re-asserts READ ONLY when the server reports the session default as off, or reports nothing (PG < 14).

        An aborted transaction is left alone: SET cannot run in it, and it can write nothing until ROLLBACK undoes
        whatever switched the default off.
        """
        if not self._read_only or self.transaction_failed():
            return
        state = self._conn.info.parameter_status(_READ_ONLY_GUC)
        if state == "on":
            return
        self._conn.execute(_READ_ONLY_SESSION)
        if state == "off":
            logger.warning("Read-only connection: default_transaction_read_only was turned off; restored")

    def run(self, sql: str, options: RunOptions | None = None) -> StatementResult:
        if self._read_only and len(split(sql, Dialect.POSTGRES)) > 1:
            # A script could switch the default off and commit between statements, which no per-call check can see.
            raise StatementError(
                "Read-only connections run one statement at a time", code="read_only_single_statement"
            )
        if _uses_client_copy(sql):
            raise StatementError(_UNSUPPORTED_COPY, code="unsupported_copy")
        try:
            # Right before the first send (the checks above take ~1 ms per KB): a cancel kept while armed means nothing
            # is sent; from here a cancel goes to the server.
            self._window.take_over()
            self._restore_read_only()
            if _likely_returns_rows(sql):
                status = self._conn.info.transaction_status
                if options is not None and options.row_at_a_time and status != pq.TransactionStatus.INERROR:
                    # An MCP agent's read (spec ⑧ T6-I1): one row in memory at a time, no FETCH round trip per row;
                    # cancelling an abandoned stream aborts the frame's transaction, which is rolled back anyway
                    return self._run_streamed(sql, size=1)
                if status == pq.TransactionStatus.IDLE and self._conn.autocommit:
                    return self._run_streamed(sql)  # no transaction: cancelling an abandoned stream is harmless
                # In a transaction, or about to open one (manual mode: psycopg begins before the first statement):
                # never stream, abandoning a stream would cancel it and abort the user's transaction.
                if status in (pq.TransactionStatus.IDLE, pq.TransactionStatus.INTRANS) and _declarable(sql):
                    return _CursorResult(self, self._server_cursor(), sql,
                                         read_ahead=options is None or options.describe)
                # Anything else (SHOW, EXPLAIN, RETURNING, scripts…) is read in full.
            return self._run_buffered(sql)
        except psycopg.Error as exc:  # e.g. creating a cursor on a dead connection
            raise self.translate_error(exc, sql) from exc

    def autocommit(self) -> bool:
        return self._conn.autocommit

    def set_autocommit(self, enabled: bool) -> None:
        if self._conn.autocommit == enabled:
            return
        if self._conn.info.transaction_status in (pq.TransactionStatus.INTRANS, pq.TransactionStatus.INERROR):
            raise transaction_open_error()
        try:
            self._conn.autocommit = enabled  # a dead connection fails here, translated like commit() does
        except psycopg.Error as exc:
            raise self.translate_error(exc, "SET AUTOCOMMIT") from exc
        logger.info("Auto-commit {} on PostgreSQL db={}", "on" if enabled else "off", self.current_database())

    def commit(self) -> None:
        try:
            self._conn.commit()  # psycopg skips it when the connection is idle
        except psycopg.Error as exc:
            raise self.translate_error(exc, "COMMIT") from exc

    def rollback(self) -> None:
        try:
            self._conn.rollback()
        except psycopg.Error as exc:
            raise self.translate_error(exc, "ROLLBACK") from exc

    def in_transaction(self) -> bool:
        return self._conn.info.transaction_status in (pq.TransactionStatus.INTRANS, pq.TransactionStatus.INERROR)

    def transaction_failed(self) -> bool:
        return self._conn.info.transaction_status == pq.TransactionStatus.INERROR

    def arm_cancel(self) -> None:
        self._window.arm()

    def disarm_cancel(self) -> None:
        self._window.disarm()

    def cancel(self) -> None:
        if self._window.keep():
            return  # run() has not sent the statement yet: it will not (force_cancel and abort come here too)
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
    dialect = Dialect.POSTGRES
    capabilities = AdapterCapabilities(
        per_database_connection=True, cancel_aborts_transaction=True, row_editing=True, orders_browse_by_key=True,
        run_file=True, dump=True, copy_data=True, login_privileges=True,
    )

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
        self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None, database: str | None,
        *, agent: bool = False,
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
            # prepare_threshold=None: psycopg's PrepareManager otherwise keeps the encoded text of the last 100
            # distinct queries even when none is prepared, so a restore of 1 MiB INSERTs (or one 10 MB INSERT in a
            # query tab) held ~100 MB until the session closed (06a fix wave F1). Nothing in DbaGuy uses named
            # prepared statements; `describe()` uses libpq's unnamed statement, which this does not touch.
            conn = psycopg.connect(autocommit=True, prepare_threshold=None, **params)
        except psycopg.Error as exc:
            raise ConnectionFailed(str(exc).strip()) from exc
        if profile.read_only:
            # Enforced by the server too, so a function with side effects called from a SELECT cannot write either.
            try:
                conn.execute(_READ_ONLY_SESSION)
            except psycopg.Error as exc:
                conn.close()
                raise ConnectionFailed(f"Could not make the connection read-only: {exc}") from exc
        logger.info(
            "Connected to PostgreSQL {}:{} db={}{}",
            params["host"], endpoint.port, params["dbname"], " read_only=True" if profile.read_only else "",
        )
        return PostgresConnection(conn, read_only=profile.read_only)
