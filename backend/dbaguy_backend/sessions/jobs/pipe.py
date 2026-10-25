"""The rows between a copy's source and its sink (spec ⑥ R-06-8, R-06-13): two workers, each on its own connection's
thread, and at most PIPE_CHUNKS chunks between them (Q1). The source puts what it read and blocks while the pipe is
full; the sink takes and blocks while it is empty; both look at their own Cancel every POLL_S. The source ends it with
`finish()` or `fail(error)`; the sink with `close()` when it stops first (a target error, a Cancel), and the source's
next `put` then stops the read (`PipeClosed`). Any thread."""

import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from ...adapters.base import StatementCancelled, StatementError
from ...domain.data_jobs import INVALID_FILTER
from ...domain.transfer import COLUMNS_CHANGED, QUERY_NOT_SELECT

PIPE_CHUNKS = 2  # R-06-8: chunks the pipe holds
POLL_S = 0.1  # how often a side waiting on the other looks at its own Cancel (§3.5.5 step 5)
SOURCE_FAILED = "source_failed"
# The source's refusals of what the preview checks too (a query that does not only read or gives more than one result,
# columns that changed, a filter that is not one condition): the copy's error keeps their code, so it reads as the
# preview's would (Task 9 docs review I2). Any other error of the source is `source_failed`: its code is the source
# connection's (a lost connection there is not the target's), and its text says where
SOURCE_CHECKS = frozenset({QUERY_NOT_SELECT, COLUMNS_CHANGED, INVALID_FILTER})
Rows = list[list[Any]]


class PipeClosed(StatementCancelled):
    """The sink stopped reading: the source ends as cancelled (nothing it read is wanted any more)."""

    def __init__(self) -> None:
        super().__init__("The copy stopped reading")


class SourceFailed(StatementError):
    """The source's read failed (spec §4 `source_failed`, or the code of one of SOURCE_CHECKS): its own error in
    `cause`, the text says where."""

    def __init__(self, source: str, cause: StatementError) -> None:
        code = cause.code if cause.code in SOURCE_CHECKS else SOURCE_FAILED
        super().__init__(f"Reading {source} failed: {cause.message}", code=code)
        self.cause = cause


class RowPipe:
    def __init__(self, source: str, capacity: int = PIPE_CHUNKS, poll_s: float = POLL_S) -> None:
        self._source = source  # what messages call it
        self._capacity = capacity
        self._poll_s = poll_s
        self._chunks: deque[Rows] = deque()
        self._lock = threading.Condition()
        self._done = False
        self._error: StatementError | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        """The sink stopped: a source that has not begun reads nothing (06c Task 3 review M-1)."""
        with self._lock:
            return self._closed

    # ---- the source's side ----

    def put(self, rows: Rows, check: Callable[[], None]) -> None:
        """Hand a chunk to the sink, waiting while the pipe is full; `check()` raises the source's own Cancel."""
        while True:
            with self._lock:
                if self._closed:
                    raise PipeClosed()
                if len(self._chunks) < self._capacity:
                    self._chunks.append(rows)
                    self._lock.notify_all()
                    return
                self._lock.wait(self._poll_s)
            check()

    def finish(self) -> None:
        """Every row was put."""
        with self._lock:
            self._done = True
            self._lock.notify_all()

    def fail(self, error: StatementError) -> None:
        """The read stopped on `error` (a cancel included): the sink raises it once the rows put before it are taken.
        The first end is the one that counts: after `finish()`, or a first error, it changes nothing."""
        with self._lock:
            if not self._done and self._error is None:
                self._error = error
            self._lock.notify_all()

    # ---- the sink's side ----

    def take(self, check: Callable[[], None]) -> Rows | None:
        """The next chunk, waiting for one; None once every row was taken. `check()` raises the sink's own Cancel.
        Raises SourceFailed with the source's error, once the rows put before it are taken: the sink stops reading
        before it cancels its source (`close`), so a cancel here is not the copy's own (the tab's session closed)."""
        while True:
            with self._lock:
                if self._chunks:
                    rows = self._chunks.popleft()
                    self._lock.notify_all()
                    return rows
                if self._error is not None:
                    raise SourceFailed(self._source, self._error)
                if self._done:
                    return None
                self._lock.wait(self._poll_s)
            check()

    def close(self) -> None:
        """The sink stopped: what is in the pipe is dropped, and the source's next `put` stops it."""
        with self._lock:
            self._closed = True
            self._chunks.clear()
            self._lock.notify_all()
