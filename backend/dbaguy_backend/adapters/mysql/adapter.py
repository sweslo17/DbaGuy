"""MySQL through mysql-connector's pure-Python driver: user SQL sent verbatim and streamed row by row, text-first value
conversion (`types`), KILL QUERY from a side connection for cancel and for closing a partly read result, transactions
from the protocol's status flags, and READ ONLY sessions for read-only profiles."""

import re
import threading
import time
from contextlib import suppress
from typing import Any

import mysql.connector
from loguru import logger
from mysql.connector.connection import MySQLConnection
from mysql.connector.constants import ServerFlag
from mysql.connector.cursor import MySQLCursorRaw

from ...domain.agent import LoginPrivileges
from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ...domain.edits import EditTarget
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
    DataTools,
    Dumping,
    Endpoint,
    ParsedUrl,
    RowEditing,
    RunOptions,
    StatementCancelled,
    StatementError,
    StatementResult,
    invalid_text_error,
    transaction_open_error,
)
from ..cancel_window import CancelWindow
from ..columns import TableColumn
from ..dump import DumpCatalog, DumpSession
from ..sockets import enable_keepalive
from . import catalog, columns, dump, editing, introspection
from . import schema as mysql_schema
from .settings import URL_SCHEMES, connect_params, kill_params, parse_mysql_url, requires_tls, session_settings
from .types import column_meta, row_converter

_CONNECTION_LOST_ERRNOS = frozenset({1053, 2006, 2013, 2055, 4031})  # shutdown, gone away, lost, lost at, idle kick
_QUERY_INTERRUPTED = 1317
_DRAIN_BATCH = 1000  # rows read and dropped at a time when discarding the rest of a result
# When the KILL cannot be sent, discarding the rest of a result gives up after this long and drops the connection: an
# endless, stalled or trickling stream would otherwise hold the session forever (Reconnect beats a wedged tab). After a
# KILL that was sent, reading rows has no limit: the server stops, and only what it already sent is left (R-03a-T9f).
# Nor has waiting for the end of the statement: the server may still be finishing what it killed (a slow rollback, a
# CONTINUE HANDLER, a SLEEP or two). Only once a CALL sends another result set after the KILL (a SLEEP in the procedure
# swallowed it) are the waits for further result sets limited to this long in total, so a loop around that SLEEP
# cannot hold the close until the procedure ends (R-03a-F5).
_DRAIN_LIMIT_S = 10.0
_FAST_BATCH_S = 0.01  # without a KILL, batches grow from one row only while each one comes back this fast
_DRAIN_REPORT_MS = 200  # a discard slower than this is logged with its row count
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
# ERR packets carry no status flags, but a server error can end the transaction: a deadlock rolls it back, and DDL
# commits it before it runs, even DDL that then fails. This statement reads the flags again (R-03a-T9b) and, being a
# diagnostic statement, keeps what SHOW WARNINGS, SHOW ERRORS and GET DIAGNOSTICS report (R-03a-T9e; `DO 0` cleared it).
_STATUS_PROBE = "SHOW WARNINGS LIMIT 0"
_PARSE_ERRNOS = frozenset({1064, 1065})  # syntax error, empty query: rejected before anything ran
_INFO_COUNT = re.compile(r"[0-9]+")  # the server prints the counts with %ld: ASCII digits


def rows_matched(info: str | None) -> int | None:
    """The rows an UPDATE matched, from its OK packet's info: `Rows matched: 1  Changed: 0  Warnings: 0` (⑤ A1), which
    lc_messages translates (`Datensätze gefunden: 1  Geändert: 0  Warnungen: 0`, `該当した行: 1  変更: 0  警告: 0`) with
    the three counts in the same order (ruling G8): the first of exactly three numbers; None otherwise (fail closed)."""
    counts = _INFO_COUNT.findall(info or "")
    return int(counts[0]) if len(counts) == 3 else None


