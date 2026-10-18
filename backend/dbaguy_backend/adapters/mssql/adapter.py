"""SQL Server through pymssql's low-level `_mssql` API. Batches are sent verbatim (no driver parameters, so `%` is never
touched) and streamed with pymssql's row iterator.

- A phase lock decides how a cancel reaches the statement: across threads only while the worker waits for the server
  (EXECUTING); otherwise it only sets a flag, and the worker stops before its next row or never sends the batch.
- `SET IMPLICIT_TRANSACTIONS` gives manual commit. The transaction state (@@TRANCOUNT, XACT_STATE(), the mode and the
  database) is cached and re-read in one round trip only when it may have changed and nothing is unread.
- Messages (PRINT, RAISERROR ≤ 10) arrive live through `set_msghandler`; DbaGuy's own queries never pass theirs on.
- Column types come from `sys.dm_exec_describe_first_result_set` for the first result set, guessed for the others.

Known limits (probed): a batch reports one row count (its last statement's); results after an error later in a batch
are not shown although the server goes on running the batch; SQL Server holds a result's last packet until the batch
moves on, so a cancel that arrives while those rows are awaited waits for them; @@ROWCOUNT and @@ERROR read in a later
batch report DbaGuy's state query, not the previous batch (inside one batch they are the user's).
"""

import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from enum import StrEnum
from typing import Any

from loguru import logger

from ...domain.errors import ConnectionFailed, ValidationFailed
from ...domain.execution import ColumnMeta, ErrorPosition, MessageSeverity, ServerMessage
from ...domain.metadata import ObjectDetail, ObjectNode, PathSegment, ServerInfo
from ...domain.profiles import AnyProfile, Engine, ServerProfile
from ...domain.schema import SchemaSnapshot
from ...sql.dialects import Dialect, quote_literal
from ..base import (
    AdapterCapabilities,
    DatabaseAdapter,
    DatabaseConnection,
    Endpoint,
    MessageListener,
    ParsedUrl,
    StatementCancelled,
    StatementError,
    StatementResult,
    invalid_text_error,
    transaction_open_error,
)
from . import catalog, driver, freetds, introspection
from . import schema as mssql_schema
from .settings import CONNECT_TIMEOUT_S, URL_SCHEMES, connect_params, parse_mssql_url
from .types import ColumnKind, convert_row, described_kind, guessed_kinds

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
# A cross-thread cancel goes out only once the worker has waited this long in EXECUTING. pymssql's cancel() is
# dbcancel(): it sends the attention and then reads until the server acknowledges it. While the worker is blocked in a
# read, that second step returns at once and the worker reads the acknowledgement itself (the probes' WAITFOR case).
# But a statement that is answering is between two reads inside execute_query() for a few microseconds at a time, and
# a dbcancel() then reads alongside the worker: the hammer test wedged the session or killed it (about once in 500
# cancels of a 1 ms query). A statement still waiting after the grace period is in a blocking read; one that answers
# sooner leaves EXECUTING, and the flag stops it at the next phase change.
_CANCEL_GRACE_S = 0.05
SQL_VARIANT_HINT = "sql_variant values are shown as raw bytes: CAST them to read them, e.g. CAST(col AS nvarchar(4000))"
_LOGIN_HINT = (
    "SQL Server refused the login. Check the user name and password, that the login may open the database, and the "
    "SSL mode: the server may require encryption, and verify-ca / verify-full need the CA file that signed the "
    "server's certificate."
)


class _Phase(StrEnum):
    IDLE = "idle"  # no statement on the connection
    # run() has begun but the user's batch is not on the wire yet: the state read and the describe query's rows. A
    # cancel only sets the flag, and the batch is then never sent (R-03b-P4, R-03b-P5).
    STARTING = "starting"
    EXECUTING = "executing"  # the worker waits for the server: sending the describe query, execute, the next result set
    STREAMING = "streaming"  # the user's rows are being read, or wait unread (a paused page)


def _text(raw: bytes | str | None) -> str:
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")


