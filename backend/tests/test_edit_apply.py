"""The outcome of an apply (spec ⑤ §3.5's table, R-05-5, R-05-6, D-05-5, D-05-6; rulings E11, E12, E14): one test per
row, pure."""

import pytest

from dbaguy_backend.adapters.base import StatementCancelled, StatementError
from dbaguy_backend.domain.edits import PlannedStatement
from dbaguy_backend.sessions.edit_apply import EditProgress, EditReport, history_error, refused

LOST = StatementError("Connection to the server was lost", code="connection_lost")
SERVER = StatementError("duplicate key value violates unique constraint", code="23505")


def progress(*kinds: str, transactional: bool = True) -> EditProgress:
    statements = [PlannedStatement(change_id=f"c{i}", kind=kind, sql=f"{kind} {i}", row=f"id = {i}")
                  for i, kind in enumerate(kinds)]
    started = EditProgress(statements, transactional)
    started.sent = True
    return started


def run(started: EditProgress, *rows: int | None) -> StatementError | None:
    """Statement i matched rows[i], until one stops the apply (its error) or all ran (None)."""
    for index, count in enumerate(rows):
        started.index = index
        if (error := started.matched(count)) is not None:
            return error
    return None


def test_every_statement_matching_as_planned_commits():
    started = progress("delete", "update", "insert")
    assert run(started, 0, 1, 1) is None
    report = started.committed()
    assert (report.outcome, report.applied, report.already_gone, report.error) == (
        "committed", ["c0", "c1", "c2"], ["c0"], None)


@pytest.mark.parametrize(("kind", "rows", "code", "text"), [
    ("update", 0, "edit_conflict",
     "Row id = 0 was changed or deleted since it was loaded (or a trigger skipped it): nothing was applied"),
    ("update", 2, "edit_conflict", "Row id = 0 matched 2 rows: its key is not unique. Nothing was applied"),
    ("delete", 2, "edit_conflict", "Row id = 0 matched 2 rows: its key is not unique. Nothing was applied"),
    ("insert", 0, "edit_conflict", "The new row was not inserted (a trigger may have skipped it): nothing was applied"),
    ("insert", 2, "edit_conflict", "Inserting one row reported 2 rows: nothing was applied"),
    ("update", None, "row_count_unknown", "Could not confirm how many rows changed: nothing was applied"),
    ("delete", -1, "row_count_unknown", "Could not confirm how many rows changed: nothing was applied"),
])
def test_a_statement_that_matched_wrongly_stops_the_apply(kind, rows, code, text):  # R-05-5, D-05-5, spec §4
    started = progress(kind)
    error = run(started, rows)
    assert (error.code, error.message) == (code, text)
    report = started.failed(error)
    assert (report.outcome, report.failed_change, report.statement, report.applied) == ("rolled_back", "c0", 0, [])


@pytest.mark.parametrize("error", [SERVER, LOST, StatementCancelled("canceling statement due to user request")])
def test_any_failure_before_the_commit_rolls_everything_back(error):  # R-05-6
    started = progress("update", "update")
    run(started, 1)
    started.index = 1
    report = started.failed(error)
    assert (report.outcome, report.failed_change, report.statement, report.applied) == ("rolled_back", "c1", 1, [])
    if isinstance(error, StatementCancelled):
        assert (report.error.code, report.error.message) == ("cancelled", "Cancelled: nothing was applied")


def test_refused_before_anything_was_sent():
    report = refused(StatementError("The preview has expired: preview the changes again", code="plan_expired"))
    assert (report.outcome, report.applied, report.error.code) == ("rolled_back", [], "plan_expired")
    assert refused(StatementCancelled()).error.message == "Cancelled: nothing was applied"


def test_before_the_commit_a_cancel_or_a_failed_transaction_rolls_back_and_a_vanished_one_is_unknown():  # ruling E12
    started = progress("update")
    run(started, 1)
    cancelled, aborted, vanished = (started.cancelled_before_commit(), started.aborted_before_commit(),
                                    started.ended_before_commit())
    assert (cancelled.outcome, cancelled.error.message) == ("rolled_back", "Cancelled: nothing was applied")
    assert (aborted.outcome, aborted.error.code) == ("rolled_back", "transaction_failed")
    assert (vanished.outcome, vanished.error.code) == ("unknown", "outcome_unknown")


