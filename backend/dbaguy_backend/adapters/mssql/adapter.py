"""SQL Server through pymssql's low-level `_mssql` API. Batches are sent verbatim (no driver parameters, so `%` is never
touched) and streamed with pymssql's row iterator.

- How a cancel reaches the statement (flag, the wait callback, the message handler, dropping the connection as the
  last resort; never from another thread) is `cancel.CancelControl`'s job; this module tells it which driver call is in
  progress.
- `SET IMPLICIT_TRANSACTIONS` gives manual commit. The transaction state (@@TRANCOUNT, XACT_STATE(), the mode and the
  database) is cached and re-read in one round trip only when it may have changed and nothing is unread.
- Messages (PRINT, RAISERROR ≤ 10) arrive live through `set_msghandler`; DbaGuy's own queries never pass theirs on.
- Column types come from `sys.dm_exec_describe_first_result_set` for the first result set, guessed for the others.

Known limits (probed): a batch reports one row count (its last statement's); results after an error later in a batch
are not shown although the server goes on running the batch; @@ROWCOUNT and @@ERROR read in a later batch report
DbaGuy's state query, not the previous batch (inside one batch they are the user's); cancelling a batch that keeps the
connection busy without sending messages (a NOCOUNT OFF loop) drops the connection after about a second, because the
driver offers no safe moment to send the cancel (see `cancel`).
"""

import re
import socket
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from typing import Any

from loguru import logger

from ...domain.column_category import ColumnCategory
from ...domain.edits import EditTarget
from ...domain.errors import ConnectionFailed, QueryFailed, ValidationFailed
from ...domain.execution import ColumnMeta, ErrorPosition, MessageSeverity, ServerMessage
from ...domain.metadata import ObjectDetail, ObjectNode, PathSegment, ServerInfo
from ...domain.profiles import AnyProfile, Engine, ServerProfile
from ...domain.schema import SchemaSnapshot
from ...sql.dialects import Dialect, quote_literal
from ...sql.lexer import TokenKind, scan
from ..base import (
    AdapterCapabilities,
    DatabaseAdapter,
    DatabaseConnection,
    Endpoint,
    MessageListener,
    ParsedUrl,
    RowEditing,
    RunOptions,
    StatementCancelled,
    StatementError,
    StatementResult,
    invalid_text_error,
    transaction_open_error,
)
from . import catalog, driver, editing, freetds, introspection
from . import schema as mssql_schema
from .cancel import Call, CallKind, CancelControl, CancelledWhileWaiting, CancelPending
from .settings import CONNECT_TIMEOUT_S, URL_SCHEMES, connect_params, parse_mssql_url
from .types import ColumnKind, described_kind, guessed_kinds, row_converter

_PROCEDURE_LINE = "\n(in {procedure}, line {line})"
_CONNECTION_LOST_NUMBERS = frozenset({20003, 20004, 20006, 20009, 20047})  # DB-Lib; rarely seen (severities < 11)
_FATAL_SEVERITY = 20  # SQL Server closes the connection after an error of severity 20-25
_DESCRIBE_LIMIT = 64 * 1024  # longer batches (scripts) are not described: compiling them twice costs more than a header
_TYPE_PEEK = 100  # rows read ahead to guess the column types of a result set the describe query did not cover
_OPEN_QUERY = "SELECT @@SPID, @@TRANCOUNT, XACT_STATE(), @@OPTIONS & 2, DB_NAME()"
_STATE_QUERY = "SELECT @@TRANCOUNT, XACT_STATE(), @@OPTIONS & 2, DB_NAME()"  # @@OPTIONS bit 2: IMPLICIT_TRANSACTIONS
_SERVER_INFO = "SELECT @@VERSION, DB_NAME(), SUSER_SNAME()"
_COMMIT = "WHILE @@TRANCOUNT > 0 COMMIT TRANSACTION"  # every nesting level (Ruling 18)
_ROLLBACK = "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION"
_NO_COLUMN_NAME = "(No column name)"
_TCP_CHECK_S = 3
# A top-level FOR JSON / FOR XML result arrives as rows of about 2,000 characters in one column with this name; SSMS
# joins them into one value, and so does the grid.
_JOINED_COLUMNS = {
    "JSON_F52E2B61-18A1-11d1-B105-00805F49916B": ColumnKind("json", ColumnCategory.JSON),
    "XML_F52E2B61-18A1-11d1-B105-00805F49916B": ColumnKind("xml", ColumnCategory.TEXT),
}
# Words after which the cached transaction state cannot be trusted while rows wait unread (a paused page).
_TRANSACTION_WORDS = frozenset({"TRAN", "TRANSACTION", "COMMIT", "ROLLBACK", "IMPLICIT_TRANSACTIONS", "ANSI_DEFAULTS"})
_TRANSACTION_HINT = re.compile("|".join(_TRANSACTION_WORDS), re.IGNORECASE)
SQL_VARIANT_HINT = "sql_variant values are shown as raw bytes: CAST them to read them, e.g. CAST(col AS nvarchar(4000))"
# Metadata reads (R-03b-F3). Catalog views read under READ UNCOMMITTED never wait for another session's uncommitted DDL
# (an ALTER in a manual-commit tab would otherwise hang the tree and completion until its rollback). The functions that
# still take locks (OBJECT_ID, OBJECT_DEFINITION, COL_NAME, …) give up after the lock timeout; XACT_ABORT ends the
# batch at the first such timeout instead of letting each of its queries wait in turn (probed: 6 queries, 6 timeouts).
_METADATA_SESSION = "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED; SET LOCK_TIMEOUT 5000; SET XACT_ABORT ON"
_LOCK_TIMEOUT = 1222
LOCKED_MESSAGE = (
    "This object is locked by an uncommitted change in another session (for example an open ALTER). Commit or roll "
    "back that change, then refresh."
)
_LOGIN_HINT = (
    "SQL Server refused the login. Check the user name and password, that the login may open the database, and the "
    "SSL mode: the server may require encryption, and verify-ca / verify-full need the CA file that signed the "
    "server's certificate."
)


