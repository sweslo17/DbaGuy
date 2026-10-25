"""How an apply of a grid-edit plan ends (spec ⑤ §3.5, R-05-5, R-05-6, D-05-6): what each statement must have matched,
and the outcome — committed, rolled_back, partial or unknown — for every way the apply can stop. Pure: the worker does
the I/O and asks here. Messages name the row by its key: they go to the user's dialog, never to a log."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..adapters.base import CONNECTION_LOST, StatementCancelled, StatementError
from ..domain.dialect import Dialect
from ..domain.edits import PlannedStatement
from ..domain.execution import EditOutcome

EDIT_CONFLICT = "edit_conflict"
ROW_COUNT_UNKNOWN = "row_count_unknown"
OUTCOME_UNKNOWN = "outcome_unknown"
TRANSACTION_FAILED = "transaction_failed"
NOTHING_APPLIED = "nothing was applied"
EARLIER_STAY = "the statements before it stay applied (this table cannot roll back)"
# A table that cannot roll back, whose statement went out and failed (but not by matching no row): what it changed stays
# too (pre-flight L3, rulings G10, G13); without statements before it, only its own change may stay
THIS_MAY_STAY = ("this change may have taken effect, and the statements before it stay applied (this table cannot "
                 "roll back)")
THIS_MAY_HAVE = "this change may have taken effect (this table cannot roll back)"
# Server errors that end the transaction inside the statement, which may have committed what it held: SQL Server 3609
# "The transaction ended in the trigger" — a trigger ran COMMIT TRANSACTION (or ROLLBACK). By dialect: the same number
# means something else elsewhere (MySQL 3609 is ER_IMP_SCHEMA_DOES_NOT_EXIST).
TRANSACTION_ENDED: dict[Dialect, frozenset[str]] = {Dialect.TSQL: frozenset({"3609"})}
# Pre-flight L7: COMMIT returned with a transaction still open; the worker rolls that one back
COMMIT_LEFT_OPEN = "COMMIT left a transaction open, which was rolled back: the changes may or may not have been applied"
# Pre-flight L8. src/features/edits/commitFlow.ts matches its start (only as a fallback for finished.in_transaction)
ROLLBACK_FAILED = "The transaction could not be rolled back: close and reopen this tab"
# The warning of a commit that stood though its COMMIT failed (a table that cannot roll back), before the server's text.
# src/features/edits/commitFlow.ts matches its start to toast it on its own: change both together
COMMIT_WARNING = "COMMIT reported an error after every statement had taken effect"
# The plan's literals read backslashes as the metadata connection's sql_mode did at the preview (D-05-13); the session
# that applies reads them differently (a GLOBAL sql_mode changed between the two connects, a proxy's backends differ):
# refused right after BEGIN, before any DML (05a review M2). Only MySQL says how its session reads them.
SQL_MODE_CHANGED = "sql_mode_changed"
SQL_MODE_CHANGED_MESSAGE = "The session's sql_mode changed since the preview: preview the changes again"


@dataclass(frozen=True)
class EditReport:
    """What the apply's last events say: its EditOutcomeEvent, and the error before it."""

    outcome: EditOutcome
    applied: list[str] = field(default_factory=list)
    already_gone: list[str] = field(default_factory=list)
    failed_change: str | None = None
    statement: int | None = None
    error: StatementError | None = None


def _as_cancel(error: StatementError, tail: str) -> StatementError:
    """A cancel reads as the spec words it (`Cancelled: nothing was applied`), whatever the adapter said."""
    return StatementCancelled(f"Cancelled: {tail}") if isinstance(error, StatementCancelled) else error


def refused(error: StatementError) -> EditReport:
    """Stopped with nothing applied: before the BEGIN went out, by the BEGIN itself or right after it (no DML sent), or
    (a transactional target) rolled back before COMMIT because its state could not be read."""
    return EditReport("rolled_back", error=_as_cancel(error, NOTHING_APPLIED))


