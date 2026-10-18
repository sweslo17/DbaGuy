"""How a cancel reaches a SQL Server statement (spec §3.6; rulings R-03b-P4, P5, T8a, T8d, T8e).

The worker thread runs the statement. cancel() comes from any thread: the user's Cancel, a timeout, or closing the tab.
pymssql has one way to stop a statement, `cancel()`, which is FreeTDS's dbcancel(): it marks the cancel, sends an
attention and reads up to the acknowledgement. pymssql's FreeTDS is built with MARS, and there dbcancel() from another
thread is safe in one situation only: while the worker is the connection's network reader, blocked waiting for data.
FreeTDS then just wakes the worker, which sends the attention and reads the acknowledgement itself. At any other moment
(the worker between two reads, a chatty batch sends a token every few microseconds), the other thread becomes the
network reader, takes the worker's packets, and the worker waits on a condition variable that nothing signals: a wedge
that no socket operation can end (seen in review: 3 hung tabs in 41 cancels of a NOCOUNT OFF loop).

So a cancel never makes two threads use the driver, except in that one situation:
- Waiting for a batch's first response (a WAITFOR, a long query before its first row, a lock wait, the describe): the
  wait callback polls the socket and raises CancelledWhileWaiting; the worker sends the cancel itself (R-03b-T8e).
- Starting (state read, describe rows), between rows, a paused page: only the flag. The worker stops before its next
  row or never sends the batch (R-03b-P4, P5).
- A message (PRINT, RAISERROR … WITH NOWAIT) during a driver call: the worker sends the cancel from its message handler,
  where FreeTDS expects it (the worker holds the wire, so dbcancel() only marks the cancel and wakes the reader).
- Blocked in a read the flag cannot reach (after the first response, the wait for the next result set, a row read
  without progress such as a row lock): once the socket has received nothing for GRACE_S and nothing waits unread, the
  worker is blocked waiting for data, and dbcancel() goes out from a canceller thread, holding no lock.
- Still running WEDGE_S after the cancel without such a moment (a chatty batch without messages, e.g. a NOCOUNT OFF
  loop): when no transaction can be open, the socket is shut down. When one may be (manual commit, @@TRANCOUNT > 0
  before the batch, a transaction word in it), a Cancel must never silently roll the user's work back (R-03b-T8h):
  the request gets one warning and the cancel waits on; a second Cancel or closing the session drops the connection.
  A second Cancel or closing the session also drops it once a cancel was sent and has not ended the call. The socket
  is shut down, never closed from another thread: closing under a reading thread aborts FreeTDS.
A shut-down connection reports connection_lost; the server ends the session and rolls back its transaction.

Residual risk: data arriving within the microseconds between the quiet check and dbcancel() can still wedge the
worker; the second Cancel then cannot free the thread either, though the shutdown ends the server's session.
"""

import array
import fcntl
import os
import select
import socket
import struct
import termios
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum

from loguru import logger

from . import driver

GRACE_S = 0.05  # the socket received nothing this long: the worker is blocked waiting for data
WEDGE_S = 1.0  # a cancel that found no safe moment in this time drops the connection, or warns (R-03b-T8h)
_POLL_MS = 50  # the wait callback's polling step: how soon it sees the flag
_WATCH_S = 0.01  # how often the canceller looks again
_TCP_CONNECTION_INFO = 0x106  # macOS <netinet/tcp.h>; struct tcp_connection_info, tcpi_rxbytes at offset 88
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
# The same two texts when what kept the connection busy was not row counts (a row read that receives no bytes it can
# show as progress, or a platform without the socket's byte counter).
BUSY_DATA_DROPPED_MESSAGE = (
    "The connection was dropped to stop the batch: the server kept sending data, so it could not be cancelled safely. "
    "Statements it had already completed stay committed (auto-commit); the statement that was running, and any "
    "transaction the batch itself opened, were rolled back. Reconnect to go on."
)
BUSY_DATA_WARNING = (
    "This batch cannot be stopped safely while the server keeps sending data. Press Cancel again to drop the "
    "connection — the server rolls back the open transaction."
)
_BUSY_REASON = "no safe moment to send the cancel and no transaction to lose: the batch kept the connection busy"


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


def _received(fd: int) -> tuple[int, int] | None:
    """(bytes received so far, bytes waiting unread) of the socket; None where the platform does not tell."""
    try:
        with socket.socket(fileno=os.dup(fd)) as sock:
            info = sock.getsockopt(socket.IPPROTO_TCP, _TCP_CONNECTION_INFO, _TCP_INFO_SIZE)
        unread = array.array("i", [0])
        fcntl.ioctl(fd, termios.FIONREAD, unread)
    except OSError:
        return None
    if len(info) < _RXBYTES_AT + 8:
        return None
    return struct.unpack_from("Q", info, _RXBYTES_AT)[0], unread[0]


