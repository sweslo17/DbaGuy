"""The armed window of one statement (`DatabaseConnection.arm_cancel`), for adapters whose cancel needs a running
statement: PostgreSQL's cancel request and MySQL's KILL QUERY are ignored while the session is idle, and SQLite drops
an interrupt when no statement runs."""

import threading

from loguru import logger

from .base import StatementCancelled


class CancelWindow:
    """From the worker's last cancel check until run() takes the statement over, a cancel has nothing to stop yet: it is
    kept here instead of being sent, and run() then does not start. After run() has taken over, a cancel goes its usual
    way. A kept cancel never outlives its statement (run() and disarm() both end the window), so it can never stop a
    later one (R-03a-P4).

    Ordering for the adapter: `keep()` first in cancel(); `take_over()` in run() before anything is sent, and after
    whatever makes a later cancel effective (SQLite: inside the gate's call), so no cancel falls between the two."""

    def __init__(self, engine: str) -> None:
        self._engine = engine  # for the log
        self._lock = threading.Lock()
        self._armed = False
        self._kept = False

    def arm(self) -> None:
        """Worker thread."""
        with self._lock:
            self._armed, self._kept = True, False

    def disarm(self) -> None:
        """Worker thread; idempotent."""
        with self._lock:
            self._armed = self._kept = False

    def keep(self) -> bool:
        """Any thread, first thing in cancel(): True while armed; the cancel is then kept for run() and the caller sends
        nothing (there is nothing to stop yet)."""
        with self._lock:
            if self._armed:
                self._kept = True
            return self._armed

    def take_over(self) -> None:
        """Worker thread, in run() before anything is sent: end the window; raise StatementCancelled when a cancel was
        kept in it."""
        with self._lock:
            kept = self._kept
            self._armed = self._kept = False
        if kept:
            logger.info("{} statement cancelled before it was sent", self._engine)
            raise StatementCancelled()
