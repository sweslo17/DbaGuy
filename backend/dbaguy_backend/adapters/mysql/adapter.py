"""MySQL through mysql-connector's pure-Python driver: user SQL sent verbatim and streamed row by row, text-first value
conversion (`types`), KILL QUERY from a side connection for cancel and for closing a partly read result, transactions
from the protocol's status flags, and READ ONLY sessions for read-only profiles."""

import re
import socket
import threading
import time
from contextlib import suppress
from typing import Any

import mysql.connector
from loguru import logger
from mysql.connector.connection import MySQLConnection
from mysql.connector.cursor import MySQLCursorRaw

from ...domain.errors import ConnectionFailed, ValidationFailed
from ...domain.execution import ColumnMeta, ErrorPosition, MessageSeverity, ServerMessage
from ...domain.metadata import ObjectDetail, ObjectNode, PathSegment, ServerInfo
from ...domain.profiles import AnyProfile, Engine, ServerProfile
from ...domain.schema import SchemaSnapshot
from ...sql.dialects import Dialect
from ...sql.lexer import first_keyword
from ..base import (
    AdapterCapabilities,
    DatabaseAdapter,
    DatabaseConnection,
    Endpoint,
    ParsedUrl,
    StatementCancelled,
    StatementError,
    StatementResult,
    transaction_open_error,
)
from . import catalog, introspection
from . import schema as mysql_schema
from .settings import URL_SCHEMES, connect_params, kill_params, parse_mysql_url, requires_tls
from .types import column_meta, row_converter

_CONNECTION_LOST_ERRNOS = frozenset({1053, 2006, 2013, 2055, 4031})  # shutdown, gone away, lost, lost at, idle kick
_QUERY_INTERRUPTED = 1317
_DRAIN_BATCH = 1000  # rows read and dropped at a time when discarding the rest of a result
_TLS_LOGIN_HINT = (  # R-03a-P5: the pure driver cannot finish a caching_sha2_password full login over plain TCP
    "MySQL 8 needs TLS (SSL mode Prefer or Require) for the first password login after a server restart; "
    "or use an account with mysql_native_password."
)
_LOGIN_FAILED_ERRNOS = frozenset({1045, 2061})  # access denied; "authentication requires secure connection"
_AT_LINE = re.compile(r" at line (\d+)\s*$")
# Ruling 12: statements that may switch auto-commit. A version comment hides the first keyword
# (`/*!50000 CALL p() */` gives ""), so a CALL written that way is caught only when its text says "autocommit".
_MENTIONS_AUTOCOMMIT = re.compile(r"autocommit", re.IGNORECASE)
_MAY_SET_AUTOCOMMIT = frozenset({"CALL", "EXECUTE"})  # a procedure or prepared statement may switch it too
_READ_ONLY_SESSION = "SET SESSION TRANSACTION READ ONLY"


class _VerbatimCursor(MySQLCursorRaw):
    """Sends a statement exactly as written. `MySQLCursor.execute()` first runs any text containing `delimiter `
    through the connector's DELIMITER-script rewriter (even inside a comment or as an alias), which changes user SQL."""

    def execute_verbatim(self, sql: str) -> None:
        self._connection.handle_unread_result()
        self._reset_result()
        statement = sql.encode(self._connection.python_charset)
        self._executed = statement
        self._handle_result(self._connection.cmd_query(statement))