@dataclass(eq=False)
class Call:
    """One driver call a cancel may have to interrupt. `rows` is bumped by the worker for every row it reads."""

    kind: CallKind
    started: float = field(default_factory=time.monotonic)
    thread: int = field(default_factory=threading.get_ident)
    answered_at: float | None = None  # EXECUTE: the wait callback returned, the first response is in
    rows: int = 0
    ended_at: float | None = None
    watched: bool = False  # a canceller thread looks after this call
    warned: bool = False  # no safe moment came and a transaction may be open: the request was told (R-03b-T8h)
    attention: str | None = None  # how a cancel was sent during this call ("across threads", "from a message")
    attention_at: float | None = None  # when that cancel went out
    across_sent_at: float | None = None
    across_done_at: float | None = None


class CancelControl:
    """The cancel state of one connection: the flag, the driver call in progress, the socket, and the canceller."""

    def __init__(
        self, conn: driver.Connection, spid: int, *, may_hold_transaction: Callable[[], bool] = lambda: True,
        warn: Callable[[str], None] = lambda text: None,
    ) -> None:
        """`may_hold_transaction`: whether the running batch may have a transaction to lose (called on the canceller
        thread while the worker is inside the driver). `warn`: tells the running request (thread-safe)."""
        self._conn = conn
        self._spid = spid
        self._may_hold_transaction = may_hold_transaction
        self._warn = warn
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._in_statement = False
        self.cancel_requested = False  # read without the lock between rows: a plain attribute
        self._call: Call | None = None
        self._socket: _Socket | None = None
        self.aborted: str | None = None  # why the socket was shut down
        self._aborted_message = ABORTED_MESSAGE  # what the statement it stopped reports
        self.poisoned = False  # a dbcancel() may still run on another thread: the driver must never be touched again

    # ---- worker thread ----

    @property
    def unusable(self) -> bool:
        return self.aborted is not None or self.poisoned

    @property
    def aborted_message(self) -> str:
        return self._aborted_message

    def begin_statement(self) -> None:
        """The flag belongs to one statement (R-03a-P4): from here a cancel is kept, not dropped (R-03b-P5)."""
        with self._lock:
            self._in_statement = True
            self.cancel_requested = False

    def end_statement(self) -> None:
        with self._lock:
            self._in_statement = False
            self.cancel_requested = False
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
        pending (CancelPending). On the way out, a dbcancel() still running on the canceller thread is waited for: once
        the call is over, the driver is never used by two threads."""
        with self._lock:
            if self.cancel_requested and kind in (CallKind.EXECUTE, CallKind.NEXT_SET):
                raise CancelPending()
            current = self._call = Call(kind)
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
        here: FreeTDS is in the middle of reading for this thread, so dbcancel() only marks the cancel and wakes the
        reader, the way FreeTDS expects a cancel from a message handler."""
        with self._lock:
            call = self._call
            if (call is None or not self.cancel_requested or call.kind is CallKind.STOP or call.attention is not None
                    or call.thread != threading.get_ident()):
                return
            call.attention, call.attention_at = "from a message", time.monotonic()
        try:
            self._conn.cancel()
        except Exception as exc:  # never raise into the driver
            logger.warning("SQL Server cancel of SPID {} from a message failed: {}: {}", self._spid,
                           type(exc).__name__, exc)
            return
        logger.info("SQL Server cancel sent to SPID {} from its message handler ({})", self._spid, call.kind)

    def _leave(self, call: Call) -> None:
        with self._lock:
            call.ended_at = time.monotonic()
            if self._call is call:
                self._call = None
            self._changed.notify_all()
            while call.across_sent_at is not None and call.across_done_at is None:
                waited = time.monotonic() - call.ended_at
                if waited >= WEDGE_S and self.aborted is None:
                    self._shutdown("the statement returned, the cross-thread cancel did not", call)
                if waited >= WEDGE_S + 1.0:
                    self.poisoned = True
                    logger.error("A cross-thread cancel on SQL Server SPID {} never returned; the connection is given "
                                 "up without closing it", self._spid)
                    return
                self._changed.wait(0.05)

    # ---- any thread ----

    def cancel(self) -> None:
        with self._lock:
            if not self._in_statement or self.aborted is not None:
                return
            self.cancel_requested = True
            self._changed.notify_all()
            call = self._call
            if call is None or call.kind is CallKind.STOP or call.watched:
                logger.info("SQL Server cancel flagged for SPID {}: the worker stops at its next step", self._spid)
                return
            call.watched = True
        threading.Thread(
            target=self._watch, args=(call, time.monotonic()), name=f"mssql-cancel-{self._spid}", daemon=True,
        ).start()

    def force_cancel(self) -> None:
        """A repeated Cancel of the same request. It drops the connection only when the request was warned that it
        cannot be stopped safely, or when a cancel sent for the driver call still running (across threads, from a
        message, or the worker's own) went out at least WEDGE_S ago. A double click lands well inside that second,
        while the worker's own cancel may still be reading a stream to its acknowledgement: it acts as a plain cancel
        then (R-03b-T8i)."""
        with self._lock:
            call = self._call
            if call is not None:
                sent_at = call.started if call.kind is CallKind.STOP else call.attention_at
                if call.warned or (sent_at is not None and time.monotonic() - sent_at >= WEDGE_S):
                    self._shutdown("forced by a second cancel", call)
                    return
        self.cancel()

    def abort(self) -> None:
        """The session is closing (its close guard has asked about an open transaction): stop now. A driver call in
        progress ends by dropping the connection; with none, the flag stops the worker before its next one."""
        with self._lock:
            if self._in_statement:
                self.cancel_requested = True
                self._changed.notify_all()
            if self._call is not None:
                self._shutdown("forced by closing the session", self._call)

    # ---- the canceller thread ----

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
        """Send the cancel across threads at a safe moment. When none comes within WEDGE_S, drop the connection if no
        transaction can be open, otherwise warn the request once and keep looking (R-03b-T8h)."""
        progress = [call.rows, flagged_at]
        quiet: list = [None, flagged_at]  # [bytes received, since when unchanged]
        unreachable: list = [None]  # since when the flag cannot reach the worker
        while (outcome := self._safe_moment(call, progress, quiet, unreachable)) == "deadline":
            holds = self._holds_transaction()  # outside the lock: it may lex the batch
            # Row counts (DONE tokens) are what a call between result sets receives without a row or a message.
            counts = call.kind in (CallKind.EXECUTE, CallKind.NEXT_SET)
            with self._lock:
                if self._call is not call or self.aborted is not None or call.attention is not None:
                    return
                if not holds:
                    self._shutdown(_BUSY_REASON, call, BUSY_DROPPED_MESSAGE if counts else BUSY_DATA_DROPPED_MESSAGE)
                    return
                call.warned = True
            logger.info("SQL Server cancel of SPID {} found no safe moment and a transaction may be open: the request "
                        "is warned, a second Cancel drops the connection", self._spid)
            self._warn(BUSY_WARNING if counts else BUSY_DATA_WARNING)
        if outcome != "send":
            return
        try:
            self._conn.cancel()  # the worker is the network reader: FreeTDS only wakes it
        except Exception as exc:  # never let a cancel raise
            logger.warning("SQL Server cancel of SPID {} across threads failed: {}: {}", self._spid,
                           type(exc).__name__, exc)
        with self._lock:
            call.across_done_at = time.monotonic()
            self._changed.notify_all()
        logger.info("SQL Server cancel sent to SPID {} across threads while it waited for data ({}), in {:.0f} ms",
                    self._spid, call.kind, (call.across_done_at - call.across_sent_at) * 1000)

    def _holds_transaction(self) -> bool:
        """`may_hold_transaction()`, with a failure counted as "may": a Cancel never rolls work back by mistake."""
        try:
            return bool(self._may_hold_transaction())
        except Exception as exc:
            logger.warning("Could not tell whether SQL Server SPID {} holds a transaction ({}): assuming it may",
                           self._spid, type(exc).__name__)
            return True

    def _safe_moment(self, call: Call, progress: list, quiet: list, unreachable: list) -> str:
        """Watch the call until it ends ("gone"), a safe moment for a cross-thread cancel comes ("send", marked on the
        call), or, unless the request was warned already, WEDGE_S pass without one ("deadline")."""
        with self._lock:
            while self._call is call and self.aborted is None and call.attention is None:
                now = time.monotonic()
                received = _received(self._socket.fd) if self._socket is not None else None
                bytes_arrived = False
                if received is not None:
                    total, unread = received
                    bytes_arrived = quiet[0] is not None and total != quiet[0]
                    if total != quiet[0] or unread:
                        quiet[0], quiet[1] = total, now
                if self._reachable(call, progress, bytes_arrived):
                    unreachable[0] = None
                else:
                    unreachable[0] = unreachable[0] or now
                    if received is not None and now - quiet[1] >= GRACE_S:
                        call.attention, call.attention_at, call.across_sent_at = "across threads", now, now
                        return "send"
                    if not call.warned and now - unreachable[0] >= WEDGE_S:
                        return "deadline"
                self._changed.wait(_WATCH_S)
        return "gone"

    def _shutdown(self, reason: str, call: Call, message: str = ABORTED_MESSAGE) -> None:
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
        logger.warning("Dropped the connection of SQL Server SPID {} to stop its {} ({}), {:.0f} ms into the call",
                       self._spid, call.kind, reason, (time.monotonic() - call.started) * 1000)
