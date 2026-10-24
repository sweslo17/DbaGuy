"""A copy's source (spec ⑥ R-06-8, §3.5.5): one table's rows read on the source's own worker — the session the copy
opened for itself, or the query tab's (the same transaction and temporary tables as the tab, as an export) — and put
into the pipe to its sink, a chunk at a time. A helper job (`Job.helper`): never listed on its own; its events reach the
source session's client, which knows no such request (a query tab ignores them). Its outcome is not the copy's: the sink
reads the pipe, where every way it ends is put (`finish`, `fail`; `not_run` when it never began, 06c pre-flight M1).

Its read is DbaGuy's SELECT of the table (with the table-data tab's filter, one condition that only reads), or the
tab's statement as it is, which must only read too (the classifier's plain read, as a dump's filter: Task 9 fix M2),
give the columns the preview mapped (`columns_changed`) and only one result (a T-SQL batch of several would copy its
first, maybe not the one shown: pre-flight M4). It is checked before anything is sent to the session (Task 3 review
M-3), and a source whose sink already stopped sends nothing (M-1). Around it the session's output settings are set to
what every engine reads and set back after (`transfer_sql.SOURCE_SETTINGS`): PostgreSQL's ISO dates, MySQL's UTC
TIMESTAMPs and a net_write_timeout a slow target cannot outwait (A11). `wait_ended()`: the sink closes a session it
opened only once its last read set them back. Reads are sized by the rows' text (PIPE_CHUNK_CHARS a chunk, at
most FETCH_ROWS rows), so memory follows the rows' size, never their number (Q1). When the sink stops first it closes
the pipe and never cancels a query tab's read (pre-flight H2: on PostgreSQL that aborts the tab's transaction): the
next `put` ends the read, and closing a result early is safe on every engine."""

import threading
from typing import Any

from loguru import logger

from ...adapters.base import StatementCancelled, StatementError
from ...domain.data_jobs import INVALID_FILTER
from ...domain.execution import ColumnMeta
from ...domain.transfer import QUERY_NOT_SELECT, QUERY_NOT_SELECT_MESSAGE
from ...services.transfer_plans import PlannedTable
from ...sql.select_builder import FILTER_NOT_A_CONDITION
from ...sql.transfer_sql import SOURCE_SETTINGS, read_settings, set_setting
from .context import FIRST_FETCH, Job, JobContext, JobReport, fetch_size, plain_read
from .pipe import PipeClosed, RowPipe

PIPE_CHUNK_CHARS = 4 * 1_048_576  # R-06-13: a chunk's values hold about this much text at most
COLUMNS_CHANGED = "columns_changed"
COLUMNS_CHANGED_MESSAGE = "The query's columns changed since the preview: preview the copy again"
MANY_RESULTS = "The statement returns more than one result: copy one statement's result at a time"
_OTHER_CHARS = 8  # what a value that is not text or bytes counts for in a chunk's size


def row_chars(row: list[Any]) -> int:
    return sum(len(value) if isinstance(value, str | bytes | bytearray) else _OTHER_CHARS for value in row)