def _text(raw: bytes | str | None) -> str:
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")


def server_text(exc: Exception) -> str:
    """The server's message without pymssql's "DB-Lib error message 20018, severity N: …" tail; "" when there is none.
    Read from `exc.args` only: pymssql raises MSSQLDatabaseException(((number, message_bytes),)), copying the message
    into its args, and MSSQLDriverException carries just the text.

    Never from `.text` (nor `.message`, `.srvname`, `.procname`), ruling G14: those are C char* properties. On an
    exception pymssql did not fill in (one built in Python) they are NULL and reading one crashes the whole process;
    on one it raised they point into the connection's message buffer, which a later message overwrites and closing the
    connection frees."""
    first = exc.args[0] if exc.args else ""
    raw = first[1] if isinstance(first, tuple) and len(first) > 1 else str(first)
    text = _text(raw).split("DB-Lib error message", 1)[0].strip()
    return "" if text in ("", "Unknown error") else text


def _mentions_transaction(sql: str) -> bool:
    """Whether the batch has a transaction word outside strings and comments. The regex spares the lexer most batches;
    a script over _DESCRIBE_LIMIT is not lexed (a second or so per megabyte): a mention anywhere counts."""
    if not _TRANSACTION_HINT.search(sql):
        return False
    if len(sql) > _DESCRIBE_LIMIT:
        return True
    return any(t.kind is TokenKind.WORD and t.text.upper() in _TRANSACTION_WORDS for t in scan(sql, Dialect.TSQL))


