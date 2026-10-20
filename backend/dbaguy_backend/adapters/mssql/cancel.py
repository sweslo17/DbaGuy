"""How a cancel reaches a SQL Server statement (spec §3.6; rulings R-03b-P4, P5, T8d, T8e, T8h, T8i, F1, F2, F6).

The worker thread runs the statement. cancel() comes from any thread: the user's Cancel, a timeout, or closing the tab.
pymssql has one way to stop a statement, `cancel()`, which is FreeTDS's dbcancel(): it sends an attention and reads up
to the acknowledgement. Only the worker ever enters FreeTDS for its connection (R-03b-F6; `driver.GuardedConnection`
refuses any other thread). A dbcancel() from another thread wedged the worker on a condition variable when it was
between two reads, and freed the blob buffers and result info pymssql's get_row was still converting when the worker
was not blocked at all (a big value fully arrived, the next rows already buffered): corrupt values, SystemError, and
a native crash only an allocator's luck away. So a cancel reaches the statement only on the worker's own thread:
- Waiting for a batch's first response (a WAITFOR, a long query before its first row, a lock wait, the describe): the
  wait callback polls the socket and raises CancelledWhileWaiting; the worker sends the cancel itself (R-03b-T8e).
- Starting (state read, describe rows), between rows, between result sets, a paused page: only the flag. The worker
  stops before its next row or never sends the batch (R-03b-P4, P5).
- A message (PRINT, RAISERROR … WITH NOWAIT) during a driver call: the worker sends the cancel from its message handler,
  inside its own driver call, where FreeTDS expects it.
- None of these within WEDGE_S (a read blocked mid-result on a row lock, the wait for the next result set, a NOCOUNT OFF
  loop that streams row counts): the call escalates (`_escalate`). The bytes of one big value count as progress: the
  worker sees the flag once the row is complete.
- A repeated Cancel escalates too, once the call is stuck: a cancel sent for it at least WEDGE_S ago has not ended it
  (the server is still undoing the statement, R-03b-F1), or a row read flagged at least WEDGE_S ago still receives
  one big value (R-03b-F2). Earlier, a repeated Cancel is a double click and acts as a plain cancel (R-03b-T8i).
Escalating, the one warn-then-force rule: when no transaction can be open, the socket is shut down. When one may be
(manual commit, @@TRANCOUNT > 0 before the batch, a transaction word in it), a Cancel must never roll the user's work
back unannounced (R-03b-T8h): the request gets one warning and the cancel waits on; the next Cancel (not the rest of
a double click, DOUBLE_CLICK_S) or closing the session drops the connection. The socket is shut down, never closed
from another thread: the worker's read ends with an error on its own thread.
A shut-down connection reports connection_lost; the server ends the session and rolls back its transaction.
"""

import os
import select
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum

from loguru import logger

from . import driver

