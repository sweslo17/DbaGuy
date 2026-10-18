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

Importing this module is what sets the threshold and the callback, so every production module takes pymssql from
here, never from `pymssql` directly (test helpers may).
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from pymssql import _mssql

_mssql.min_error_severity = 11  # severities 0-10 (PRINT, RAISERROR … 10) are messages; pymssql's default 6 ends the batch
ROW = _mssql.ROW_FORMAT_TUPLE
Connection = _mssql.MSSQLConnection
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


def connect(login_timeout: int, **kwargs: Any) -> Connection:
    """`_mssql.connect`. pymssql keeps the login timeout in a module global (FreeTDS applies it process-wide), so it is
    set right before each login."""
    _mssql.login_timeout = login_timeout
    return _mssql.connect(**kwargs)