class MysqlResult(StatementResult):
    def __init__(self, owner: "MysqlConnection", cursor: _VerbatimCursor) -> None:
        self._owner, self._cursor = owner, cursor
        self._closed = False
        self._failed = False  # after a driver error, never ask for another result set: its flags may be stale
        self._load_columns()

    def _load_columns(self) -> None:
        description = self._cursor.description
        self._columns = [column_meta(d) for d in description] if description else None
        self._convert = row_converter(description) if description else None
        self._warned = False

    def _translate(self, exc: mysql.connector.Error) -> StatementError:
        self._failed = True
        return self._owner.translate_error(exc, statement=True)

    def columns(self) -> list[ColumnMeta] | None:
        return self._columns

    def fetch(self, n: int) -> list[list[Any]]:
        if self._columns is None or self._closed or n <= 0:  # the driver's fetchmany(0) would return a row
            return []
        try:
            rows = self._cursor.fetchmany(n)
        except mysql.connector.Error as exc:
            raise self._translate(exc) from exc
        if self._owner.killed():  # rows that reached us after KILL QUERY: the statement was cancelled
            self._failed = True
            raise StatementCancelled()
        return [self._convert(row) for row in rows]

    def affected_rows(self) -> int | None:
        rowcount = self._cursor.rowcount
        return rowcount if self._columns is None and rowcount >= 0 else None

    def next_result(self) -> bool:
        if self._closed or self._failed:
            return False
        try:
            more = bool(self._cursor.nextset())
        except mysql.connector.Error as exc:
            raise self._translate(exc) from exc
        if self._owner.killed():
            self._failed = True
            raise StatementCancelled()
        if more:
            self._load_columns()
        return more

    def take_messages(self) -> list[ServerMessage]:
        """No round trip: the warning count arrives with the end of each result set (SHOW WARNINGS is left to the user)."""
        count = self._cursor.warning_count
        if self._warned or not count or self._owner.unread():
            return []
        self._warned = True
        text = f"{count} warning{'s' if count != 1 else ''}: run SHOW WARNINGS to see {'them' if count != 1 else 'it'}"
        return [ServerMessage(severity=MessageSeverity.WARNING, text=text)]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._owner.finish(self._cursor, more_results=not self._failed)


