"""Copy data (spec ⑥ §3.5.5): a previewed plan run on the target's job session, each table in a transaction of its own,
its rows read on the source's worker (`TransferSource`) and handed over a pipe (R-06-8). What the preview showed is
what runs: its CREATE TABLE, DELETE and identity statements, its columns and SELECTs. Only the INSERTs are made here,
for the target session as it is (§3.5.5 step 1: MySQL's backslash mode read after its BEGIN; the session runs a dump's
restore settings, `transfer_sql.TARGET_SETUP`): each value converted as the type map says (`type_map.conversion`; a
value the target cannot hold fails its row, named by row and column), written as the target column's literal
(`InsertBatcher`: at most 1,000 rows, 100 on SQL Server, 1 MiB a statement), and each INSERT must report its rows (a
trigger that skipped or added rows fails the table, ⑤ D-05-5; SQL Server's INSTEAD OF triggers report the rows asked
for, A16).

Outcomes (R-06-12), per table: copied, rolled_back, not_started, unknown; for the copy: completed, partial (some tables
copied, then one failed or a Cancel stopped it), rolled_back (none copied), unknown (a COMMIT whose answer was lost, a
table that cannot roll back after a write went out — replace's DELETE included (pre-flight H3) — a SQL Server trigger's
COMMIT, a MySQL ROLLBACK that left changes, 1196). Whether a MySQL table can roll back is what the server says when the
table starts, never the plan's word alone: a table the copy created took the server's default engine, and another may
have changed since the preview (Task 4 review I-1). A copy stops at the first table that does not copy. A Cancel stops
the table being copied (rolled back; not_started when it came before the table's first statement) and every table after
it; one that comes during a COMMIT stops the next table; so does a profile made read-only meanwhile (pre-flight L-b).
Nothing is ever sent again. An unknown table counts no rows: its warning says how many may stand (review M-2).

The source: when the sink stops first it closes the pipe. It cancels a read on a session of the copy's own; a query
tab's read it never cancels (on PostgreSQL that aborts the tab's open transaction, pre-flight H2), and only withdraws it
while it still waits in the tab's queue (Task 3 review M-1): one the tab already runs stops at its next chunk. A source
that never starts (its session closed, a queue that cancelled it) fails the pipe, so the copy never waits for it
(pre-flight M1). A session the copy opened is closed once its last read set its settings back.

Logs: INFO the copy's tables, rows and outcome per table (the worker logs the job's end); never a value."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from loguru import logger

from ...adapters.base import CONNECTION_LOST, StatementCancelled, StatementError
from ...adapters.columns import TableColumn
from ...adapters.dump import plural
from ...domain.data_jobs import JobObject, JobOutcomeKind, TableOutcome
from ...domain.edits import PlanUnavailable
from ...domain.errors import READ_ONLY_MESSAGE, DomainError, NotFound
from ...domain.execution import CopyDataCommand
from ...domain.transfer import COLUMNS_CHANGED
from ...services.safety import GuardAction
from ...services.transfer_plans import PlannedTable, StoredTransfer
from ...sql.bulk_insert import InsertBatcher
from ...sql.dialects import Dialect, qualified, transaction_frame
from ...sql.transfer_sql import TARGET_RESET, TARGET_SETUP
from ...sql.type_map import Convert, RowFailure, conversion
from ..edit_apply import ROW_COUNT_UNKNOWN, TRANSACTION_ENDED
from .context import JOB_ROLLBACK_FAILED, Job, JobContext, JobReport, PreparedJob, SourceLease
from .kept_changes import KEPT, kept_changes, table_rolls_back
from .pipe import RowPipe, SourceFailed
from .run_file import TRANSACTION_ENDED as ENDED_BEFORE_COMMIT
from .transfer_source import TransferSource

ROW_COUNT_MISMATCH = "row_count_mismatch"
VALUE_NOT_COPYABLE = "value_not_copyable"
# A MySQL table the copy created: it stays whatever happens next. Empty only once it rolled back (I-1)
CREATED_EMPTY = ("{} was created (MySQL commits a CREATE TABLE at once) and stays, empty: drop it, or copy again "
                 "into it")
CREATED_KEPT = ("{} was created (MySQL commits a CREATE TABLE at once) and cannot roll back (non-transactional): it "
                "stays, with the rows written before the failure")
CREATED_CHECK = "{} was created (MySQL commits a CREATE TABLE at once) and stays: check it before copying into it again"
# A table that cannot roll back, once a write went out (⑤ G10, pre-flight H3; review M-5: the DELETE's own words)
NOT_ROLLED_BACK = "{} cannot roll back (non-transactional): the rows written before the failure stay"
DELETED_STAY = ("{} cannot roll back (non-transactional): the DELETE of its rows and the rows written before the "
                "failure stay")
DELETE_STOPPED = ("{} cannot roll back (non-transactional): its DELETE stopped part way, and the rows it removed "
                  "before that stay removed")
TRIGGER_ENDED = "A trigger ended the transaction of {}: what it committed stays"
MAY_STAND = "Up to {:,} rows written to {} may stand"  # an unknown table's (review M-2: its object counts none)
# After a ROLLBACK that left changes (1196): what stands is another table's, one a trigger wrote, not the rows of the
# table copied, which rolled back (Task 4 re-review N-1)
SOME_MAY_STAND = "Some changes may stand (a table that cannot roll back)"
NOT_STARTED = "{} not copied: {}"  # `1 table` or `n tables`, then their names (review M-8)
SOURCE_UNAVAILABLE = "The source connection could not be opened: {}"
SOURCE_END_WAIT_S = 10.0  # how long a session the copy opened may take to end its last read before it is closed


@dataclass
class _Table:
    """One table's end."""

    outcome: TableOutcome
    rows: int = 0
    error: StatementError | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Writing:
    """What one table's copy did so far: how it ends follows from it."""

    transactional: bool  # it rolls back: the plan's word, and on MySQL the server's when the table starts (I-1)
    began: bool = False  # its first statement ran (MySQL's CREATE TABLE, or BEGIN): before it a Cancel starts nothing
    created: bool = False  # MySQL's CREATE TABLE ran: the table stays, whatever comes next
    sent: bool = False  # a write went out: replace's DELETE, an INSERT
    deleted: bool = False  # replace's DELETE ran to its end (M-5)
    rows: int = 0  # taken from the source
    written: int = 0  # the rows of the INSERTs that went out, the failing one's included: what may stand (M-2)