class TransferSource(Job):
    kind = "copy_data"
    helper = True

    def __init__(self, pipe: RowPipe, table: PlannedTable, *, query: bool, label: str) -> None:
        self._pipe = pipe
        self._table = table
        self._query = query  # the tab's statement: it is checked to only read, and its columns are compared
        self._label = label  # the copy's name in the source session's log
        self._ended = threading.Event()  # its run is over, settings set back included (or it never ran)

    @property
    def title(self) -> str:
        return f"Read {self._table.source_name} for {self._label}"

    def wait_ended(self, timeout: float) -> bool:
        """Wait until its run is over (its settings set back) or it is known never to run; whether it is."""
        return self._ended.wait(timeout)

    def run(self, ctx: JobContext) -> JobReport:
        try:
            return self._run(ctx)
        finally:
            self._ended.set()

    def _run(self, ctx: JobContext) -> JobReport:
        saved: list[tuple[str, str]] = []
        rows = 0
        try:
            if self._pipe.closed:  # its sink stopped while it waited in the queue (Task 3 review M-1): nothing is sent
                raise PipeClosed()
            sql = self._checked(ctx)  # before the session's settings are touched (M-3)
            saved = self._read(ctx)
            for name, value in SOURCE_SETTINGS.get(ctx.dialect, ()):
                ctx.setting(set_setting(ctx.dialect, name, value))
            result = ctx.run(sql)
            try:
                self._compare(result.columns())
                size, largest = FIRST_FETCH, 0
                while chunk := result.fetch(size):  # until `[]`: an error may follow a short chunk (06a fix H1)
                    largest = max(largest, max(row_chars(row) for row in chunk))
                    rows += len(chunk)
                    self._pipe.put(chunk, ctx.raise_if_cancelled)
                    size = fetch_size(largest, PIPE_CHUNK_CHARS)
                if self._query and result.next_result():  # a T-SQL batch: its first result was read, maybe not the
                    raise StatementError(MANY_RESULTS, code=QUERY_NOT_SELECT)  # one shown (the table rolls back, M4)
            finally:
                ctx.close(result)
            self._pipe.finish()
            return JobReport(ctx.outcome("completed", rows=rows))
        except StatementError as exc:
            self._pipe.fail(exc)
            return JobReport(ctx.outcome("failed" if ctx.sent else "not_started", rows=rows,
                                         cancelled=isinstance(exc, StatementCancelled)), exc)
        except Exception as exc:  # a bug: the sink must not wait for rows that never come
            logger.exception("Reading {} for a copy failed", self._table.source_name)
            error = StatementError(f"Internal error: {exc}")
            self._pipe.fail(error)
            return JobReport(ctx.outcome("failed" if ctx.sent else "not_started", rows=rows), error)
        finally:
            self._restore(ctx, saved)

    def not_run(self, error: StatementError) -> None:
        """Nothing sent (cancelled or withdrawn while queued, its session closed first): the sink must not wait for
        rows."""
        self._pipe.fail(error)
        self._ended.set()

    def broke(self, ctx: JobContext, exc: Exception) -> JobReport:
        """Never reached (`run` classifies every error), but the sink must hear of it whatever happened."""
        error = exc if isinstance(exc, StatementError) else StatementError(f"Internal error: {exc}")
        self._pipe.fail(error)
        self._ended.set()
        return super().broke(ctx, exc)

    def _checked(self, ctx: JobContext) -> str:
        """The SELECT, when it may run: a statement the user wrote, or a filter of theirs in DbaGuy's, must only read
        (on any profile), and every statement passes the session's gate."""
        refusal = None
        if self._query:
            refusal = StatementError(QUERY_NOT_SELECT_MESSAGE, code=QUERY_NOT_SELECT)  # the preview's words
        elif self._table.filtered:  # the user typed a filter, not a statement: the dump's words (Task 3 review M-4)
            refusal = StatementError(FILTER_NOT_A_CONDITION, code=INVALID_FILTER)
        return plain_read(ctx, self._table.select, not_a_read=refusal,
                          read_only="This connection is read-only, and the statement is not a plain read")

    def _compare(self, columns: list[ColumnMeta] | None) -> None:
        """A query runs again: its columns must be the ones the preview mapped (spec §3.5.5 step 2)."""
        expected = [column.name for column in self._table.columns]
        found = [column.name for column in columns or []]
        if columns is None or (self._query and found != expected):
            raise StatementError(COLUMNS_CHANGED_MESSAGE, code=COLUMNS_CHANGED)

    @staticmethod
    def _read(ctx: JobContext) -> list[tuple[str, str]]:
        """The session's own values of the settings the copy sets (`transfer_sql.SOURCE_SETTINGS`): set back after."""
        read = read_settings(ctx.dialect)
        if read is None:
            return []
        result = ctx.run(read)
        try:
            [values] = result.fetch(1)
            while result.fetch(1):
                pass
        finally:
            ctx.close(result)
        names = [name for name, _ in SOURCE_SETTINGS[ctx.dialect]]
        return [(name, str(value)) for name, value in zip(names, values, strict=True)]

    @staticmethod
    def _restore(ctx: JobContext, saved: list[tuple[str, str]]) -> None:
        """Set back what the copy changed (a query tab's session is the user's); no Cancel may stop it. A failure is
        logged with its code only (a server's text may echo what it read, D-06-12) and stops the rest."""
        if not saved:
            return
        ctx.stop_cancels()
        for name, value in saved:
            try:
                ctx.setting(set_setting(ctx.dialect, name, value))
            except Exception as exc:  # never let it escape: the read's own end is already in the pipe
                logger.warning("[copy] setting {} back on the source failed ({})", name,
                               getattr(exc, "code", None) or type(exc).__name__)
                return