class MysqlConnection(DatabaseConnection):
    def __init__(self, conn: MySQLConnection, kill_params: dict[str, Any], read_only: bool, database: str | None) -> None:
        self._conn = conn
        self._kill_params = kill_params
        self._read_only = read_only  # READ ONLY was set on the session at connect; the server enforces it
        self._database = database
        self._thread_id = conn.connection_id
        self._autocommit = True  # connections open in auto-commit mode (settings fix it)
        self._autocommit_stale = False  # a statement may have switched it: re-read once nothing is unread
        self._killed = threading.Event()  # KILL QUERY was sent for the current statement (R-03a-P4)

    # ---- statements ----

    def kill_query(self, why: str) -> bool:
        """KILL QUERY <our thread> from a short side connection (same endpoint, account and TLS settings). Ends only
        the running statement: the session, its temporary tables and its transaction stay. Safe from any thread."""
        started = time.monotonic()
        try:  # a fresh conn_attrs per connection: the driver adds its own entries to the dict it is given
            killer = mysql.connector.connect(**self._kill_params, conn_attrs={"program_name": "DbaGuy cancel"})
            try:
                killer.cmd_query(f"KILL QUERY {int(self._thread_id)}")
            finally:
                killer.close()
        except Exception as exc:  # mysql.connector.Error, OSError, …: never let a cancel raise
            logger.warning("MySQL {} failed: KILL QUERY {} not sent ({}: {})", why, self._thread_id, type(exc).__name__, exc)
            return False
        logger.info("MySQL {}: KILL QUERY {} in {:.0f} ms", why, self._thread_id, (time.monotonic() - started) * 1000)
        return True

    def cancel(self) -> None:
        self._killed.set()  # first: SLEEP() may return its 1 before the KILL's own reply reaches us
        if not self.kill_query("cancel"):
            self._killed.clear()  # nothing was interrupted: report what the statement really does

    def killed(self) -> bool:
        return self._killed.is_set()

    def unread(self) -> bool:
        return bool(self._conn.unread_result)

    def finish(self, cursor: _VerbatimCursor, more_results: bool) -> None:
        """End a result: stop the statement if rows are still coming, then read and drop what is left, including later
        result sets of a CALL (otherwise: "Commands out of sync"). The transaction is untouched. If the KILL cannot be
        sent, everything left is read in raw batches (never held in memory at once, never converted), which is slow for a
        huge result but keeps the transaction. The statement is over afterwards, so its "KILL sent" flag is cleared."""
        try:
            if self._conn.unread_result:
                self.kill_query("close of a partly read result")
            while True:
                while self._conn.unread_result:
                    self._conn.get_rows(count=_DRAIN_BATCH, raw=True)  # R-03a-P7: raw batches, not consume_results()
                if not more_results or not cursor.nextset():
                    break
        except mysql.connector.Error as exc:
            if exc.errno != _QUERY_INTERRUPTED:  # 1317 is the KILL we just sent
                logger.warning("Discarding the rest of a MySQL result failed: {}", exc)
        finally:
            self._killed.clear()  # R-03a-P4: the flag belongs to this statement only
            with suppress(mysql.connector.Error):
                cursor.close()

    def translate_error(self, exc: mysql.connector.Error, statement: bool = False) -> StatementError:
        """`statement`: the error comes from the user's statement (run, fetch, next result), the only place a cancel can
        show. Commit, rollback, auto-commit and state queries never report "cancelled" (R-03a-P4)."""
        if statement and (self._killed.is_set() or exc.errno == _QUERY_INTERRUPTED):
            return StatementCancelled()
        text = exc.msg or str(exc)
        # errno -1 OperationalError: "MySQL Connection not available" (the socket is already gone)
        if exc.errno in _CONNECTION_LOST_ERRNOS or (isinstance(exc, mysql.connector.errors.OperationalError) and exc.errno == -1):
            return StatementError(f"Connection to the server was lost: {text}", code="connection_lost")
        message = text + (f"\nSQLSTATE: {exc.sqlstate}" if exc.sqlstate else "")
        line = _AT_LINE.search(text)
        return StatementError(
            message, code=str(exc.errno) if exc.errno > 0 else None,
            position=ErrorPosition(line=int(line.group(1)), column=1) if line else None,
        )

    def run(self, sql: str) -> StatementResult:
        self._killed.clear()
        if _MENTIONS_AUTOCOMMIT.search(sql) or first_keyword(sql, Dialect.MYSQL) in _MAY_SET_AUTOCOMMIT:
            self._autocommit_stale = True
        cursor = _VerbatimCursor(self._conn)  # not conn.cursor(): that sends a PING first, one round trip per statement
        try:
            cursor.execute_verbatim(sql)
        except mysql.connector.Error as exc:
            error = self.translate_error(exc, statement=True)
            self._killed.clear()  # the statement is over: its flag must not reach the next call (R-03a-P4)
            with suppress(mysql.connector.Error):
                cursor.close()
            raise error from exc
        result = MysqlResult(self, cursor)
        if self._killed.is_set():  # it ended normally after KILL QUERY (SLEEP() returns 1): still a cancel
            result.close()  # finish() clears the flag
            raise StatementCancelled()
        return result

    # ---- transactions ----

    def autocommit(self) -> bool:
        """Cached: the driver's own property is a round trip, and the protocol does not report the mode (Ruling 12).
        Re-read only once the statement has nothing left to send: a CALL paused at the end of one result set still has
        the next ones on the wire, and a query sent then would read them as its own answer."""
        if self._autocommit_stale and not self._conn.unread_result and not self._conn.have_next_result:
            try:
                row = catalog.first_row(self._conn, "SELECT @@session.autocommit")
            except mysql.connector.Error as exc:
                logger.debug("Could not re-read auto-commit on MySQL thread {}: {}", self._thread_id, exc)
            else:
                if row is not None:
                    self._autocommit = bool(int(row[0]))
                    self._autocommit_stale = False
        return self._autocommit

    def set_autocommit(self, enabled: bool) -> None:
        if self.autocommit() == enabled:
            return
        if self._conn.in_transaction:
            raise transaction_open_error()
        try:
            self._conn.cmd_query(f"SET autocommit = {1 if enabled else 0}")
        except mysql.connector.Error as exc:
            raise self.translate_error(exc) from exc
        self._autocommit = enabled
        logger.info("Auto-commit {} on MySQL thread {}", "on" if enabled else "off", self._thread_id)

    def in_transaction(self) -> bool:
        return bool(self._conn.in_transaction)  # the status flag of the last packet: no round trip

    def commit(self) -> None:
        try:
            self._conn.commit()
        except mysql.connector.Error as exc:
            raise self.translate_error(exc) from exc

    def rollback(self) -> None:
        try:
            self._conn.rollback()
        except mysql.connector.Error as exc:
            raise self.translate_error(exc) from exc

    # ---- metadata ----

    def server_info(self) -> ServerInfo:
        try:
            row = catalog.first_row(self._conn, "SELECT VERSION(), DATABASE(), CURRENT_USER()")
        except mysql.connector.Error as exc:
            raise self.translate_error(exc) from exc
        version, database, user = row
        self._database = database
        product = "MariaDB" if "mariadb" in version.lower() else "MySQL"
        return ServerInfo(engine=Engine.MYSQL, version=f"{product} {version}", current_database=database, current_user=user)

    def current_database(self) -> str | None:
        return self._database

    def list_children(self, path: list[PathSegment]) -> list[ObjectNode]:
        return introspection.list_children(self._conn, path)

    def object_detail(self, path: list[PathSegment]) -> ObjectDetail:
        return introspection.object_detail(self._conn, path)

    def schema_snapshot(self) -> SchemaSnapshot:
        return mysql_schema.build_snapshot(self._conn)

    def close(self) -> None:
        try:
            self._conn.close()
        except (mysql.connector.Error, OSError):
            self._conn.shutdown()