def server_text(exc: Exception) -> str:
    """The server's message without pymssql's "DB-Lib error message 20018, severity N: …" tail; "" when there is none.
    MSSQLDatabaseException carries it in `.text` (bytes); MSSQLDriverException only in its args."""
    raw = getattr(exc, "text", None)
    if raw is None:
        first = exc.args[0] if exc.args else ""
        raw = first[1] if isinstance(first, tuple) and len(first) > 1 else str(first)
    text = _text(raw).split("DB-Lib error message", 1)[0].strip()
    return "" if text in ("", "Unknown error") else text


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
        self._columns = [
            ColumnMeta(name=name or _NO_COLUMN_NAME, type_name=kind.name, category=kind.category)
            for name, kind in zip(names, kinds)
        ]
        if any(kind.name == "sql_variant" for kind in kinds):
            self._owner.note(ServerMessage(severity=MessageSeverity.INFO, text=SQL_VARIANT_HINT))

    def _read(self, n: int) -> list[tuple]:
        rows: list[tuple] = []
        try:
            while len(rows) < n:
                if self._owner.cancel_requested:  # a plain attribute read; cancel() sets it under the owner's lock
                    self._exhausted = True
                    raise self._owner.stop_stream()
                row = next(self._iter, None)
                if row is None:
                    self._rows_done = True
                    break
                rows.append(row)
        except driver.ERRORS as exc:  # an error later in the batch; pymssql has already cancelled the rest
            self._exhausted = True
            error = self._owner.statement_error(exc)
            if not rows:
                raise error from exc
            self._rows_done, self._deferred = True, error
        return rows

    def columns(self) -> list[ColumnMeta] | None:
        return self._columns

    def fetch(self, n: int) -> list[list[Any]]:
        if self._columns is None or self._closed:
            return []
        rows, self._buffer = self._buffer[:n], self._buffer[n:]
        if len(rows) < n and not self._rows_done:
            rows += self._read(n - len(rows))
        return [convert_row(row) for row in rows]

    def affected_rows(self) -> int | None:
        count = self._owner.rows_affected()
        return count if self._columns is None and count >= 0 else None

    def next_result(self) -> bool:
        """The worker asks only once the current set is read to its end (pymssql's nextresult() would otherwise drain
        it while the phase says EXECUTING)."""
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