def test_a_commit_the_server_refuses_rolls_back_and_a_lost_one_is_unknown():  # R-05-6, A13
    started = progress("update")
    run(started, 1)
    refusal = started.commit_failed(StatementError("insert or update violates foreign key constraint", code="23503"))
    assert (refusal.outcome, refusal.statement, refusal.applied, refusal.error.code) == ("rolled_back", None, [], "23503")
    assert refusal.error.message == (
        "insert or update violates foreign key constraint\nThe transaction was rolled back: nothing was applied")
    lost = started.commit_failed(LOST)
    assert (lost.outcome, lost.applied, lost.error) == ("unknown", [], LOST)


def test_a_table_that_cannot_roll_back_says_what_took_effect():  # D-05-6, ruling E11
    started = progress("update", "update", "update", transactional=False)
    error = run(started, 1, 0)
    assert error.message.endswith(": the statements before it stay applied (this table cannot roll back)")
    partial = started.failed(error)
    assert (partial.outcome, partial.applied, partial.failed_change, partial.statement) == ("partial", ["c0"], "c1", 1)
    first = progress("update", "update", transactional=False)
    assert first.failed(run(first, 0)).outcome == "rolled_back"  # nothing took effect
    lost = progress("update", "update", transactional=False)
    run(lost, 1)
    lost.index = 1
    assert (lost.failed(LOST).outcome, lost.failed(LOST).applied) == ("unknown", ["c0"])
    done = progress("update", transactional=False)
    run(done, 1)
    stood = done.commit_failed(SERVER)
    assert (stood.outcome, stood.applied, stood.error) == ("committed", ["c0"], SERVER)


def test_a_bug_is_an_unknown_outcome_once_something_went_out():  # R-05-6
    assert EditProgress([], True).broke(RuntimeError("bug")).outcome == "rolled_back"
    report = progress("update").broke(RuntimeError("bug"))
    assert (report.outcome, report.error.message) == ("unknown", "Internal error: bug")


def test_history_keeps_ok_cancelled_or_the_error():  # D-05-9, ruling E14
    started = progress("update", "update", transactional=False)
    partial = started.failed(run(started, 1, 0))
    assert history_error(progress("update").committed(), 1) is None
    assert history_error(EditReport("rolled_back", error=StatementCancelled("Cancelled: nothing was applied")), 1) is None
    assert history_error(EditReport("unknown", error=LOST), 1).message == (
        "Outcome unknown: Connection to the server was lost")
    assert history_error(partial, 2).message.startswith("Partly applied (1 of 2): Row id = 1 was changed")
    assert history_error(EditReport("rolled_back", error=SERVER), 1) is SERVER


@pytest.mark.parametrize(("kind", "rows", "text"), [
    ("update", None, "Could not confirm how many rows changed: this change may have taken effect, and the statements "
                     "before it stay applied (this table cannot roll back)"),
    ("update", 2, "Row id = 1 matched 2 rows: its key is not unique. This change may have taken effect, and the "
                  "statements before it stay applied (this table cannot roll back)"),
    ("insert", 2, "Inserting one row reported 2 rows: this change may have taken effect, and the statements before it "
                  "stay applied (this table cannot roll back)"),
])
def test_on_a_table_that_cannot_roll_back_a_statement_that_ran_unmatched_is_unknown(kind, rows, text):  # L3
    # It ran: whatever it changed stays, so neither "nothing was applied" nor "only the ones before it" is true
    alone = progress(kind, transactional=False)
    assert alone.failed(run(alone, rows)).outcome == "unknown"
    started = progress("update", kind, transactional=False)
    error = run(started, 1, rows)
    assert error.message == text
    report = started.failed(error)
    assert (report.outcome, report.applied, report.failed_change, report.statement) == ("unknown", ["c0"], "c1", 1)
    assert history_error(report, 2).message == f"Outcome unknown: {text}"


def test_a_commit_that_left_a_transaction_open_is_unknown_unless_nothing_could_roll_back():  # pre-flight L7
    started = progress("update")
    run(started, 1)
    left = started.commit_unconfirmed()
    assert (left.outcome, left.applied, left.error.code) == ("unknown", [], "outcome_unknown")
    assert left.error.message == (
        "COMMIT left a transaction open, which was rolled back: the changes may or may not have been applied")
    assert started.commit_unconfirmed(LOST).error is LOST
    done = progress("update", transactional=False)
    run(done, 1)
    assert (done.commit_unconfirmed().outcome, done.commit_unconfirmed(LOST).error) == ("committed", None)
