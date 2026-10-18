"""The one place production code reaches pymssql: `pymssql._mssql` with DbaGuy's module-wide settings applied.

`min_error_severity = 11`. SQL Server reports PRINT and RAISERROR with severity 0-10 as messages, not errors. pymssql's
default (6) raises an exception for severities 6-10 and so ends the batch on a mere message; with 11, only what the
server itself calls an error (severity 11 and above: a failed statement, `RAISERROR('x', 16, 1)`) raises.

Side effect on DB-Lib errors: FreeTDS's own errors (20003 timeout, 20004 read error, 20006 write error, 20009
connection failed, 20047 dead connection) pass through the same threshold, and all of them have a severity of 10 or
less. With 11 they never raise, and what is left of a dead connection is an `MSSQLDatabaseException` with number 0 and
the text "Unknown error" while `connected` stays True. Whoever needs to know whether the connection is alive asks the
server (the adapter runs a `SELECT 1`); it never reads an error text for that.

The wait callback (R-03b-T8d). pymssql calls one process-wide callback with the socket's file descriptor right before
each `dbsqlok()`, the wait for a batch's first response, on the thread running the batch. DbaGuy installs it here once;
it only dispatches to the hook the calling thread set with `wait_hook()` for its own driver call, and does nothing for
any other call (other connections, test helpers). A hook may block (polling the socket) and may raise to abandon the
wait; it must never call `cancel()`: dbsqlok() would then wait for an acknowledgement already consumed. The socket is
then left with the batch sent and unread, which the raising thread ends itself with `cancel()`.

Every entry into the driver is counted (R-03b-F5, F6): `connect()` hands out a `GuardedConnection`, never pymssql's
own object. Only one thread uses a connection, the cancel included. dbclose() frees FreeTDS's connection; a thread
still inside the driver for it would then use freed memory and take the whole sidecar down with it. So `close()` waits,
bounded, until no call is inside, and otherwise leaves the connection open (leaked) instead of freeing it under it.

Importing this module is what sets the threshold and the callback, so every production module takes pymssql from
here, never from `pymssql` directly (test helpers may).
"""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from loguru import logger
from pymssql import _mssql

_mssql.min_error_severity = 11  # severities 0-10 (PRINT, RAISERROR … 10) are messages; pymssql's default 6 ends the batch
ROW = _mssql.ROW_FORMAT_TUPLE
DatabaseError = _mssql.MSSQLDatabaseException
DriverError = _mssql.MSSQLDriverException
ERRORS = (_mssql.MSSQLDatabaseException, _mssql.MSSQLDriverException)

WaitHook = Callable[[int], None]  # called with the socket's file descriptor
_thread_hooks = threading.local()


def _on_wait(fd: int) -> None:
    hook: WaitHook | None = getattr(_thread_hooks, "hook", None)
    if hook is not None:
        hook(fd)


_mssql.set_wait_callback(_on_wait)


@contextmanager
def wait_hook(hook: WaitHook) -> Iterator[None]:
    """`hook` receives this thread's waits for a first response until the block ends."""
    previous = getattr(_thread_hooks, "hook", None)
    _thread_hooks.hook = hook
    try:
        yield
    finally:
        _thread_hooks.hook = previous


CLOSE_WAIT_S = 3.0  # how long close() waits for the calls inside the driver to leave before leaking the connection


class GuardedConnection:
    """A pymssql connection whose every entry into the driver goes through one count (R-03b-F5, F6).

    - Only one thread ever uses the connection: a call from another thread while one is inside is refused before it
      reaches the driver, with no exception (two threads in FreeTDS for one connection is what aborts it, or corrupts
      the values the other thread is converting). A cancel goes through the same door: only the worker sends one.
    - `close()` frees the connection only once no call is inside (see `close`).
    - A row iterator counts each row read on its own, so a reader blocked inside a row is inside the driver.

    No lock is held during a driver call. A thread inside a call also holds a reference to pymssql's object, so its
    deallocation (which closes) cannot run under that call either. The methods are the `_mssql.MSSQLConnection` ones
    DbaGuy uses."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self._lock = threading.Lock()
        self._user: int | None = None  # the thread whose calls are inside
        self._depth = 0  # how many (a message handler's cancel nests in the call that brought the message)
        self._closed = False  # set first thing in close(): no call enters from then on
        self._freed = False  # dbclose() ran

    @property
    def busy(self) -> bool:
        """Whether a call is inside the driver right now."""
        with self._lock:
            return self._depth > 0

    def _enter(self) -> Any:
        me = threading.get_ident()
        with self._lock:
            if self._closed:
                raise DriverError("The SQL Server connection is closed")  # never reach a freed connection
            if self._depth and self._user != me:
                raise DriverError("Another thread is using this SQL Server connection")
            self._user, self._depth = me, self._depth + 1
        return self._raw

    def _leave(self) -> None:
        with self._lock:
            self._depth -= 1

    def _call(self, name: str, *args: Any) -> Any:
        raw = self._enter()
        try:
            return getattr(raw, name)(*args)
        finally:
            self._leave()

    def execute_query(self, sql: str) -> None:
        self._call("execute_query", sql)

    def execute_non_query(self, sql: str) -> None:
        self._call("execute_non_query", sql)

    def nextresult(self) -> Any:
        return self._call("nextresult")

    def get_header(self) -> Any:
        return self._call("get_header")

    def set_msghandler(self, handler: Callable[..., None] | None) -> None:
        self._call("set_msghandler", handler)

    def cancel(self) -> None:
        """The worker's own cancel (also from its message handler, inside the call that brought the message)."""
        self._call("cancel")

    @property
    def rows_affected(self) -> int:
        raw = self._enter()
        try:
            return raw.rows_affected
        finally:
            self._leave()

    def get_iterator(self, row_format: int) -> Iterator[tuple]:
        return self._rows(self._call("get_iterator", row_format))

    def _rows(self, rows: Iterator[tuple]) -> Iterator[tuple]:
        while True:
            self._enter()
            try:
                row = next(rows, None)
            finally:
                self._leave()
            if row is None:
                return
            yield row

    def close(self, wait_s: float = CLOSE_WAIT_S) -> bool:
        """dbclose() once no call is inside the driver; False when a call stayed inside for `wait_s`: the connection
        is then leaked, never freed under it (the caller drops the socket first, so a blocked read ends). Idempotent."""
        with self._lock:
            self._closed = True
            if self._freed:
                return True
        deadline = time.monotonic() + wait_s
        while self.busy:
            if time.monotonic() >= deadline:
                logger.error("A call stayed inside the SQL Server driver for {:.1f} s after close: the connection is "
                             "leaked instead of freed under it", wait_s)
                return False
            time.sleep(0.01)
        with self._lock:
            if self._freed:
                return True
            self._freed = True
        self._raw.close()
        return True


Connection = GuardedConnection


def connect(login_timeout: int, **kwargs: Any) -> GuardedConnection:
    """`_mssql.connect`. pymssql keeps the login timeout in a module global (FreeTDS applies it process-wide), so it is
    set right before each login."""
    _mssql.login_timeout = login_timeout
    return GuardedConnection(_mssql.connect(**kwargs))
