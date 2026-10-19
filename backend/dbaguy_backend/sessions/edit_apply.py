"""How an apply of a grid-edit plan ends (spec ⑤ §3.5, R-05-5, R-05-6, D-05-6): what each statement must have matched,
and the outcome — committed, rolled_back, partial or unknown — for every way the apply can stop. Pure: the worker does
the I/O and asks here. Messages name the row by its key: they go to the user's dialog, never to a log."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..adapters.base import StatementCancelled, StatementError
from ..domain.edits import PlannedStatement
from ..domain.execution import EditOutcome

CONNECTION_LOST = "connection_lost"
EDIT_CONFLICT = "edit_conflict"
ROW_COUNT_UNKNOWN = "row_count_unknown"
OUTCOME_UNKNOWN = "outcome_unknown"
TRANSACTION_FAILED = "transaction_failed"
NOTHING_APPLIED = "nothing was applied"
EARLIER_STAY = "the statements before it stay applied (this table cannot roll back)"
# A table that cannot roll back, whose statement ran but matched more than one row or an unreadable count: what it
# changed stays too (pre-flight L3)
THIS_MAY_STAY = ("this change may have taken effect, and the statements before it stay applied (this table cannot "
                 "roll back)")
# Pre-flight L7: COMMIT returned with a transaction still open; the worker rolls that one back
COMMIT_LEFT_OPEN = "COMMIT left a transaction open, which was rolled back: the changes may or may not have been applied"
ROLLBACK_FAILED = "The transaction could not be rolled back: close and reopen this tab"  # pre-flight L8


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
    """Stopped with nothing applied: before the BEGIN went out, by the BEGIN itself, or (a transactional target)
    rolled back before COMMIT because its state could not be read."""
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

    def _tail(self) -> str:
        if self.transactional:
            return NOTHING_APPLIED
        return THIS_MAY_STAY if self.uncertain else EARLIER_STAY

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
        tail = self._tail()
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
        """The current statement failed, matched wrongly, was cancelled, or lost its connection (spec §3.5)."""
        statement = self._current()
        common = {"failed_change": statement.change_id, "statement": self.index,
                  "error": _as_cancel(error, self._tail())}
        if self.transactional:
            return EditReport("rolled_back", **common)
        done = {"applied": list(self.applied), "already_gone": list(self.gone)}
        if error.code == CONNECTION_LOST or self.uncertain:  # this statement may or may not have taken effect
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