def _enable_keepalive(conn: MySQLConnection) -> None:
    """TCP keepalive (30 s idle, 10 s interval, 3 probes) as for PostgreSQL; the driver has no option for it, so it is
    set on its socket, best effort."""
    sock = getattr(getattr(conn, "_socket", None), "sock", None)
    if sock is None:
        logger.debug("MySQL socket not reachable; TCP keepalive not set")
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPALIVE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
            if (option := getattr(socket, name, None)) is not None:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
    except OSError as exc:
        logger.debug("MySQL TCP keepalive not set: {}", exc)


def _login_failed(exc: mysql.connector.Error) -> bool:
    return exc.errno in _LOGIN_FAILED_ERRNOS or "caching_sha2_password" in str(exc)


class MysqlAdapter(DatabaseAdapter):
    engine = Engine.MYSQL
    url_schemes = URL_SCHEMES
    default_port = 3306
    dialect = Dialect.MYSQL
    capabilities = AdapterCapabilities()  # one connection sees every database; KILL QUERY keeps the transaction

    def parse_url(self, url: str) -> ParsedUrl:
        return parse_mysql_url(url)

    def open(
        self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None, database: str | None
    ) -> DatabaseConnection:
        if not isinstance(profile, ServerProfile) or endpoint is None:
            raise ValidationFailed("Not a MySQL server profile")
        params = connect_params(profile, endpoint, password, database)
        try:
            conn = mysql.connector.connect(**params)
        except mysql.connector.Error as exc:
            message = str(exc).strip()
            if profile.ssl.mode == "disable" and _login_failed(exc):
                message += "\n" + _TLS_LOGIN_HINT  # R-03a-P5
            raise ConnectionFailed(message) from exc
        except (AttributeError, ValueError, TypeError) as exc:  # an option the driver does not know or cannot take
            raise ConnectionFailed(f"Invalid MySQL connection option: {exc}") from exc
        try:
            if requires_tls(profile.ssl) and not conn.is_secure:
                raise ConnectionFailed(f"The server does not offer TLS, which ssl mode '{profile.ssl.mode}' requires")
            if profile.read_only:
                # After connect(): the driver interpolates `sql_mode`/`time_zone` options into its own SET at connect,
                # and the server enforces READ ONLY for writes hidden in functions or procedures too.
                try:
                    conn.cmd_query(_READ_ONLY_SESSION)
                except mysql.connector.Error as exc:
                    raise ConnectionFailed(f"Could not make the connection read-only: {exc}") from exc
            _enable_keepalive(conn)
        except BaseException:
            conn.shutdown()
            raise
        logger.info(
            "Connected to MySQL {}:{} db={} tls={}{} thread={}{}",
            endpoint.tls_server_name or endpoint.host, endpoint.port, params.get("database"), profile.ssl.mode,
            " (active)" if conn.is_secure else "", conn.connection_id, " read_only=True" if profile.read_only else "",
        )
        return MysqlConnection(conn, kill_params(params), profile.read_only, params.get("database"))