GRACE_S = 0.05  # a row read without a new row or new bytes this long: the flag cannot reach the worker
_ACTIVE_S = 0.25  # bytes this recent at the deadline: the server keeps sending (row counts, data), not waiting
# A cancel that found no safe moment in this time escalates (R-03b-T8h); a repeated Cancel forces only this long after
# the cancel went out or was flagged, so a double click never does (R-03b-T8i).
WEDGE_S = 1.0
# A Cancel this soon after the warning is the rest of a double click (macOS's default interval): the user cannot have
# read the warning yet, so it does not drop the connection.
DOUBLE_CLICK_S = 0.5
_POLL_MS = 50  # the wait callback's polling step: how soon it sees the flag
_WATCH_S = 0.01  # how often the watcher looks again
# The socket's byte counter, read only to see progress (macOS <netinet/tcp.h>: struct tcp_connection_info, tcpi_rxbytes
# at offset 88). It never decides anything but "progress" and which text a drop or a warning gives.
_TCP_CONNECTION_INFO = 0x106
_TCP_INFO_SIZE, _RXBYTES_AT = 112, 88
ABORTED_MESSAGE = (
    "The connection was dropped to stop the statement; the server rolled back any open transaction. Reconnect to go on"
)
# In auto-commit mode with no transaction open before the batch: its completed statements are committed already.
BUSY_DROPPED_MESSAGE = (
    "The connection was dropped to stop the batch: it kept streaming row counts, so it could not be cancelled safely. "
    "Statements it had already completed stay committed (auto-commit); the statement that was running, and any "
    "transaction the batch itself opened, were rolled back. Reconnect to go on. SET NOCOUNT ON makes such batches "
    "cancellable."
)
BUSY_WARNING = (
    "This batch cannot be stopped safely while it streams row counts. Press Cancel again to drop the connection — the "
    "server rolls back the open transaction. (SET NOCOUNT ON makes such batches cancellable.)"
)
# The same two texts when what kept the connection busy was data: one big value a repeated Cancel gives up on, or a row
# read on a platform without the socket's byte counter (no way to tell data from waiting there).
BUSY_DATA_DROPPED_MESSAGE = (
    "The connection was dropped to stop the batch: the server kept sending data, so it could not be cancelled safely. "
    "Statements it had already completed stay committed (auto-commit); the statement that was running, and any "
    "transaction the batch itself opened, were rolled back. Reconnect to go on."
)
BUSY_DATA_WARNING = (
    "This batch cannot be stopped safely while the server keeps sending data. Press Cancel again to drop the "
    "connection — the server rolls back the open transaction."
)
# A repeated Cancel while the cancel that went out has not ended the call: the server acknowledges a cancel only once it
# has undone the statement, which takes about as long as the statement had run.
UNDOING_WARNING = (
    "SQL Server is still undoing the cancelled statement. Press Cancel again to drop the connection — the server rolls "
    "back the open transaction."
)
# The server sends nothing: the statement waits inside a result where a cancel cannot reach it (a row lock in the
# middle of a result, a WAITFOR or a slow statement between two result sets).
WAITING_DROPPED_MESSAGE = (
    "The connection was dropped to stop the batch: it was waiting inside a result (for example on a lock), where a "
    "cancel cannot reach it. Statements it had already completed stay committed (auto-commit); the statement that was "
    "running, and any transaction the batch itself opened, were rolled back. Reconnect to go on."
)
WAITING_WARNING = (
    "This batch is waiting inside a result (for example on a lock), where a cancel cannot reach it. Press Cancel again "
    "to drop the connection — the server rolls back the open transaction."
)
_BUSY_REASON = "the cancel could not reach the statement and no transaction can be open"
_FORCED_REASON = "forced by a second cancel"


@dataclass(frozen=True)
class _Escalation:
    """A cancel that cannot stop the call safely: what the statement reports if the connection is dropped (no
    transaction can be open), and what the request is told instead when one may be (R-03b-T8h)."""

    situation: str  # for the log
    dropped: str
    warning: str


_ROW_COUNTS = _Escalation("the batch keeps streaming row counts", BUSY_DROPPED_MESSAGE, BUSY_WARNING)
_DATA = _Escalation("the server keeps sending data", BUSY_DATA_DROPPED_MESSAGE, BUSY_DATA_WARNING)
_UNDOING = _Escalation("the server has not acknowledged the cancel yet", ABORTED_MESSAGE, UNDOING_WARNING)
_WAITING = _Escalation("the statement waits inside a result", WAITING_DROPPED_MESSAGE, WAITING_WARNING)


class CallKind(StrEnum):
    EXECUTE = "execute"  # execute_query(): the wait callback covers the wait for the first response
    NEXT_SET = "next result set"  # nextresult(): waits for the next result set
    ROWS = "rows"  # row reads; progress is the row counter
    STOP = "stop"  # the worker's own dbcancel(), reading up to the acknowledgement


class CancelledWhileWaiting(Exception):
    """Raised from the wait callback: the batch is sent and its first response unread; the worker cancels it."""


class CancelPending(Exception):
    """A cancel reached the statement before this driver call: it must not start."""


@dataclass(frozen=True)
class _Socket:
    fd: int
    local: object
    peer: object


def _identify(fd: int) -> _Socket | None:
    try:
        with socket.socket(fileno=os.dup(fd)) as sock:
            return _Socket(fd, sock.getsockname(), sock.getpeername())
    except OSError:
        return None


def _received(fd: int) -> int | None:
    """The bytes the socket has received so far; None where the platform does not tell."""
    try:
        with socket.socket(fileno=os.dup(fd)) as sock:
            info = sock.getsockopt(socket.IPPROTO_TCP, _TCP_CONNECTION_INFO, _TCP_INFO_SIZE)
    except OSError:
        return None
    if len(info) < _RXBYTES_AT + 8:
        return None
    return struct.unpack_from("Q", info, _RXBYTES_AT)[0]