class MssqlResult(StatementResult):
    """The result sets of one batch, read on the worker thread."""

    def __init__(self, owner: "MssqlConnection", described: list[ColumnKind] | None) -> None:
        self._owner = owner
        self._closed = False
        self._exhausted = False  # every result set read, or the batch failed
        # An error that came while rows were read (a procedure's RAISERROR right after its SELECT arrives with the
        # read that ends the set): those rows are returned first, the error with the next call.
        self._deferred: StatementError | None = None
        self._open_set(described)

    def _open_set(self, described: list[ColumnKind] | None) -> None:
        header = self._owner.header()
        self._buffer: list[tuple] = []
        self._rows_done = header is None
        if header is None:
            self._columns = None
            return
        self._iter = self._owner.rows()
        names = [h[0] for h in header]
        if described is not None and len(described) == len(names):  # run() already matched the described names
            kinds = described
        else:
            self._buffer = self._read(_TYPE_PEEK)
            kinds = guessed_kinds(header, self._buffer)
        if len(names) == 1 and names[0] in _JOINED_COLUMNS:
            kinds = [_JOINED_COLUMNS[names[0]]]
            self._join_fragments()
        self._convert = row_converter(kinds)
        self._columns = [
            ColumnMeta(name=name or _NO_COLUMN_NAME, type_name=kind.name, category=kind.category)
            for name, kind in zip(names, kinds)
        ]
        if any(kind.name == "sql_variant" for kind in kinds):
            self._owner.note(ServerMessage(severity=MessageSeverity.INFO, text=SQL_VARIANT_HINT))

    def _join_fragments(self) -> None:
        while not self._rows_done:
            self._buffer += self._read(1000)
        self._buffer = [("".join(fragment or "" for (fragment,) in self._buffer),)] if self._buffer else []

    def _read(self, n: int) -> list[tuple]:
        owner = self._owner
        rows: list[tuple] = []
        ended = False
        try:
            with owner.row_call() as call:
                while len(rows) < n and not owner.cancel_requested:  # a plain attribute read between rows
                    row = next(self._iter, None)
                    if row is None:
                        ended = True
                        break
                    rows.append(row)
                    call.rows += 1  # the watcher sees a read that makes no progress
        except driver.ERRORS as exc:  # an error later in the batch; pymssql has already cancelled the rest
            self._exhausted = True
            error = owner.statement_error(exc)
            if not rows or isinstance(error, StatementCancelled) or error.code == "connection_lost":
                raise error from exc  # a cancel or a lost connection never looks like the end of the rows
            self._rows_done, self._deferred = True, error
            return rows
        if owner.cancel_requested or owner.unusable:  # also when a cancel or a shutdown ended the rows: not their end
            self._exhausted = True
            raise owner.stop_stream()
        self._rows_done = ended
        return rows

    def columns(self) -> list[ColumnMeta] | None:
        return self._columns

    def fetch(self, n: int) -> list[list[Any]]:
        if self._columns is None or self._closed:
            return []
        rows, self._buffer = self._buffer[:n], self._buffer[n:]
        if len(rows) < n and not self._rows_done:
            rows += self._read(n - len(rows))
        return [self._convert(row) for row in rows]

    def affected_rows(self) -> int | None:
        count = self._owner.rows_affected()
        return count if self._columns is None and count >= 0 else None

    def next_result(self) -> bool:
        """The worker asks only once the current set is read to its end (pymssql's nextresult() would otherwise drain
        it inside one call the flag cannot reach)."""
        if self._closed:
            return False
        while self._columns is not None and not self._rows_done and not self._exhausted:
            self._read(1000)
        if self._deferred is not None:
            error, self._deferred = self._deferred, None
            raise error
        if self._exhausted:
            return False
        try:
            more = self._owner.next_set()
        except StatementError:
            self._exhausted = True
            raise
        if not more:
            self._exhausted = True
            self._owner.end_statement()
            return False
        self._open_set(None)
        return True

    def take_messages(self) -> list[ServerMessage]:
        return self._owner.take_pending()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._owner.finish_result(self, complete=self._exhausted)