class _VerbatimCursor(MySQLCursorRaw):
    """Sends a statement exactly as written. `MySQLCursor.execute()` first runs any text containing `delimiter `
    through the connector's DELIMITER-script rewriter (even inside a comment or as an alias), which changes user SQL."""

    ok_info: str | None = None  # the OK packet's info text of the last statement (`rows_matched`)
    ok_status: int | None = None  # the OK packet's status flags of the last statement (`backslash_escapes`)

    def execute_verbatim(self, sql: str) -> None:
        self._connection.handle_unread_result()
        self._reset_result()
        statement = sql.encode(self._connection.python_charset)
        self._executed = statement
        answer = self._connection.cmd_query(statement)
        # An UPDATE's OK packet says how many rows it matched, which affected_rows (rows changed) does not (⑤ D-05-5),
        # and every OK packet whether the session reads backslashes as escapes (05a review M2)
        ok = isinstance(answer, dict) and "affected_rows" in answer
        self.ok_info = answer.get("info_msg") if ok else None
        self.ok_status = answer.get("status_flag") if ok else None
        self._handle_result(answer)

    def next_set_within(self, seconds: float | None) -> bool:
        """nextset() that waits at most `seconds` for the next result set to start (ReadTimeoutError after that). The
        cursor's own read timeout is restored afterwards, also when it raises."""
        previous, self._read_timeout = self._read_timeout, seconds
        try:
            return bool(self.nextset())
        finally:
            self._read_timeout = previous


class MysqlResult(StatementResult):
    def __init__(self, owner: "MysqlConnection", cursor: _VerbatimCursor, *, update: bool = False) -> None:
        self._owner, self._cursor = owner, cursor
        self._update = update  # an UPDATE: its rows matched are in the OK packet's info (matched_rows)
        self._closed = False
        # After an error packet the statement is over: never ask for another result set (the flags are stale then).
        # A cancel seen after a successful read is not a failure: the rest of the statement is still on the wire.
        self._failed = False
        self._load_columns()

    def _load_columns(self) -> None:
        description = self._cursor.description
        self._columns = [column_meta(d) for d in description] if description else None
        self._convert = row_converter(description) if description else None
        self._warned = False

    def _translate(self, exc: mysql.connector.Error) -> StatementError:
        self._failed = True
        return self._owner.statement_error(exc)

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
            raise StatementCancelled()  # not `_failed`: close() still reads it to its end (later sets, the final OK)
        return [self._convert(row) for row in rows]

    def affected_rows(self) -> int | None:
        rowcount = self._cursor.rowcount
        return rowcount if self._columns is None and rowcount >= 0 else None

    def matched_rows(self) -> int | None:
        return rows_matched(self._cursor.ok_info) if self._update else self.affected_rows()

    def next_result(self) -> bool:
        if self._closed or self._failed:
            return False
        try:
            more = bool(self._cursor.nextset())
        except mysql.connector.Error as exc:
            raise self._translate(exc) from exc
        if self._owner.killed():  # e.g. a SLEEP in the procedure swallowed the KILL and the next set came anyway
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