@dataclass(eq=False)
class Call:
    """One driver call a cancel may have to interrupt. `rows` is bumped by the worker for every row it reads."""

    kind: CallKind
    started: float = field(default_factory=time.monotonic)
    thread: int = field(default_factory=threading.get_ident)
    answered_at: float | None = None  # EXECUTE: the wait callback returned, the first response is in
    rows: int = 0
    ended_at: float | None = None
    flagged_at: float | None = None  # when the cancel flag first reached this call
    watched: bool = False  # a watcher thread looks after this call
    attention_at: float | None = None  # the worker sent a cancel from its message handler during this call


class CancelControl:
    """The cancel state of one connection: the flag, the driver call in progress, the socket, and the watcher."""

    def __init__(
        self, conn: driver.Connection, spid: int, *, may_hold_transaction: Callable[[], bool] = lambda: True,
        warn: Callable[[str], None] = lambda text: None,
    ) -> None:
        """`may_hold_transaction`: whether the running batch may have a transaction to lose (called off the worker
        thread while the worker is inside the driver). `warn`: tells the running request (thread-safe)."""
        self._conn = conn
        self._spid = spid
        self._may_hold_transaction = may_hold_transaction
        self._warn = warn
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._in_statement = False
        self.cancel_requested = False  # read without the lock between rows: a plain attribute
        # The statement's request was told it cannot be stopped safely (R-03b-T8h): the next Cancel drops the connection
        self._warned_at: float | None = None
        self._call: Call | None = None
        self._socket: _Socket | None = None
        self.aborted: str | None = None  # why the socket was shut down
        self._aborted_message = ABORTED_MESSAGE  # what the statement it stopped reports

    # ---- worker thread ----

    @property
    def unusable(self) -> bool:
        return self.aborted is not None

    @property
    def aborted_message(self) -> str:
        return self._aborted_message

    def begin_statement(self) -> None:
        """The flag belongs to one statement (R-03a-P4): from here a cancel is kept, not dropped (R-03b-P5)."""
        with self._lock:
            self._in_statement = True
            self.cancel_requested = False
            self._warned_at = None

    def end_statement(self) -> None:
        with self._lock:
            self._in_statement = False
            self.cancel_requested = False
            self._warned_at = None
            self._changed.notify_all()

    def note_socket(self, fd: int) -> None:
        """Record the connection's socket the first time it is seen (and whenever the number changes)."""
        if self._socket is None or self._socket.fd != fd:
            found = _identify(fd)
            with self._lock:
                self._socket = found

    @contextmanager
    def call(self, kind: CallKind) -> Iterator[Call]:
        """Around a driver call a cancel may have to interrupt. EXECUTE and NEXT_SET do not start while a cancel is
        pending (CancelPending)."""
        with self._lock:
            if self.cancel_requested and kind in (CallKind.EXECUTE, CallKind.NEXT_SET):
                raise CancelPending()
            current = self._call = Call(kind)
            if self.cancel_requested:
                current.flagged_at = current.started
            self._changed.notify_all()
        try:
            if kind is CallKind.EXECUTE:
                with driver.wait_hook(lambda fd: self._wait_for_answer(current, fd)):
                    yield current
            else:
                yield current
        finally:
            self._leave(current)

    def _wait_for_answer(self, call: Call, fd: int) -> None:
        """The wait callback (worker thread, inside execute_query, the batch sent): poll the socket in steps of
        _POLL_MS and leave the wait when the flag is set (R-03b-T8e). Never calls cancel()."""
        self.note_socket(fd)
        poller = select.poll()
        poller.register(fd, select.POLLIN | select.POLLPRI | select.POLLERR | select.POLLHUP)
        while True:
            if self.cancel_requested:
                raise CancelledWhileWaiting()
            if poller.poll(_POLL_MS):
                break
        with self._lock:
            call.answered_at = time.monotonic()
            self._changed.notify_all()

    def on_message(self) -> None:
        """The message handler saw a message (worker thread, inside a driver call). With a cancel pending, send it from
        here, inside the worker's own call, the way FreeTDS expects a cancel from a message handler."""
        with self._lock:
            call = self._call
            if (call is None or not self.cancel_requested or call.kind is CallKind.STOP or call.attention_at is not None
                    or call.thread != threading.get_ident()):
                return
            call.attention_at = time.monotonic()
        try:
            self._conn.cancel()
        except Exception as exc:  # never raise into the driver
            # The number only above DEBUG: a server's text may echo literals (spec ⑤ §5, G12)
            logger.warning("SQL Server cancel of SPID {} from a message failed: {} {}", self._spid,
                           type(exc).__name__, getattr(exc, "number", "") or "")
            logger.debug("SQL Server cancel of SPID {} from a message failed: {}", self._spid, exc)
            return
        logger.info("SQL Server cancel sent to SPID {} from its message handler ({})", self._spid, call.kind)

    def _leave(self, call: Call) -> None:
        with self._lock:
            call.ended_at = time.monotonic()
            if self._call is call:
                self._call = None
            self._changed.notify_all()

    # ---- any thread ----

    def cancel(self) -> None:
        with self._lock:
            if not self._in_statement or self.aborted is not None:
                return
            self.cancel_requested = True
            self._changed.notify_all()
            call = self._call
            if call is not None and call.flagged_at is None:
                call.flagged_at = time.monotonic()
            if call is None or call.kind is CallKind.STOP or call.watched:
                logger.info("SQL Server cancel flagged for SPID {}: the worker stops at its next step", self._spid)
                return
            call.watched = True
        threading.Thread(
            target=self._watch, args=(call, call.flagged_at), name=f"mssql-cancel-{self._spid}", daemon=True,
        ).start()

    def force_cancel(self) -> None:
        """A repeated Cancel of the same request. After a warning it drops the connection, unless it comes within
        DOUBLE_CLICK_S of the warning. Otherwise it escalates a stuck call (see the module docstring): a cancel sent
        for it (the worker's own, or from its message handler) at least WEDGE_S ago has not ended it (R-03b-F1), or a
        row read flagged that long ago still receives one value (R-03b-F2). Anything else, a double click among them,
        acts as a plain cancel (R-03b-T8i)."""
        with self._lock:
            call, now = self._call, time.monotonic()
            stuck = None
            if call is not None and self._warned_at is not None:
                if now - self._warned_at >= DOUBLE_CLICK_S:
                    self._shutdown(_FORCED_REASON, call)
                    return
            elif call is not None:
                stuck = self._stuck(call, now)
        if stuck is not None:
            self._escalate(call, stuck, forced=True)
        self.cancel()

    def _stuck(self, call: Call, now: float) -> _Escalation | None:
        """Caller holds the lock. Why a repeated Cancel may force `call` now, or None (R-03b-F1, F2, T8i)."""
        sent_at = call.started if call.kind is CallKind.STOP else call.attention_at
        if sent_at is not None:
            return _UNDOING if now - sent_at >= WEDGE_S else None
        if call.kind is CallKind.ROWS and call.flagged_at is not None and now - call.flagged_at >= WEDGE_S:
            return _DATA  # the bytes of one big value keep the watcher from its deadline
        return None

    def drop(self, reason: str) -> None:
        """Shut the socket down whatever is in progress (closing under a call on another thread): its read ends."""
        with self._lock:
            self._shutdown(reason, self._call)

    def abort(self) -> None:
        """The session is closing (its close guard has asked about an open transaction): stop now. A driver call in
        progress ends by dropping the connection; with none, the flag stops the worker before its next one."""
        with self._lock:
            if self._in_statement:
                self.cancel_requested = True
                self._changed.notify_all()
            if self._call is not None:
                self._shutdown("forced by closing the session", self._call)

    # ---- the watcher thread (never enters the driver) ----

    def _reachable(self, call: Call, progress: list, bytes_arrived: bool) -> bool:
        """Whether the worker will see the flag by itself. Caller holds the lock. `progress` is [rows, when]."""
        if call.kind is CallKind.EXECUTE:
            return call.answered_at is None  # sending, or inside the wait callback
        if call.kind is CallKind.ROWS:
            now = time.monotonic()
            # Rows keep coming, or the bytes of one big row do: the worker stops when the row is complete.
            if call.rows != progress[0] or bytes_arrived:
                progress[0], progress[1] = call.rows, now
            return now - progress[1] < GRACE_S
        return False

    def _watch(self, call: Call, flagged_at: float) -> None:
        try:
            self._watch_call(call, flagged_at)
        except Exception:  # never die silently: a later Cancel starts a new watcher
            logger.exception("The SQL Server cancel watcher of SPID {} failed", self._spid)
            with self._lock:
                call.watched = False

    def _watch_call(self, call: Call, flagged_at: float) -> None:
        """Watch a flagged call; when the flag cannot reach the worker for WEDGE_S, escalate: drop the connection if no
        transaction can be open, otherwise warn the request once and wait for the call to end (R-03b-T8h, F6)."""
        progress = [call.rows, flagged_at]
        received: list = [None, None]  # [bytes received, when they last changed]
        unreachable: list = [None]  # since when the flag cannot reach the worker
        while self._unreached(call, progress, received, unreachable):
            self._escalate(call, self._why(call, received), forced=False)
            unreachable[0] = None  # an escalation that could not act (socket unknown) tries again after WEDGE_S, no spin

    @staticmethod
    def _why(call: Call, received: list) -> _Escalation:
        """What kept the flag from the worker, for the texts: the server waits and sends nothing (the byte counter says
        so), or it keeps sending: row counts (DONE tokens) between result sets, data in a row read."""
        if received[0] is not None and (received[1] is None or time.monotonic() - received[1] >= _ACTIVE_S):
            return _WAITING
        return _ROW_COUNTS if call.kind in (CallKind.EXECUTE, CallKind.NEXT_SET) else _DATA

    def _escalate(self, call: Call, why: _Escalation, *, forced: bool) -> None:
        """The one warn-then-force rule (R-03b-T8h, F1, F2), for the watcher's deadline and a repeated Cancel
        (`forced`): no transaction can be open → drop the connection; one may be → warn the request, once per
        statement. Neither the transaction check nor the warning runs under the lock."""
        holds = self._holds_transaction()  # it may lex the batch
        reason = _FORCED_REASON if forced else _BUSY_REASON
        with self._lock:
            if self._call is not call or self.aborted is not None or self._warned_at is not None:
                return  # the call ended, or another thread dropped the connection or warned meanwhile
            if not forced and call.attention_at is not None:
                return  # the watcher's deadline, but the message handler sent a cancel meanwhile
            if not holds:
                self._shutdown(reason, call, why.dropped)
                return
            self._warned_at = time.monotonic()
        logger.info("SQL Server cancel of SPID {} ({}, {}{}): a transaction may be open, so the request is warned and "
                    "the next Cancel drops the connection", self._spid, call.kind, why.situation,
                    ", Cancel repeated" if forced else "")
        self._warn(why.warning)

    def _holds_transaction(self) -> bool:
        """`may_hold_transaction()`, with a failure counted as "may": a Cancel never rolls work back by mistake."""
        try:
            return bool(self._may_hold_transaction())
        except Exception as exc:
            logger.warning("Could not tell whether SQL Server SPID {} holds a transaction ({}): assuming it may",
                           self._spid, type(exc).__name__)
            return True

    def _unreached(self, call: Call, progress: list, received: list, unreachable: list) -> bool:
        """Watch the call until it ends (False) or, unless the request was warned already, the flag has not been able to
        reach the worker for WEDGE_S (True)."""
        with self._lock:
            while self._call is call and self.aborted is None and call.attention_at is None:
                now = time.monotonic()
                total = _received(self._socket.fd) if self._socket is not None else None
                bytes_arrived = total is not None and received[0] is not None and total != received[0]
                if total is not None and total != received[0]:
                    received[0], received[1] = total, (now if bytes_arrived else received[1])
                if self._reachable(call, progress, bytes_arrived):
                    unreachable[0] = None
                else:
                    unreachable[0] = unreachable[0] or now
                    if self._warned_at is None and now - unreachable[0] >= WEDGE_S:
                        return True
                self._changed.wait(_WATCH_S)
        return False

    def _shutdown(self, reason: str, call: Call | None, message: str = ABORTED_MESSAGE) -> None:
        """Caller holds the lock. Shut the socket down (never close it): the worker's reads end, its driver call
        fails, and the server ends the session; the statement reports `message`."""
        if self.aborted is not None:
            return
        known = self._socket
        if known is None or _identify(known.fd) != known:
            logger.warning("Cannot drop SQL Server SPID {} ({}): its socket is not known", self._spid, reason)
            return
        try:
            with socket.socket(fileno=os.dup(known.fd)) as sock:
                sock.shutdown(socket.SHUT_RDWR)
        except OSError as exc:
            logger.warning("Dropping SQL Server SPID {} ({}) failed: {}", self._spid, reason, exc)
            return
        self.aborted, self._aborted_message = reason, message
        self.cancel_requested = True
        self._changed.notify_all()
        if call is None:
            logger.warning("Dropped the connection of SQL Server SPID {} ({})", self._spid, reason)
            return
        logger.warning("Dropped the connection of SQL Server SPID {} to stop its {} ({}), {:.0f} ms into the call",
                       self._spid, call.kind, reason, (time.monotonic() - call.started) * 1000)