def _internal(exc: Exception) -> StatementError:
    return StatementError(f"Internal error: {exc}")


class TransferSink(Job):
    kind = "copy_data"

    def __init__(self, command: CopyDataCommand, plan: StoredTransfer, source_dialect: Dialect) -> None:
        self._command = command
        self._plan = plan
        self._source_dialect = source_dialect

    @property
    def title(self) -> str:
        return self._plan.title

    @property
    def source_profile_id(self) -> UUID | None:
        return self._plan.source_profile_id

    def run(self, ctx: JobContext) -> JobReport:
        """What is refused before anything is sent is `not_started` (the worker's)."""
        plan, command = self._plan, self._command
        if ctx.hooks.read_only():
            raise StatementError(READ_ONLY_MESSAGE, code="read_only")
        if plan.query != (command.source_session_id is not None):
            raise StatementError("A query's copy reads on its tab's session, a table's on one of its own: preview the "
                                 "copy again", code="plan_mismatch")
        try:
            lease = ctx.hooks.source(plan.source_profile_id, plan.source_database, command.source_session_id)
        except PlanUnavailable as exc:
            raise StatementError(exc.message, code=exc.code) from None
        except DomainError as exc:  # a password, a server down, the tab's session gone: nothing was sent
            raise StatementError(SOURCE_UNAVAILABLE.format(exc.message), code=exc.code) from None
        copy: _Copy | None = None
        try:  # the lease is released whatever happens from here (review M-9)
            copy = _Copy(ctx, plan, lease, self._source_dialect)
            ctx.begin_record(plan.summary)
            logger.info("[copy] {} table(s): {}", len(plan.tables), plan.title)
            return copy.go()
        finally:
            if copy is not None:
                copy.settle()
            lease.release()