class MysqlConnection(DatabaseConnection, RowEditing, DataTools, Dumping):
    def __init__(self, conn: MySQLConnection, kill_params: dict[str, Any], read_only: bool, database: str | None) -> None:
        self._conn = conn
        self._kill_params = kill_params
        self._read_only = read_only  # READ ONLY was set on the session at connect; the server enforces it
        self._database = database
        self._thread_id = conn.connection_id
        self._autocommit = True  # connections open in auto-commit mode (settings fix it)
        self._autocommit_stale = False  # a statement may have switched it: re-read once nothing is unread
        self._killed = threading.Event()  # KILL QUERY was sent for the current statement (R-03a-P4)
        # A cancel before run() has sent anything is kept here: KILL QUERY does nothing to an idle thread (Task 6b)
        self._window = CancelWindow("MySQL")
        self._lost = False  # the connection is gone (or was dropped): it has no transaction any more
        self._ok_status: int | None = None  # the status flags of the last run()'s OK packet (backslash_escapes)

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

    def arm_cancel(self) -> None:
        self._window.arm()

    def disarm_cancel(self) -> None:
        self._window.disarm()

    def cancel(self) -> None:
        if self._window.keep():
            return  # run() has not sent the statement yet: it will not, and no KILL is needed (nor the KILL flag)
        self._killed.set()  # first: SLEEP() may return its 1 before the KILL's own reply reaches us
        if not self.kill_query("cancel"):
            self._killed.clear()  # nothing was interrupted: report what the statement really does

    def killed(self) -> bool:
        return self._killed.is_set()

    def unread(self) -> bool:
        return bool(self._conn.unread_result)

    def finish(self, cursor: _VerbatimCursor, more_results: bool) -> None:
        """End a result: stop the statement if anything is still to come, then read and drop what is left, including
        later result sets of a CALL (otherwise the next statement reads them as its own answer). The transaction is
        untouched, unless the connection is dropped because the rest would not end (see `_discard`). The statement is
        over afterwards, so its "KILL sent" flag is cleared.

        Caveat: a SLEEP inside a procedure swallows KILL QUERY (it returns early and the procedure goes on to its next
        statement), so a CALL can keep sending results after the KILL; they are read and dropped like the rest, until
        the waits for the ones after the first add up to `_DRAIN_LIMIT_S`."""
        try:
            # R-03a-T9a: a CALL closed between two result sets is still running, possibly towards a write.
            # R-03a-T9d: KILL even when this statement was already cancelled. That KILL may have reached the server
            # just before the statement began, and been ignored; a redundant one costs only a short side connection.
            if self._conn.unread_result or (more_results and self._conn.have_next_result):
                sent = self.kill_query("close of a partly read result")
                self._discard(cursor, more_results, killed=sent)
        except mysql.connector.Error as exc:
            if self._note_lost(exc):
                logger.warning("Discarding the rest of a MySQL result failed: {}", exc)
            else:  # the end of a killed or failing statement (1317, 1213, 3141…): expected here
                logger.debug("A discarded MySQL result ended with {}: {}", exc.errno, exc.msg)
                # A killed SELECT leaves the flags as they were; a failing statement, or a killed CALL (whose last
                # flags still say "more results"), may not.
                if _may_change_status(exc) and (exc.errno != _QUERY_INTERRUPTED or self._conn.have_next_result):
                    self._read_status()
        finally:
            self._killed.clear()  # R-03a-P4: the flag belongs to this statement only
            with suppress(mysql.connector.Error):
                cursor.close()

    def _discard(self, cursor: _VerbatimCursor, more_results: bool, killed: bool) -> None:
        """Read and drop the rest of the statement in raw batches (R-03a-P7: never held at once, never converted).

        Once `_DRAIN_LIMIT_S` is used up the connection is dropped. Without a KILL (it could not be sent) the limit
        covers everything: every read waits at most for the time left, and batches start at one row and grow only
        while rows come fast. So neither a stream that never ends, nor one that stalls or trickles, nor a CALL working
        between result sets can hold the session. After a KILL that was sent, reading rows is not limited (only what
        the server already sent is left, R-03a-T9f), and neither is waiting while the server finishes the killed
        statement. A result set arriving after the KILL proves a SLEEP in the procedure swallowed it: from then on the
        waits for further result sets are limited, so a procedure looping around that SLEEP cannot hold the session
        either (R-03a-F5).
        """
        started = time.monotonic()
        deadline = None if killed else started + _DRAIN_LIMIT_S  # the whole drain, without a KILL
        # After a KILL: None (no limit) until a result set arrives anyway, then what is left for waiting on later ones.
        waiting_left: float | None = None
        batch = _DRAIN_BATCH if killed else 1
        drained = 0
        waiting = False  # in a wait limited here (a ReadTimeoutError elsewhere is the profile's own read_timeout)
        try:
            while True:
                while self._conn.unread_result:
                    left = None if deadline is None else deadline - time.monotonic()
                    if left is not None and left <= 0:
                        self._give_up(drained, killed)
                        return
                    read_started = time.monotonic()
                    waiting = deadline is not None
                    rows, _ = self._conn.get_rows(count=batch, raw=True, read_timeout=left)
                    waiting = False
                    drained += len(rows)
                    if deadline is not None:  # a trickle is then read row by row, each read bounded by the time left
                        fast = time.monotonic() - read_started < _FAST_BATCH_S
                        batch = min(batch * 2, _DRAIN_BATCH) if fast else 1
                if not (more_results and self._conn.have_next_result):
                    break
                left = waiting_left if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    self._give_up(drained, killed)
                    return
                wait_started, waiting = time.monotonic(), left is not None
                more = cursor.next_set_within(left)
                waiting = False
                if not more:
                    break
                if deadline is None:  # after a KILL, and another result set came: a SLEEP swallowed the KILL
                    if waiting_left is None:
                        waiting_left = _DRAIN_LIMIT_S  # the limit starts now: that wait was the server finishing up
                    else:
                        waiting_left -= time.monotonic() - wait_started
        except mysql.connector.errors.ReadTimeoutError:
            if not waiting:
                raise  # the profile's own read_timeout: a lost connection, handled by finish()
            self._give_up(drained, killed)
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000  # the drain alone: the KILL's connection is not counted
            if elapsed_ms > _DRAIN_REPORT_MS:
                logger.info("Discarded {:,} rows of a closed MySQL result in {:.0f} ms", drained, elapsed_ms)

    def _give_up(self, drained: int, killed: bool) -> None:
        """The rest of a result would not end (without a KILL), or a CALL kept sending result sets after one: drop the
        connection, so the next call reports `connection_lost` (and the Reconnect UI) instead of the session hanging."""
        if killed:
            logger.warning(
                "A MySQL CALL went on sending result sets after KILL QUERY (a SLEEP in it swallows the KILL), and the "
                "waits for more reached {:.0f} s ({:,} rows discarded); closing the connection of thread {}",
                _DRAIN_LIMIT_S, drained, self._thread_id,
            )
        else:
            logger.warning(
                "Discarding a MySQL result without KILL took over {:.0f} s ({:,} rows); closing the connection of "
                "thread {}", _DRAIN_LIMIT_S, drained, self._thread_id,
            )
        self._drop()

    def _drop(self) -> None:
        self._lost = True
        self._conn.shutdown()
        self._conn.unread_result = False  # the next command then fails on the closed socket ("lost"), not "unread"

    def _note_lost(self, exc: mysql.connector.Error) -> bool:
        """Whether `exc` means the connection is gone; it then reports no transaction from now on. A client-side read
        or write timeout leaves a reply half read, so that connection is dropped: its next reply would be the old one."""
        if not _is_lost(exc):
            return False
        if isinstance(exc, _CLIENT_TIMEOUTS):
            self._drop()
        self._lost = True
        return True

    def _read_status(self) -> None:
        """Refresh the transaction and more-results flags after an error packet (see `_STATUS_PROBE`)."""
        if self._lost or self._conn.unread_result:
            return
        try:
            self._conn.cmd_query(_STATUS_PROBE)  # an empty result set: its end packet carries the flags
            self._conn.get_rows(raw=True)
        except mysql.connector.Error as exc:
            self._note_lost(exc)
            logger.debug("Could not re-read the MySQL status flags on thread {}: {}", self._thread_id, exc)

    def statement_error(self, exc: mysql.connector.Error) -> StatementError:
        """The error of the user's statement (run, fetch, next result). A server error may have ended the transaction
        (R-03a-T9b), so the status flags are read again; never after a parse error, a cancel or a lost connection."""
        error = self.translate_error(exc, statement=True)
        if _may_change_status(exc) and not isinstance(error, StatementCancelled):
            self._read_status()
        return error

    def translate_error(self, exc: mysql.connector.Error, statement: bool = False) -> StatementError:
        """`statement`: the error comes from the user's statement (run, fetch, next result), the only place a cancel can
        show. Commit, rollback, auto-commit and state queries never report "cancelled" (R-03a-P4)."""
        text = exc.msg or str(exc)
        if self._note_lost(exc):  # before the cancel check: a KILL QUERY never drops the connection
            return StatementError(f"Connection to the server was lost: {text}", code="connection_lost")
        if statement and (self._killed.is_set() or exc.errno == _QUERY_INTERRUPTED):
            return StatementCancelled()
        message = text + (f"\nSQLSTATE: {exc.sqlstate}" if exc.sqlstate else "")
        line = _AT_LINE.search(text)
        return StatementError(
            message, code=str(exc.errno) if exc.errno > 0 else None,
            position=ErrorPosition(line=int(line.group(1)), column=1) if line else None,
        )

    def run(self, sql: str, options: RunOptions | None = None) -> StatementResult:
        self._killed.clear()
        keyword = first_keyword(sql, Dialect.MYSQL)
        if _MENTIONS_AUTOCOMMIT.search(sql) or keyword in _MAY_SET_AUTOCOMMIT:
            self._autocommit_stale = True
        # Right before the first send, after the clear: a cancel kept while armed means nothing is sent; from here
        # one sets the KILL flag and KILLs.
        self._window.take_over()
        cursor = _VerbatimCursor(self._conn)  # not conn.cursor(): that sends a PING first, one round trip per statement
        self._ok_status = None
        try:
            cursor.execute_verbatim(sql)
        except (mysql.connector.Error, UnicodeEncodeError) as exc:
            error = self.statement_error(exc) if isinstance(exc, mysql.connector.Error) else invalid_text_error(sql, exc.start)
            self._killed.clear()  # the statement is over: its flag must not reach the next call (R-03a-P4)
            with suppress(mysql.connector.Error):
                cursor.close()
            raise error from exc
        self._ok_status = cursor.ok_status
        result = MysqlResult(self, cursor, update=keyword == "UPDATE")
        if self._killed.is_set():  # it ended normally after KILL QUERY (SLEEP() returns 1): still a cancel
            completed = not self._conn.have_next_result  # a CALL with more result sets to come is not over
            result.close()  # finish() clears the flag
            raise StatementCancelled(completed=completed)
        return result

    # ---- transactions ----

    def autocommit(self) -> bool:
        """Cached: the driver's own property is a round trip, and the protocol does not report the mode (Ruling 12).
        Re-read only once the statement has nothing left to send: a CALL paused at the end of one result set still has
        the next ones on the wire, and a query sent then would read them as its own answer."""
        pending = self._conn.unread_result or self._conn.have_next_result
        if self._autocommit_stale and not pending and not self._lost:
            try:
                row = catalog.first_row(self._conn, "SELECT @@session.autocommit")
            except mysql.connector.Error as exc:
                self._note_lost(exc)
                logger.debug("Could not re-read auto-commit on MySQL thread {}: {}", self._thread_id, exc)
            else:
                if row is not None:
                    self._autocommit = bool(int(row[0]))
                    self._autocommit_stale = False
        return self._autocommit

    def set_autocommit(self, enabled: bool) -> None:
        if self.autocommit() == enabled:
            return
        if self.in_transaction():
            raise transaction_open_error()
        try:
            self._conn.cmd_query(f"SET autocommit = {1 if enabled else 0}")
        except mysql.connector.Error as exc:
            raise self.translate_error(exc) from exc
        self._autocommit = enabled
        logger.info("Auto-commit {} on MySQL thread {}", "on" if enabled else "off", self._thread_id)

    def in_transaction(self) -> bool:
        # The status flag of the last packet, no round trip; a lost connection has no transaction any more.
        return not self._lost and bool(self._conn.in_transaction)

    def backslash_escapes(self) -> bool | None:
        # SERVER_STATUS_NO_BACKSLASH_ESCAPES in the last run()'s OK packet: the session's own sql_mode, no round trip
        if self._ok_status is None:
            return None
        return not self._ok_status & ServerFlag.STATUS_NO_BACKSLASH_ESCAPES

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

    def login_privileges(self) -> LoginPrivileges:
        try:
            return catalog.login_privileges(self._conn)
        except mysql.connector.Error as exc:
            raise self.translate_error(exc) from exc

    def list_children(self, path: list[PathSegment]) -> list[ObjectNode]:
        return introspection.list_children(self._conn, path)

    def object_detail(self, path: list[PathSegment]) -> ObjectDetail:
        return introspection.object_detail(self._conn, path)

    def edit_target(self, path: list[PathSegment], *, order: bool = True) -> EditTarget:
        return editing.edit_target(self._conn, path)  # no stable_order to plan (PostgreSQL's only)

    def table_columns(self, path: list[PathSegment]) -> list[TableColumn]:
        try:
            return columns.table_columns(self._conn, path)
        except mysql.connector.Error as exc:
            raise self.translate_error(exc) from exc

    def dump_session(self, scope: DumpScope) -> DumpSession:
        return dump.session(scope.schema_name or self._database or "")

    def dump_catalog(self, scope: DumpScope, kinds: DumpObjectKinds, *, ddl: bool = True) -> DumpCatalog:
        info = self.server_info()  # its current database too: the dump session USEs the dumped one
        try:
            return dump.dump_catalog(self._conn, info.current_database, scope, kinds, info.version, ddl=ddl)
        except mysql.connector.Error as exc:
            raise self.translate_error(exc) from exc

    def schema_snapshot(self) -> SchemaSnapshot:
        return mysql_schema.build_snapshot(self._conn)

    def close(self) -> None:
        try:
            self._conn.close()
        except (mysql.connector.Error, OSError):
            self._conn.shutdown()