@dataclass
class EditProgress:
    """Where an apply is. The worker moves it on; each way of stopping asks it for the outcome (spec §3.5's table)."""

    statements: Sequence[PlannedStatement]
    transactional: bool
    sent: bool = False  # the BEGIN went out
    index: int | None = None  # the statement running, or the last one that ran
    committing: bool = False  # the COMMIT went out: a lost answer from here is an unknown outcome
    applied: list[str] = field(default_factory=list)  # statements that ran and matched as planned
    gone: list[str] = field(default_factory=list)  # DELETEs that matched no row: already gone (R-05-5)
    # The current statement ran but matched more than one row, or a count that cannot be read: it may have changed
    # rows, which a table that cannot roll back keeps (pre-flight L3)
    uncertain: bool = False
    # The current statement passed the worker's last cancel check and went to run(): on a table that cannot roll back,
    # whatever stops it from here may have left its change behind (an AFTER trigger's error, a KILL that came late)
    reached: bool = False
    dialect: Dialect | None = None  # which server error codes end the transaction (TRANSACTION_ENDED)

    def _tail(self, may_have_changed: bool) -> str:
        """What the message adds about what stays: on a table that cannot roll back, the statements before this one,
        and this one's own change when it may have taken effect; nothing at the first statement that changed nothing
        (fix round 1, item 3)."""
        if self.transactional:
            return NOTHING_APPLIED
        if may_have_changed:
            return THIS_MAY_STAY if self.applied else THIS_MAY_HAVE
        return EARLIER_STAY if self.applied else NOTHING_APPLIED

    def _may_have_changed(self, error: StatementError) -> bool:
        """On a table that cannot roll back, whether the failed statement may have left a change behind (rulings G10,
        G13): its connection was lost, its count was more than one or unreadable, or it went out and failed for any
        reason but matching no row — a server error, a failing trigger, a cancel after it was sent."""
        if error.code == CONNECTION_LOST or self.uncertain:
            return True
        return self.reached and error.code != EDIT_CONFLICT  # a conflict without `uncertain` matched no row

    def _current(self) -> PlannedStatement:
        if self.index is None:
            raise ValueError("no statement has run")
        return self.statements[self.index]

    def matched(self, rows: int | None) -> StatementError | None:
        """The current statement matched `rows` (R-05-5): None when that is what it had to match — it then counts as
        applied — else the error that stops the apply. An UPDATE and an INSERT match exactly one row, a DELETE at most
        one; a count that cannot be read fails closed (D-05-5)."""
        statement = self._current()
        if rows == 1 or (statement.kind == "delete" and rows == 0):
            self.applied.append(statement.change_id)
            if rows == 0:
                self.gone.append(statement.change_id)
            return None
        self.uncertain = rows != 0  # None, negative or more than one: the statement may have changed rows
        tail = self._tail(self.uncertain)
        if rows is None or rows < 0:
            return StatementError(f"Could not confirm how many rows changed: {tail}", code=ROW_COUNT_UNKNOWN)
        if statement.kind == "insert":
            text = (f"The new row was not inserted (a trigger may have skipped it): {tail}" if rows == 0
                    else f"Inserting one row reported {rows} rows: {tail}")
        elif rows == 0:
            text = f"Row {statement.row} was changed or deleted since it was loaded (or a trigger skipped it): {tail}"
        else:
            text = f"Row {statement.row} matched {rows} rows: its key is not unique. {tail[0].upper()}{tail[1:]}"
        return StatementError(text, code=EDIT_CONFLICT)

    def failed(self, error: StatementError) -> EditReport:
        """The current statement failed, matched wrongly, was cancelled, or lost its connection (spec §3.5). A cancel
        caught before it was sent names no change: nothing of it went out (fix round 1, item 4)."""
        statement = self._current()
        unsent = isinstance(error, StatementCancelled) and not self.reached
        changed = not self.transactional and self._may_have_changed(error)
        common = {"failed_change": None if unsent else statement.change_id, "statement": None if unsent else self.index,
                  "error": _as_cancel(error, self._tail(changed))}
        if self.transactional:
            ended = self.dialect is not None and error.code in TRANSACTION_ENDED.get(self.dialect, frozenset())
            return EditReport("unknown" if ended else "rolled_back", **common)  # ended: what it held may be committed
        done = {"applied": list(self.applied), "already_gone": list(self.gone)}
        if changed:
            return EditReport("unknown", **done, **common)
        return EditReport("partial" if self.applied else "rolled_back", **done, **common)

    def cancelled_before_commit(self) -> EditReport:
        return EditReport("rolled_back", error=StatementCancelled(f"Cancelled: {NOTHING_APPLIED}"))

    def aborted_before_commit(self) -> EditReport:
        """The transaction can only roll back now (PostgreSQL: aborted; SQL Server: uncommittable)."""
        return EditReport("rolled_back", error=StatementError(
            f"The transaction had failed: {NOTHING_APPLIED}", code=TRANSACTION_FAILED))

    def ended_before_commit(self) -> EditReport:
        """The transaction is gone without an error (spec §3.5 step 7): what happened to it is not known."""
        return EditReport("unknown", error=StatementError(
            "The transaction ended before COMMIT: the changes may or may not have been applied", code=OUTCOME_UNKNOWN))

    def commit_failed(self, error: StatementError) -> EditReport:
        """COMMIT went out and failed (R-06): refused by the server → rolled back; its answer lost → unknown. A table
        that cannot roll back had every statement take effect already (ruling E11): committed, the error a warning."""
        if not self.transactional:
            return self.committed(warning=error)
        if error.code == CONNECTION_LOST:
            return EditReport("unknown", error=error)
        return EditReport("rolled_back", error=StatementError(
            f"{error.message}\nThe transaction was rolled back: {NOTHING_APPLIED}", code=error.code))

    def commit_unconfirmed(self, error: StatementError | None = None) -> EditReport:
        """COMMIT returned, yet a transaction is still open (`error` None), or the connection could not say (pre-flight
        L7). Which of two things happened is not known: SQL Server's COMMIT TRANSACTION under a trigger's BEGIN TRAN
        only lowers @@TRANCOUNT (nothing committed; the worker's rollback then undoes every level), while MySQL with
        `completion_type=CHAIN` commits and opens the next transaction at once. So: unknown. A table that cannot roll
        back had every statement take effect already (ruling E11): committed."""
        if not self.transactional:
            return self.committed()
        return EditReport("unknown", error=error or StatementError(COMMIT_LEFT_OPEN, code=OUTCOME_UNKNOWN))

    def committed(self, warning: StatementError | None = None) -> EditReport:
        return EditReport("committed", applied=[s.change_id for s in self.statements], already_gone=list(self.gone),
                          error=warning)

    def broke(self, exc: Exception) -> EditReport:
        """A bug (not a StatementError): before the BEGIN went out nothing was applied; after it, unknown (R-06)."""
        error = StatementError(f"Internal error: {exc}")
        if not self.sent:
            return EditReport("rolled_back", error=error)
        if self.transactional:
            return EditReport("unknown", error=error)
        return EditReport("unknown", applied=list(self.applied), already_gone=list(self.gone), error=error)


def history_error(report: EditReport, total: int) -> StatementError | None:
    """What History keeps as the apply's error (D-05-9, ruling E14): none for a commit or a cancel (status ok or
    cancelled), `Outcome unknown: …`, `Partly applied (n of m): …`, else the error itself."""
    error = report.error
    if report.outcome == "committed" or (report.outcome == "rolled_back" and isinstance(error, StatementCancelled)):
        return None
    message = error.message if error is not None else ""
    if report.outcome == "unknown":
        return StatementError(f"Outcome unknown: {message}", code=OUTCOME_UNKNOWN)
    if report.outcome == "partial":
        return StatementError(f"Partly applied ({len(report.applied)} of {total}): {message}",
                              code=error.code if error is not None else None)
    return error