class _Copy:
    """One copy's tables, in order, and the outcome table of R-06-12."""

    def __init__(self, ctx: JobContext, plan: StoredTransfer, lease: SourceLease, source_dialect: Dialect) -> None:
        self.ctx = ctx
        self.plan = plan
        self.lease = lease
        self.source_dialect = source_dialect
        self.dialect = ctx.dialect
        self.frame = transaction_frame(ctx.dialect)
        self.statements = 0  # INSERTs sent and done
        self.created = False  # a CREATE TABLE went out (the schema cache is refreshed)
        self.source: TransferSource | None = None  # the last table's read, once submitted

    def go(self) -> JobReport:
        ctx = self.ctx
        try:
            for sql in TARGET_SETUP.get(self.dialect, ()):
                ctx.setting(sql)
        except StatementError as exc:  # no table started (pre-flight L-k)
            return self._report([], setup=exc)
        ended: list[_Table] = []
        for index, table in enumerate(self.plan.tables):
            if index and ctx.resume_cancels():  # a Cancel during the COMMIT before: this table does not start
                ended.append(_Table("not_started", error=StatementCancelled()))
                break
            if index and ctx.hooks.read_only():  # made read-only after the copy began: no table after it starts
                ended.append(_Table("not_started", error=StatementError(READ_ONLY_MESSAGE, code="read_only")))
                break
            done = self._table(index, table)
            ended.append(done)
            logger.log("WARNING" if done.outcome == "unknown" else "INFO", "[copy] {} → {}: {} ({:,} rows){}",
                       table.source_name, table.target_name, done.outcome, done.rows,
                       f" ({done.error.code})" if done.error is not None and done.error.code else "")
            if done.outcome != "copied":
                break
        self._reset()
        return self._report(ended)

    def settle(self) -> None:
        """Before the source's lease ends: a session the copy opened is closed only once its last read is over (its
        settings set back), within SOURCE_END_WAIT_S; a query tab's is the tab's again at once (a read it still runs
        ends at its next chunk, H2)."""
        if not self.plan.query and self.source is not None and not self.source.wait_ended(SOURCE_END_WAIT_S):
            logger.warning("[copy] the source's last read had not ended after {:g} s: closing its session",
                           SOURCE_END_WAIT_S)

    # ---- one table ----

    def _table(self, index: int, table: PlannedTable) -> _Table:
        ctx = self.ctx
        ctx.progress.stage, ctx.progress.object = "data", table.target_name
        ctx.tick(force=True)
        state = _Writing(transactional=table.transactional)
        try:
            self.created = self.created or table.created
            if table.create_first:  # MySQL: it commits at once, before the table's transaction
                ctx.drain(ctx.run(table.create_first))
                state.began = state.created = True
            if self.dialect is Dialect.MYSQL:  # its engine as the server has it now, not as the plan said (I-1)
                state.transactional = self._rolls_back(table, state.transactional)
            ctx.drain(ctx.run(self.frame.begin))
            state.began = True
            escapes = ctx.connection.backslash_escapes() if self.dialect is Dialect.MYSQL else None
            for sql in table.before:
                # Replace's DELETE is a write: a table that cannot roll back keeps what it did, even when an error or a
                # Cancel stops it part way (pre-flight H3)
                state.sent = True
                ctx.drain(ctx.run(sql))
            state.deleted = bool(table.before)
            batcher, converts = self._writer(table, escapes)
            pipe = RowPipe(table.source_name)
            source_id = f"{ctx.request_id}:source:{index + 1}"
            source = TransferSource(pipe, table, query=self.plan.query, label=self.plan.title)
            try:
                self.lease.worker.submit(PreparedJob(request_id=source_id, job=source))
            except DomainError as exc:  # the source's session closed (its tab, its connection deleted): nothing read
                raise SourceFailed(table.source_name, StatementError(exc.message, code=exc.code)) from None
            self.source = source
            try:
                while (chunk := pipe.take(ctx.raise_if_cancelled)) is not None:
                    for row in chunk:
                        state.rows += 1
                        if (statement := batcher.add(self._values(table, converts, row, state.rows))) is not None:
                            self._insert(statement, batcher.flushed_rows, state)
                    ctx.progress.rows += len(chunk)
                    ctx.tick()
                if (statement := batcher.flush()) is not None:
                    self._insert(statement, batcher.flushed_rows, state)
            except BaseException:
                pipe.close()  # the source's next chunk stops it
                self._stop_source(source_id)
                raise
            for sql in table.after:
                ctx.drain(ctx.run(sql))
        except StatementError as exc:
            return self._failed(table, exc, state)
        except Exception as exc:  # a bug: still the table's end as it is, not the whole copy's unknown
            logger.exception("[copy] copying {} failed", table.target_name)
            return self._failed(table, _internal(exc), state)
        return self._commit(table, state)

    def _rolls_back(self, table: PlannedTable, assumed: bool) -> bool:
        """MySQL: whether the table's engine rolls back, as the server says when the table starts. A table the copy
        created took the server's default engine (MyISAM on some servers), and another may have changed engines since
        the preview (review I-1). One it does not find: a table it just created is there, under a name the server
        spells otherwise (lower_case_table_names), so it may not roll back (Task 4 re-review N-2); another, as the plan
        said (the columns' read then says it is gone)."""
        found = table_rolls_back(self.ctx, table.target_schema, table.target_table)
        if found is None:
            return False if table.created else assumed
        return found

    def _stop_source(self, source_id: str) -> None:
        """A read on the copy's own session is cancelled; a query tab's only withdrawn while it waits in the tab's
        queue, never cancelled once it runs (H2: on PostgreSQL that aborts the tab's open transaction)."""
        if self.plan.query:
            self.lease.worker.withdraw(source_id)
        else:
            self.lease.worker.cancel(source_id)

    def _writer(self, table: PlannedTable, escapes: bool | None) -> tuple[InsertBatcher, list[Convert | None]]:
        """The INSERTs' writer for the target table as its catalog says now (after the plan's CREATE TABLE), and each
        written column's conversion."""
        try:
            found = {column.name: column for column in self.ctx.connection.table_columns(list(table.target_path))}
        except NotFound:  # dropped since the preview (pre-flight L-c): nothing of it was written
            raise StatementError(f"{table.target_name} is not there any more: preview the copy again",
                                 code=COLUMNS_CHANGED) from None
        except DomainError as exc:
            raise StatementError(exc.message, code=exc.code) from None
        targets: list[TableColumn] = []
        for planned in table.written:
            if planned.target not in found:
                raise StatementError(f"{table.target_name} has no column {planned.target} any more: preview the copy "
                                     "again", code=COLUMNS_CHANGED)
            targets.append(found[planned.target])
        converts = [conversion(table.columns[planned.source], target, self.source_dialect, self.dialect,
                               backslash_escapes=escapes).convert
                    for planned, target in zip(table.written, targets, strict=True)]
        name = qualified(self.dialect, table.target_schema, table.target_table)
        return InsertBatcher(self.dialect, name, targets, backslash_escapes=escapes, abort_on_conflict=True), converts

    @staticmethod
    def _values(table: PlannedTable, converts: list[Convert | None], row: list[Any], number: int) -> list[Any]:
        values = []
        for planned, convert in zip(table.written, converts, strict=True):
            value = row[planned.source]
            if convert is not None:
                try:
                    value = convert(value)
                except RowFailure as failure:
                    raise StatementError(f"Row {number:,} of {table.source_name}, column {planned.target}: "
                                         f"{failure.reason}", code=VALUE_NOT_COPYABLE) from None
            values.append(value)
        return values

    def _insert(self, sql: str, rows: int, state: _Writing) -> None:
        """One INSERT: through the session's gate, and it must report the rows it was given (⑤ D-05-5, A16)."""
        ctx = self.ctx
        if ctx.hooks.gate(sql).action != GuardAction.RUN:
            raise StatementError(READ_ONLY_MESSAGE, code="read_only")
        state.sent = True
        state.written += rows
        matched = ctx.matched(sql)
        if matched is None:
            raise StatementError("Could not confirm how many rows an INSERT wrote (a trigger returned rows?): the "
                                 "table was rolled back", code=ROW_COUNT_UNKNOWN)
        if matched != rows:
            raise StatementError(f"An INSERT of {rows:,} rows reported {matched:,} rows (a trigger may have skipped "
                                 "or changed them): the table was rolled back", code=ROW_COUNT_MISMATCH)
        self.statements += 1
        ctx.progress.statements = self.statements

    def _commit(self, table: PlannedTable, state: _Writing) -> _Table:
        """COMMIT, once nothing stopped the table (⑤ §3.5 step 7): no Cancel reaches it. Only a COMMIT that went out
        and whose answer was lost (or that left a transaction open) is unknown; whatever stops the table before it is
        `_failed`'s, an unexpected exception included (review M-3)."""
        ctx, connection = self.ctx, self.ctx.connection
        ctx.progress.stage = "commit"
        ctx.tick(force=True)
        cancelled = ctx.stop_cancels()
        try:
            if cancelled:
                return self._failed(table, StatementCancelled(), state)
            if connection.transaction_failed():
                return self._failed(table, StatementError("The transaction had failed", code="transaction_failed"),
                                    state)
            open_now = connection.in_transaction()
        except StatementError as exc:  # nothing of the COMMIT went out
            return self._failed(table, exc, state)
        except Exception as exc:
            logger.exception("[copy] checking {} before its COMMIT failed", table.target_name)
            return self._failed(table, _internal(exc), state)
        if not open_now:
            return self._unknown(table, state, StatementError(
                f"The transaction of {table.target_name} ended before its COMMIT: what was written may or may not "
                "stay", code=ENDED_BEFORE_COMMIT))
        try:
            ctx.drain(ctx.run(self.frame.commit))
        except StatementError as exc:
            if exc.code == CONNECTION_LOST:
                return self._unknown(table, state, exc)
            return self._failed(table, exc, state)
        except Exception as exc:  # the COMMIT may have gone out: what it did is not known
            logger.exception("[copy] the COMMIT of {} failed", table.target_name)
            return self._unknown(table, state, _internal(exc), rollback=True)
        try:
            left_open = connection.in_transaction()
        except Exception as exc:
            return self._unknown(table, state, exc if isinstance(exc, StatementError) else _internal(exc))
        if left_open:  # nested @@TRANCOUNT, or MySQL completion_type=CHAIN: what COMMIT did cannot be told (⑤ G11)
            return self._unknown(table, state, StatementError(
                "COMMIT returned but a transaction is still open: it was rolled back; what was copied is unknown",
                code="commit_unconfirmed"), rollback=True)
        return _Table("copied", rows=state.rows)

    def _failed(self, table: PlannedTable, error: StatementError, state: _Writing) -> _Table:
        """A table that stopped before its COMMIT went out: rolled back, unless
        - a Cancel came before its first statement ran: not_started (review M-4);
        - the server ended its transaction itself (a SQL Server trigger's COMMIT, 3609): unknown;
        - it cannot roll back and a write went out (⑤ G10, pre-flight H3, review I-1): unknown;
        - MySQL's ROLLBACK says some changes stayed (1196: a table a trigger wrote, R3): unknown."""
        self.ctx.stop_cancels()
        name = table.target_name
        if isinstance(error, StatementCancelled) and not state.began:
            return _Table("not_started", error=error)
        if error.code in TRANSACTION_ENDED.get(self.dialect, frozenset()):
            return self._unknown(table, state, error, said=[TRIGGER_ENDED.format(name)], rollback=True)
        if state.sent and not state.transactional:
            return self._unknown(table, state, error, said=[self._kept(table, state)], rollback=True)
        if error.code == CONNECTION_LOST:  # the server rolls the open transaction back as the connection ends
            return _Table("rolled_back", error=error, warnings=self._created(table, state))
        if not self._rollback():
            return _Table("rolled_back", error=error, warnings=[*self._created(table, state), JOB_ROLLBACK_FAILED])
        if self.dialect is Dialect.MYSQL and kept_changes(self.ctx, "copy"):
            said = [KEPT, *([CREATED_CHECK.format(name)] if state.created else [])]
            return self._unknown(table, state, error, said=said, stand=SOME_MAY_STAND)
        return _Table("rolled_back", error=error, warnings=self._created(table, state))

    @staticmethod
    def _kept(table: PlannedTable, state: _Writing) -> str:
        """What a table that cannot roll back keeps, by what went out."""
        if state.created:
            return CREATED_KEPT.format(table.target_name)
        if table.before:  # replace's DELETE: done, or stopped part way (review M-5)
            return (DELETED_STAY if state.deleted else DELETE_STOPPED).format(table.target_name)
        return NOT_ROLLED_BACK.format(table.target_name)

    def _unknown(self, table: PlannedTable, state: _Writing, error: StatementError, *, said: list[str] | None = None,
                 rollback: bool = False, stand: str | None = None) -> _Table:
        """A table whose rows may or may not stand: why, its transaction ended first when `rollback` (and said when
        that failed, review M-10), and how many rows may stand at most (its object counts none, review M-2), or
        `stand` when what may stand is not this table's rows (N-1)."""
        warnings = list(said or [])
        if rollback and not self._rollback():
            warnings.append(JOB_ROLLBACK_FAILED)
        if stand is not None:
            warnings.append(stand)
        elif state.written:
            warnings.append(MAY_STAND.format(state.written, table.target_name))
        return _Table("unknown", error=error, warnings=warnings)

    @staticmethod
    def _created(table: PlannedTable, state: _Writing) -> list[str]:
        """A created table that rolled back: it stays, and nothing was written to it that stands."""
        return [CREATED_EMPTY.format(table.target_name)] if state.created else []

    def _rollback(self) -> bool:
        """End the table's transaction; whether it is ended (a connection that is gone has none)."""
        self.ctx.stop_cancels()
        try:
            self.ctx.connection.rollback()
            return True
        except Exception as exc:  # above DEBUG only its code: a server's text may echo what it read (D-06-12)
            logger.warning("[copy] the rollback of a stopped table failed ({})",
                           getattr(exc, "code", None) or type(exc).__name__)
        try:
            return not self.ctx.connection.in_transaction()
        except Exception:
            return True  # it cannot tell: as for a connection that is gone

    def _reset(self) -> None:
        """The session's settings back (it is closed after the job; a courtesy)."""
        self.ctx.stop_cancels()
        for sql in TARGET_RESET.get(self.dialect, ()):
            try:
                self.ctx.setting(sql)
            except StatementError as exc:
                logger.info("[copy] resetting the target session failed ({})", exc.code)
                return

    # ---- the outcome ----

    def _report(self, ended: list[_Table], *, setup: StatementError | None = None) -> JobReport:
        """The copy's outcome: every table of the plan with its own (those after the last one that ended:
        not_started), rows only for the tables copied. A setup that failed started no table: rolled_back once it sent
        something, not_started when it sent nothing (a Cancel before its first statement)."""
        ctx, plan = self.ctx, self.plan
        padded = ended + [_Table("not_started")] * (len(plan.tables) - len(ended))
        objects = [JobObject(kind="table", name=table.target_name, source=table.source_name, rows=done.rows,
                             outcome=done.outcome) for table, done in zip(plan.tables, padded, strict=True)]
        outcomes = [item.outcome for item in objects]
        copied = outcomes.count("copied")
        overall: JobOutcomeKind
        if setup is not None:
            overall = "rolled_back" if ctx.sent else "not_started"
        elif "unknown" in outcomes:
            overall = "unknown"
        elif copied == len(outcomes):
            overall = "completed"
        elif copied:
            overall = "partial"
        else:
            overall = "rolled_back"
        error = setup if setup is not None else ended[-1].error if ended else None
        warnings = [warning for done in ended for warning in done.warnings]
        left = [item.name for item in objects if item.outcome == "not_started"]
        if left and len(left) < len(objects):
            names = ", ".join(left[:10]) + (" …" if len(left) > 10 else "")
            warnings.append(NOT_STARTED.format(plural("table", len(left)), names))
        rows = sum(item.rows for item in objects if item.outcome == "copied")
        outcome = ctx.outcome(overall, rows=rows, statements=self.statements, objects=objects, warnings=warnings,
                              cancelled=isinstance(error, StatementCancelled))
        return JobReport(outcome, error, schema_changed=self.created)