_CLIENT_TIMEOUTS = (mysql.connector.errors.ReadTimeoutError, mysql.connector.errors.WriteTimeoutError)


def _is_lost(exc: mysql.connector.Error) -> bool:
    # errno -1 OperationalError: "MySQL Connection not available" (the socket is already gone)
    return (
        exc.errno in _CONNECTION_LOST_ERRNOS
        or (isinstance(exc, mysql.connector.errors.OperationalError) and exc.errno == -1)
        or isinstance(exc, _CLIENT_TIMEOUTS)
    )


def _may_change_status(exc: mysql.connector.Error) -> bool:
    """A server error (it carries a SQLSTATE) other than a parse error may have ended the transaction."""
    return bool(exc.sqlstate) and exc.errno not in _PARSE_ERRNOS and not _is_lost(exc)


def _enable_keepalive(conn: MySQLConnection) -> None:
    """TCP keepalive as for PostgreSQL; the driver has no option for it, so it is set on its socket, best effort."""
    sock = getattr(getattr(conn, "_socket", None), "sock", None)
    if sock is None:
        logger.debug("MySQL socket not reachable; TCP keepalive not set")
        return
    enable_keepalive(sock)


def _login_failed(exc: mysql.connector.Error) -> bool:
    return exc.errno in _LOGIN_FAILED_ERRNOS or "caching_sha2_password" in str(exc)


class MysqlAdapter(DatabaseAdapter):
    engine = Engine.MYSQL
    url_schemes = URL_SCHEMES
    default_port = 3306
    dialect = Dialect.MYSQL
    # one connection sees every database; KILL QUERY keeps the transaction
    capabilities = AdapterCapabilities(row_editing=True, run_file=True, dump=True, copy_data=True)

    def parse_url(self, url: str) -> ParsedUrl:
        return parse_mysql_url(url)

    def open(
        self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None, database: str | None,
        *, agent: bool = False,
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
            if settings := session_settings(profile.options):  # R-03a-F2: quoted, never pasted in by the driver
                try:
                    conn.cmd_query(settings)
                except mysql.connector.Error as exc:
                    message = f"Could not apply the time_zone / sql_mode connection options: {exc}"
                    raise ConnectionFailed(message) from exc
            if profile.read_only:
                # Last: nothing sent at connect may undo it, and the server enforces READ ONLY for writes hidden in
                # functions or procedures too.
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