class MssqlConnection(DatabaseConnection, RowEditing):
    def __init__(
        self, conn: driver.Connection, *, spid: int, database: str | None, read_only: bool,
        state: tuple[int, int, bool],  # (@@TRANCOUNT, XACT_STATE(), IMPLICIT_TRANSACTIONS) read at open
        socket_fd: int | None = None,
    ) -> None:
        self._conn = conn
        self._spid = spid
        self._database = database
        self._read_only = read_only  # application-level only (spec §3.4): logged, never enforced here
        self._control = CancelControl(
            conn, spid, may_hold_transaction=self._may_hold_transaction, warn=self._warn_running_request,
        )
        if socket_fd is not None:
            self._control.note_socket(socket_fd)
        self._result: MssqlResult | None = None  # the open result until it is read to its end or closed
        self._listener: MessageListener | None = None
        self._pending: deque[ServerMessage] = deque(maxlen=1000)
        self._muted = False  # one of DbaGuy's own queries runs: its messages belong to no statement
        self._lost = False
        self._stale = False  # True once the cached state may be out of date: re-read when nothing is unread
        self._batch = ""  # the running batch's text
        self._transaction_words: bool | None = None  # it may begin or end a transaction; worked out when first asked
        self._trancount, self._xact_state, self._implicit = state
        self._metadata_session = False  # _METADATA_SESSION is set (on a metadata connection, before its first read)
        self._armed = False  # worker thread only: arm_cancel() began the STARTING phase; run() has not taken it over
        conn.set_msghandler(self._on_message)

    # ---- cancel ----

    @property
    def cancel_requested(self) -> bool:
        return self._control.cancel_requested

    @property
    def unusable(self) -> bool:
        return self._control.unusable

    def arm_cancel(self) -> None:
        """Worker thread, before its last cancel check (Task 6b): the statement's STARTING phase begins here instead of
        in run() (Python state under the control's lock, nothing reaches the driver), so a cancel, a repeated one or
        closing the session from now on is kept as one during STARTING is, and the batch is never sent (R-03b-P5).
        The worker has closed every result first."""
        self._control.begin_statement()
        self._armed = True

    def disarm_cancel(self) -> None:
        """Worker thread. Once run() has taken the statement over, its phase goes on (the result's own cancel handling,
        or run() ended it); otherwise it ends here, with any cancel kept in it (R-03a-P4)."""
        if self._armed:
            self._armed = False
            self._control.end_statement()

    def cancel(self) -> None:
        """Any thread (spec §3.6); see `cancel.CancelControl`."""
        self._control.cancel()

    def force_cancel(self) -> None:
        self._control.force_cancel()

    def abort(self) -> None:
        self._control.abort()

    def _may_hold_transaction(self) -> bool:
        """Whether the running batch may have a transaction to lose (R-03b-T8h): manual commit, one open before it (the
        state was read when it started), or a transaction word in its text. The watcher thread asks, while the
        worker is inside the driver and changes none of this."""
        if self._implicit or self._trancount > 0:
            return True
        if self._transaction_words is None:
            self._transaction_words = _mentions_transaction(self._batch)
        return self._transaction_words

    def _warn_running_request(self, text: str) -> None:
        self.note(ServerMessage(severity=MessageSeverity.WARNING, text=text))

    def row_call(self) -> AbstractContextManager[Call]:
        return self._control.call(CallKind.ROWS)

    def end_statement(self) -> None:
        self._control.end_statement()
        self._result = None
        self._stale = True

    def _attention(self) -> None:
        """This thread's own cancel: an attention, then everything up to its acknowledgement is read and dropped (the
        transaction and #temp tables stay, probed). A second Cancel or closing the session may drop the connection
        while the server takes its time to acknowledge."""
        if self._control.unusable:
            return
        try:
            with self._control.call(CallKind.STOP):
                self._conn.cancel()
        except driver.ERRORS as exc:  # e.g. an error the server sent before the attention: the statement ends anyway
            # The number only above DEBUG: the server's text may echo literals (spec ⑤ §5, G12)
            logger.warning("Stopping the statement on SQL Server SPID {} reported error {}", self._spid,
                           getattr(exc, "number", None) or type(exc).__name__)
            logger.debug("Stopping the statement on SQL Server SPID {} reported: {}", self._spid,
                         server_text(exc) or exc)

    def stop_stream(self, cancelled: bool = True) -> StatementError:
        """Worker thread: end the statement whose cancel was flagged (`cancelled`) or whose rows are no longer wanted.
        Returns what to raise: "cancelled", or connection_lost when the connection was dropped or killed meanwhile (a
        KILL during a flagged cancel must not read as a cancel: the server is asked, a round trip per cancel)."""
        started = time.monotonic()
        self._attention()
        lost = self._control.unusable or (cancelled and not self._alive())
        error = self._lost_error() if lost else StatementCancelled()
        self.end_statement()
        logger.info("SQL Server statement on SPID {} stopped in {:.0f} ms", self._spid, (time.monotonic() - started) * 1000)
        return error

    def finish_result(self, result: MssqlResult, complete: bool) -> None:
        if self._result is not result:
            return
        if not complete:
            self.stop_stream(cancelled=False)
        else:
            self.end_statement()

    def _lost_error(self) -> StatementError:
        self._lost = True
        if self._control.aborted is not None:
            return StatementError(self._control.aborted_message, code="connection_lost")
        return StatementError("Connection to the server was lost", code="connection_lost")

    def _check_usable(self) -> None:
        if self._control.unusable:
            raise self._lost_error()

    # ---- execution ----

    def run(self, sql: str, options: RunOptions | None = None) -> StatementResult:
        if self._result is not None:  # the worker closes every result first; never send over unread rows
            self._result.close()  # that ends its statement, and a phase armed meanwhile with it: begun again below
            self._armed = False
        if not self._armed:  # armed: the phase began at arm_cancel(), and a cancel may be kept in it already
            self._control.begin_statement()  # from here a cancel is kept, not dropped (R-03b-P5)
        self._armed = False
        try:
            self._check_usable()
            if self._control.cancel_requested:  # kept during STARTING: nothing is sent, not even the state read
                raise CancelPending()
            self._batch, self._transaction_words = sql, None
            self._refresh_state()  # the transaction state (and the describe's mode); normally fresh (read per request)
            self._stale = True
            described = self._describe(sql) if options is None or options.describe else None
            with self._control.call(CallKind.EXECUTE):  # not started when a cancel arrived first: the batch never runs
                self._conn.execute_query(sql)  # sends the batch verbatim (no params: `%` untouched), waits for set 1
        except CancelPending:
            self.end_statement()
            raise StatementCancelled() from None
        except CancelledWhileWaiting:  # the batch is sent, its first response unread (R-03b-T8e)
            raise self.stop_stream() from None
        except StatementError:
            self.end_statement()
            raise
        except driver.ERRORS as exc:
            raise self.statement_error(exc) from exc
        except UnicodeEncodeError as exc:  # a lone surrogate (half an emoji) cannot be sent as UTF-8; nothing was sent
            self.end_statement()
            raise invalid_text_error(sql, exc.start) from exc
        except BaseException:
            self.end_statement()  # the statement is over: its flag must not reach the next one
            raise
        if self._control.cancel_requested:  # it ended just as the cancel arrived
            raise self.stop_stream()
        header = self.header()
        names = [h[0] for h in header] if header else []
        kinds = [kind for _, kind in described] if described and [name for name, _ in described] == names else None
        self._result = MssqlResult(self, kinds)
        return self._result

    def next_set(self) -> bool:
        if self._control.unusable:
            raise self.stop_stream()
        try:
            with self._control.call(CallKind.NEXT_SET):
                more = bool(self._conn.nextresult())
        except CancelPending:
            raise self.stop_stream() from None
        except driver.ERRORS as exc:
            raise self.statement_error(exc) from exc
        if self._control.cancel_requested or self._control.unusable:
            raise self.stop_stream()
        return more

    def header(self) -> tuple | None:
        return self._conn.get_header()

    def rows(self) -> Iterator[tuple]:
        return self._conn.get_iterator(driver.ROW)

    def rows_affected(self) -> int:
        return self._conn.rows_affected

    def _describe(self, sql: str) -> list[tuple[str, ColumnKind]] | None:
        """The first result set's column names and types, from sys.dm_exec_describe_first_result_set, on the same
        connection, before the batch. The DMF reports a batch it cannot describe as error rows instead of raising, so
        an XACT_ABORT ON transaction never dies of it, unlike sp_describe_first_result_set (probed). Its rows are read
        outside any driver call a cancel interrupts: the flag alone stops what follows (R-03b-P4).

        In manual mode a SELECT from the DMF would open an implicit transaction, so the mode is switched off around it
        in the same round trip. Whenever the describe did not finish, or a cancel reached it (its attention may have
        stopped the wrapper before the last SET even though the rows came back), the mode is switched on again before
        anything else, so manual mode never silently becomes auto-commit (R-03b-P6)."""
        if len(sql) > _DESCRIBE_LIMIT:
            return None
        query = (
            "SELECT name, system_type_name, error_number FROM sys.dm_exec_describe_first_result_set("
            f"{quote_literal(Dialect.TSQL, sql)}, NULL, 0) ORDER BY column_ordinal"
        )
        wrapped = self._implicit
        if wrapped:
            query = f"SET IMPLICIT_TRANSACTIONS OFF; {query}; SET IMPLICIT_TRANSACTIONS ON"
        found: list[tuple] | None = None
        sent = finished = False
        with self._internal():
            try:
                with self._control.call(CallKind.EXECUTE):
                    sent = True
                    self._conn.execute_query(query)
                found = catalog.read_rows(self._conn)  # reads to the end: the trailing SET included (R-03b-P7)
                finished = True
            except CancelPending:
                return None  # nothing was sent; the batch will not be either
            except CancelledWhileWaiting:
                self._attention()
            except (*driver.ERRORS, UnicodeEncodeError) as exc:
                logger.debug("Describing a SQL Server batch failed ({})", type(exc).__name__)
            finally:
                restore = wrapped and sent and (not finished or self._control.cancel_requested)
                if restore and not self._control.unusable:
                    self._conn.execute_non_query("SET IMPLICIT_TRANSACTIONS ON")  # R-03b-P6
        # The expected shape only: SET SHOWPLAN_* answers with plans, SET NOEXEC / PARSEONLY / FMTONLY with nothing.
        if not found or any(len(row) != 3 or row[2] is not None for row in found):
            return None
        return [(name or "", described_kind(type_name)) for name, type_name, _ in found]

    # ---- messages ----

    @contextmanager
    def _internal(self) -> Iterator[None]:
        """DbaGuy's own queries (state, describe, commit, rollback, metadata): a message they cause belongs to no
        statement the user ran, so it is dropped instead of reaching this or a later request (Task 4's contract)."""
        previous, self._muted = self._muted, True
        try:
            yield
        finally:
            self._muted = previous

    def set_message_listener(self, listener: MessageListener | None) -> None:
        self._listener = listener
        if listener is not None:
            self._pending.clear()  # messages from before this request belong to no statement shown now

    def note(self, message: ServerMessage) -> None:
        listener = self._listener
        if listener is not None:
            listener(message)
        else:
            self._pending.append(message)

    def take_pending(self) -> list[ServerMessage]:
        taken = list(self._pending)
        self._pending.clear()
        return taken

    def _on_message(self, state: int, severity: int, server: bytes, procedure: bytes, line: int, text: bytes) -> None:
        """pymssql calls this on the thread inside the driver call, for every message: errors too (severity >= 11),
        which pymssql then raises as the statement's error, so they are left to it. The listener is thread-safe.
        Never raises into the driver."""
        try:
            if severity >= 11:
                return
            if self._muted:
                logger.debug("Dropped a message from DbaGuy's own query on SPID {}: {}", self._spid, _text(text))
                return
            self.note(ServerMessage(severity=MessageSeverity.INFO, text=_text(text)))
            self._control.on_message()  # a pending cancel goes out from here (see cancel.CancelControl)
        except Exception:
            logger.exception("SQL Server message handler failed")

    # ---- transaction state ----

    def _refresh_state(self) -> None:
        """One round trip, only after something may have changed the state and only with nothing unread."""
        if not self._stale or self._result is not None or self._lost or self._control.unusable:
            return
        with self._internal():
            try:
                row = catalog.first_row(self._conn, _STATE_QUERY)
            except driver.ERRORS as exc:
                self.translate_error(exc)  # marks a dead connection as lost
                logger.debug("Could not read the SQL Server transaction state on SPID {}: {}", self._spid, server_text(exc))
                return
        self._stale = False  # one try per statement, whatever the answer
        try:
            trancount, xact_state, implicit, database = row  # type: ignore[misc]
            self._trancount, self._xact_state, self._implicit = int(trancount), int(xact_state), bool(implicit)
        except (TypeError, ValueError):
            # SET NOEXEC / PARSEONLY / FMTONLY ON answer with no row, SET SHOWPLAN_* ON with a plan: keep the cache
            logger.debug("The SQL Server state query on SPID {} got {!r}; the cached state is kept", self._spid,
                         row if row is None else f"{len(row)} column(s)")
            return
        self._database = database

    def in_transaction(self) -> bool:
        self._refresh_state()
        if self._lost or self._control.unusable:
            return False
        if self._result is not None:
            # Rows wait unread, so no query. Ruling 17: in manual mode a result long enough to pause comes from a
            # table, and reading a table opens an implicit transaction. A batch with BEGIN TRAN, COMMIT and the like
            # may have changed the state the cache holds: an open transaction is assumed until the pause ends.
            if self._implicit:
                return True
            if self._transaction_words is None:
                self._transaction_words = _mentions_transaction(self._batch)
            if self._transaction_words:
                return True
        return self._trancount > 0

    def transaction_failed(self) -> bool:
        self._refresh_state()
        return not self._lost and self._trancount > 0 and self._xact_state == -1  # XACT_STATE() alone is not enough

    def autocommit(self) -> bool:
        self._refresh_state()  # @@OPTIONS also catches a typed SET IMPLICIT_TRANSACTIONS or SET ANSI_DEFAULTS
        return not self._implicit

    def set_autocommit(self, enabled: bool) -> None:
        if self.autocommit() == enabled:
            return
        if self.in_transaction():
            raise transaction_open_error()
        self._internal_statement(f"SET IMPLICIT_TRANSACTIONS {'OFF' if enabled else 'ON'}")
        self._implicit = not enabled
        logger.info("Auto-commit {} on SQL Server SPID {}", "on" if enabled else "off", self._spid)

    def commit(self) -> None:
        self._stale = True
        self._internal_statement(_COMMIT)

    def rollback(self) -> None:
        self._stale = True
        self._internal_statement(_ROLLBACK)

    def _internal_statement(self, sql: str) -> None:
        """A statement of DbaGuy's own (commit, rollback, the mode): its errors never read "cancelled" (R-03a-P4)."""
        self._check_usable()
        with self._internal():
            try:
                self._conn.execute_non_query(sql)
            except driver.ERRORS as exc:
                raise self.translate_error(exc) from exc

    # ---- errors ----

    def statement_error(self, exc: Exception) -> StatementError:
        """The error of the user's statement (run, fetch, next result): the only place a cancel can show."""
        error = self.translate_error(exc, statement=True)
        self.end_statement()
        return error

    def translate_error(self, exc: Exception, statement: bool = False) -> StatementError:
        """Commit, rollback, auto-commit and state queries never report "cancelled" (R-03a-P4). A lost or dropped
        connection is checked first: a KILL or a shutdown during a flagged cancel reads connection_lost."""
        number = int(getattr(exc, "number", 0) or 0)
        text = server_text(exc)
        severity = int(getattr(exc, "severity", 0) or 0)
        if self._control.unusable:
            return self._lost_error()
        if self._lost or number in _CONNECTION_LOST_NUMBERS or severity >= _FATAL_SEVERITY or (number == 0 and not self._alive()):
            self._lost = True
            return StatementError(
                "Connection to the server was lost" + (f": {text}" if text else ""), code="connection_lost"
            )
        if statement and self._control.cancel_requested:
            return StatementCancelled()
        if number == 0:
            return StatementError(text or "SQL Server ended the statement without an error message")
        line = int(getattr(exc, "line", 0) or 0)
        # Safe only below the `number == 0` return (G14): pymssql sets `number` with its char* fields, and an exception
        # built in Python has number 0 and a NULL `procname`, whose read would crash the process
        procedure = _text(getattr(exc, "procname", b""))
        message = text + (_PROCEDURE_LINE.format(procedure=procedure, line=line) if procedure else "")
        position = ErrorPosition(line=line, column=1) if line > 0 and not procedure else None
        return StatementError(message, code=str(number), position=position)

    def _alive(self) -> bool:
        """pymssql reports a dead connection as error 0 "Unknown error" and keeps `connected` True: ask once. Runs
        outside any driver call a cancel interrupts, so a cancel arriving now only sets the flag."""
        if self._control.unusable:
            return False
        with self._internal():
            try:
                self._conn.execute_non_query("SELECT 1")
                return True
            except driver.ERRORS:
                return False

    # ---- metadata ----

    def _catalog_call[T](self, read: Callable[[], T]) -> T:
        self._check_usable()
        with self._internal():
            try:
                return read()
            except driver.ERRORS as exc:
                raise self.translate_error(exc) from exc

    def server_info(self) -> ServerInfo:
        row = self._catalog_call(lambda: catalog.first_row(self._conn, _SERVER_INFO))
        version, database, user = row if row is not None and len(row) == 3 else ("", self._database, None)
        self._database = database
        first_line = (version or "").strip().split("\n", 1)[0].strip()  # @@VERSION spans several lines
        return ServerInfo(engine=Engine.MSSQL, version=first_line, current_database=database, current_user=user)

    def current_database(self) -> str | None:
        return self._database

    def _check_database(self, path: list[PathSegment]) -> None:
        if path and path[0].name != self._database:
            raise ValidationFailed(f"Connection is bound to database '{self._database}', not '{path[0].name}'")

    def _metadata_call[T](self, read: Callable[[], T]) -> T:
        """The object tree, details and the completion snapshot. Only MetadataService's dedicated connections run them,
        so the session settings they need are set on the connection once and never meet a user's statement. A lock
        timeout reads as what it is, without a reconnect (QueryFailed is not retried)."""
        if not self._metadata_session:
            self._catalog_call(lambda: self._conn.execute_non_query(_METADATA_SESSION))
            self._metadata_session = True
        try:
            return self._catalog_call(read)
        except StatementError as exc:
            if exc.code == str(_LOCK_TIMEOUT):
                logger.info("A SQL Server metadata read on SPID {} timed out on a lock held by another session", self._spid)
                raise QueryFailed(LOCKED_MESSAGE) from exc
            raise

    def list_children(self, path: list[PathSegment]) -> list[ObjectNode]:
        self._check_database(path)
        return self._metadata_call(lambda: introspection.list_children(self._conn, path))

    def object_detail(self, path: list[PathSegment]) -> ObjectDetail:
        self._check_database(path)
        return self._metadata_call(lambda: introspection.object_detail(self._conn, path))

    def edit_target(self, path: list[PathSegment]) -> EditTarget:
        self._check_database(path)
        return self._metadata_call(lambda: editing.edit_target(self._conn, path))

    def schema_snapshot(self) -> SchemaSnapshot:
        return self._metadata_call(lambda: mssql_schema.build_snapshot(self._conn))

    def close(self) -> None:
        """Frees the connection only once no thread is inside the driver for it (R-03b-F5). The worker closes its own
        connection, so normally none is; a reader on another thread gets the socket shut down first, so its read ends,
        and the guard waits for it or leaks the connection."""
        if self._conn.busy:
            self._control.drop("closed while another thread was inside the driver")
        with suppress(*driver.ERRORS):
            if not self._conn.close():
                logger.error("SQL Server SPID {} was left open: a driver call never returned", self._spid)