class MssqlConnection(DatabaseConnection):
    def __init__(
        self, conn: driver.Connection, *, spid: int, database: str | None, read_only: bool,
        state: tuple[int, int, bool],  # (@@TRANCOUNT, XACT_STATE(), IMPLICIT_TRANSACTIONS) read at open
    ) -> None:
        self._conn = conn
        self._spid = spid
        self._database = database
        self._read_only = read_only  # application-level only (spec §3.4): logged, never enforced here
        self._lock = threading.Lock()  # guards the phase fields and cancel_requested; held across a cross-thread cancel
        self._phase_changed = threading.Condition(self._lock)
        self._phase = _Phase.IDLE
        self._phase_serial = 0  # counts phase changes: a waiting cancel sees whether the worker moved on
        self._phase_at = 0.0  # time.monotonic() of the last phase change
        self.cancel_requested = False
        self._result: MssqlResult | None = None  # the open result until it is read to its end or closed
        self._listener: MessageListener | None = None
        self._pending: deque[ServerMessage] = deque(maxlen=1000)
        self._muted = False  # one of DbaGuy's own queries runs: its messages belong to no statement
        self._lost = False
        self._stale = False  # True once the cached state may be out of date: re-read when nothing is unread
        self._trancount, self._xact_state, self._implicit = state
        conn.set_msghandler(self._on_message)

    # ---- phases and cancel ----

    def _set_phase(self, phase: _Phase) -> bool:
        """Change the phase under the lock, so a cross-thread cancel in progress finishes first. Returns whether a
        cancel reached this statement. Leaving IDLE starts a statement: the flag belongs to it alone (R-03a-P4)."""
        with self._lock:
            if self._phase is _Phase.IDLE and phase is not _Phase.IDLE:
                self.cancel_requested = False
            self._enter(phase)
            return self.cancel_requested

    def _enter_executing(self) -> bool:
        """Enter EXECUTING unless a cancel already reached this statement: then nothing may be sent, and the phase stays
        flag-only, so a stop_stream() on this thread never meets a cross-thread dbcancel(). Returns whether it was
        cancelled."""
        with self._lock:
            if self.cancel_requested:
                return True
            self._enter(_Phase.EXECUTING)
            return False

    def _enter(self, phase: _Phase) -> None:
        """Caller holds the lock."""
        self._phase = phase
        self._phase_serial += 1
        self._phase_at = time.monotonic()
        self._phase_changed.notify_all()

    def end_statement(self) -> None:
        with self._lock:
            self._enter(_Phase.IDLE)
            self.cancel_requested = False
        self._result = None
        self._stale = True

    def cancel(self) -> None:
        """Any thread (spec §3.6). EXECUTING: the attention goes out across threads, which the probes show ends a
        WAITFOR in about 1 s and keeps the session, but only once the worker has waited `_CANCEL_GRACE_S` (see there).
        STARTING and STREAMING: only the flag. Rows are being read then, and a cross-thread cancel between two row
        reads can wedge the driver (research; pre-flight R-03b-P4). The worker stops before its next row, or never
        sends the batch. IDLE: nothing runs; a flag now would cancel the next statement."""
        with self._phase_changed:
            if self._phase is _Phase.IDLE:
                return
            self.cancel_requested = True
            if self._phase is not _Phase.EXECUTING:
                logger.info("SQL Server cancel flagged for SPID {} ({}): it stops before the next row or the batch",
                            self._spid, self._phase)
                return
            serial, due = self._phase_serial, self._phase_at + _CANCEL_GRACE_S
            while self._phase_serial == serial and (left := due - time.monotonic()) > 0:
                self._phase_changed.wait(left)  # releases the lock: the worker may move on meanwhile
            if self._phase_serial != serial:
                logger.info("SQL Server cancel flagged for SPID {}: the server answered within {:.0f} ms",
                            self._spid, _CANCEL_GRACE_S * 1000)
                return
            started = time.monotonic()
            try:
                self._conn.cancel()
            except Exception as exc:  # never let a cancel raise; the flag still stops the stream
                logger.warning("SQL Server cancel of SPID {} failed: {}: {}", self._spid, type(exc).__name__, exc)
                return
            logger.info("SQL Server cancel sent to SPID {} in {:.0f} ms", self._spid, (time.monotonic() - started) * 1000)

    def stop_stream(self) -> StatementCancelled:
        """Worker thread: end the statement whose cancel was flagged (or whose rows are no longer wanted)."""
        started = time.monotonic()
        try:
            self._conn.cancel()
        except driver.ERRORS as exc:  # e.g. an error the server sent before the attention: the statement ends anyway
            logger.warning("Stopping the SQL Server stream on SPID {} reported: {}", self._spid, server_text(exc) or exc)
        self.end_statement()
        logger.info("SQL Server stream on SPID {} stopped in {:.0f} ms", self._spid, (time.monotonic() - started) * 1000)
        return StatementCancelled()

    def finish_result(self, result: MssqlResult, complete: bool) -> None:
        if self._result is not result:
            return
        if not complete:
            self.stop_stream()  # an attention on this thread: the transaction and #temp tables stay (probed)
        else:
            self.end_statement()

    # ---- execution ----

    def run(self, sql: str) -> StatementResult:
        if self._result is not None:  # the worker closes every result first; never send over unread rows
            self._result.close()
        self._set_phase(_Phase.STARTING)  # from here a cancel is kept, not dropped (R-03b-P5)
        try:
            self._refresh_state()  # the describe needs the current mode; normally fresh (the worker reads it per request)
            self._stale = True
            described = self._describe(sql)  # comes back in STARTING
            if self._enter_executing():  # a cancel arrived before the batch went out: it never runs
                raise StatementCancelled()
            self._conn.execute_query(sql)  # sends the batch verbatim (no params: `%` untouched), waits for set 1
        except StatementCancelled:
            self.end_statement()
            raise
        except driver.ERRORS as exc:
            raise self.statement_error(exc) from exc
        except UnicodeEncodeError as exc:  # a lone surrogate (half an emoji) cannot be sent as UTF-8; nothing was sent
            self.end_statement()
            raise invalid_text_error(sql, exc) from exc
        except BaseException:
            self.end_statement()  # the statement is over: its flag must not reach the next one
            raise
        if self._set_phase(_Phase.STREAMING):  # it ended just as the cancel arrived
            raise self.stop_stream()
        header = self.header()
        names = [h[0] for h in header] if header else []
        kinds = [kind for _, kind in described] if described and [name for name, _ in described] == names else None
        self._result = MssqlResult(self, kinds)
        return self._result

    def next_set(self) -> bool:
        if self._enter_executing():
            raise self.stop_stream()
        try:
            more = bool(self._conn.nextresult())
        except driver.ERRORS as exc:
            raise self.statement_error(exc) from exc
        if self._set_phase(_Phase.STREAMING):
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
        an XACT_ABORT ON transaction never dies of it, unlike sp_describe_first_result_set (probed).

        Phases (R-03b-P4): the query is sent in EXECUTING, so a describe that waits (e.g. on a schema lock) stays
        cancellable across threads. Its rows are read in STARTING, flag-only, because a cross-thread cancel between
        two row reads wedged the driver in the pre-flight. Returns in STARTING.

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
                if self._enter_executing():
                    return None  # cancelled: nothing is sent, the batch neither
                sent = True
                self._conn.execute_query(query)
                self._set_phase(_Phase.STARTING)  # flag-only from here: the rows are read
                found = catalog.read_rows(self._conn)  # reads to the end: the trailing SET included (R-03b-P7)
                finished = True
            except (*driver.ERRORS, UnicodeEncodeError) as exc:
                logger.debug("Describing a SQL Server batch failed ({})", type(exc).__name__)
            finally:
                cancelled = self._set_phase(_Phase.STARTING)
                if wrapped and sent and (not finished or cancelled):
                    self._conn.execute_non_query("SET IMPLICIT_TRANSACTIONS ON")  # R-03b-P6
        if not found or any(error is not None for _, _, error in found):
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
        which pymssql then raises as the statement's error, so they are left to it. A cross-thread cancel can bring
        the cancelled statement's last messages on the cancelling thread; the listener is thread-safe. Never raises
        into the driver."""
        try:
            if severity >= 11:
                return
            if self._muted:
                logger.debug("Dropped a message from DbaGuy's own query on SPID {}: {}", self._spid, _text(text))
                return
            self.note(ServerMessage(severity=MessageSeverity.INFO, text=_text(text)))
        except Exception:
            logger.exception("SQL Server message handler failed")

    # ---- transaction state ----

    def _refresh_state(self) -> None:
        """One round trip, only after something may have changed the state and only with nothing unread."""
        if not self._stale or self._result is not None or self._lost:
            return
        with self._internal():
            try:
                row = catalog.first_row(self._conn, _STATE_QUERY)
            except driver.ERRORS as exc:
                self.translate_error(exc)  # marks a dead connection as lost
                logger.debug("Could not read the SQL Server transaction state on SPID {}: {}", self._spid, server_text(exc))
                return
        trancount, xact_state, implicit, database = row
        self._trancount, self._xact_state, self._implicit = int(trancount), int(xact_state), bool(implicit)
        self._database = database
        self._stale = False

    def in_transaction(self) -> bool:
        self._refresh_state()
        if self._lost:
            return False
        # Ruling 17: rows are unread, so no query: in manual mode a result long enough to pause comes from a table,
        # and reading a table opens an implicit transaction.
        return self._trancount > 0 or (self._implicit and self._result is not None)

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
        """Commit, rollback, auto-commit and state queries never report "cancelled" (R-03a-P4)."""
        number = int(getattr(exc, "number", 0) or 0)
        text = server_text(exc)
        if statement and self.cancel_requested:
            return StatementCancelled()
        severity = int(getattr(exc, "severity", 0) or 0)
        if self._lost or number in _CONNECTION_LOST_NUMBERS or severity >= _FATAL_SEVERITY or (number == 0 and not self._alive()):
            self._lost = True
            return StatementError(
                "Connection to the server was lost" + (f": {text}" if text else ""), code="connection_lost"
            )
        if number == 0:
            return StatementError(text or "SQL Server ended the statement without an error message")
        line = int(getattr(exc, "line", 0) or 0)
        procedure = _text(getattr(exc, "procname", b""))
        message = text + (_PROCEDURE_LINE.format(procedure=procedure, line=line) if procedure else "")
        position = ErrorPosition(line=line, column=1) if line > 0 and not procedure else None
        return StatementError(message, code=str(number), position=position)

    def _alive(self) -> bool:
        """pymssql reports a dead connection as error 0 "Unknown error" and keeps `connected` True: ask once."""
        with self._internal():
            try:
                self._conn.execute_non_query("SELECT 1")
                return True
            except driver.ERRORS:
                return False

    # ---- metadata ----

    def _catalog_call[T](self, read: Callable[[], T]) -> T:
        with self._internal():
            try:
                return read()
            except driver.ERRORS as exc:
                raise self.translate_error(exc) from exc

    def server_info(self) -> ServerInfo:
        version, database, user = self._catalog_call(lambda: catalog.first_row(self._conn, _SERVER_INFO))
        self._database = database
        first_line = (version or "").strip().split("\n", 1)[0].strip()  # @@VERSION spans several lines
        return ServerInfo(engine=Engine.MSSQL, version=first_line, current_database=database, current_user=user)

    def current_database(self) -> str | None:
        return self._database

    def _check_database(self, path: list[PathSegment]) -> None:
        if path and path[0].name != self._database:
            raise ValidationFailed(f"Connection is bound to database '{self._database}', not '{path[0].name}'")

    def list_children(self, path: list[PathSegment]) -> list[ObjectNode]:
        self._check_database(path)
        return self._catalog_call(lambda: introspection.list_children(self._conn, path))

    def object_detail(self, path: list[PathSegment]) -> ObjectDetail:
        self._check_database(path)
        return self._catalog_call(lambda: introspection.object_detail(self._conn, path))

    def schema_snapshot(self) -> SchemaSnapshot:
        return self._catalog_call(lambda: mssql_schema.build_snapshot(self._conn))

    def close(self) -> None:
        with suppress(*driver.ERRORS):
            self._conn.close()


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
    capabilities = AdapterCapabilities(per_database_connection=True, cancel_aborts_transaction=False, server_read_only=False)

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
        try:
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
            state=(int(trancount), int(xact), bool(implicit)),
        )