def _login_failure(exc: Exception, endpoint: Endpoint, section: str) -> str:
    """What a failed login tells the user. pymssql keeps login-error state in process globals and never clears the
    number or severity, so only a server sentence ("Login failed for user 'x'.") is trusted, and only when present
    (Review Focus 5). FreeTDS names the freetds.conf section, never the host: the section is replaced by the address."""
    try:
        socket.create_connection((endpoint.host, endpoint.port), timeout=_TCP_CHECK_S).close()
    except OSError as os_error:
        return f"Could not reach SQL Server at {endpoint.host}:{endpoint.port}: {os_error}"
    text = server_text(exc).replace(section, f"{endpoint.host}:{endpoint.port}")
    if "Login failed" in text:
        return f"{text}\n{_LOGIN_HINT}"
    return _LOGIN_HINT


class MssqlAdapter(DatabaseAdapter):
    engine = Engine.MSSQL
    url_schemes = URL_SCHEMES
    default_port = 1433
    dialect = Dialect.TSQL
    # One connection works in one database; an attention keeps the transaction; read-only is DbaGuy's alone (spec §3.4).
    capabilities = AdapterCapabilities(
        per_database_connection=True, cancel_aborts_transaction=False, server_read_only=False, row_editing=True,
        run_file=True,
    )

    def parse_url(self, url: str) -> ParsedUrl:
        return parse_mssql_url(url)

    def open(
        self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None, database: str | None
    ) -> DatabaseConnection:
        if not isinstance(profile, ServerProfile) or endpoint is None:
            raise ValidationFailed("Not a SQL Server profile")
        try:
            config = freetds.active()  # first: writes DbaGuy's freetds.conf and sets FREETDSCONF before any login (R-03b-P9)
        except OSError as exc:
            raise ConnectionFailed(f"Cannot prepare DbaGuy's freetds.conf: {exc.strerror or type(exc).__name__}") from exc
        params = connect_params(profile, endpoint, password, database)
        kwargs = dict(params.kwargs)
        if params.tds_section:
            kwargs["server"] = config.section_for(params.tds_section)
        try:
            conn = driver.connect(CONNECT_TIMEOUT_S, **kwargs)
        except driver.ERRORS as exc:
            logger.debug("SQL Server login to {}:{} failed: {}", endpoint.host, endpoint.port, type(exc).__name__)
            raise ConnectionFailed(_login_failure(exc, endpoint, kwargs["server"])) from exc
        except (TypeError, ValueError) as exc:
            raise ConnectionFailed(f"Invalid SQL Server connection setting: {exc}") from exc
        sockets: list[int] = []  # the first query names the connection's socket (R-03b-T8d)
        try:
            with driver.wait_hook(sockets.append):
                spid, trancount, xact, implicit, db = catalog.first_row(conn, _OPEN_QUERY)
        except driver.ERRORS as exc:
            with suppress(*driver.ERRORS):
                conn.close()
            raise ConnectionFailed(f"SQL Server accepted the login but did not answer: {server_text(exc) or exc}") from exc
        except BaseException:
            with suppress(*driver.ERRORS):
                conn.close()
            raise
        logger.info(
            "Connected to SQL Server {}:{} db={} tls={} spid={}{}",
            endpoint.tls_server_name or endpoint.host, endpoint.port, db, profile.ssl.mode, spid,
            " read_only=True (enforced by DbaGuy only)" if profile.read_only else "",
        )
        return MssqlConnection(
            conn, spid=int(spid), database=db, read_only=profile.read_only,
            state=(int(trancount), int(xact), bool(implicit)), socket_fd=sockets[0] if sockets else None,
        )
