"""Execution protocol on top of the worker: policy, timeout, transaction commands, session state, history and schema
hooks. Driven by the fake connection so every ordering is checked deterministically."""

import threading
import time

import pytest

from dbaguy_backend.domain.execution import (
    CommitCommand,
    ExecuteCommand,
    ExportCommand,
    FetchMoreCommand,
    RollbackCommand,
    SetAutocommitCommand,
)
from dbaguy_backend.domain.formats import FormatKind
from dbaguy_backend.services.safety import GuardAction, GuardDecision, SafetyPolicy
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from dbaguy_backend.sql.classifier import Classification, StatementKind
from dbaguy_backend.sql.dialects import Dialect
from tests.fake_db import FakeLease, FakeResult
from tests.fake_ssh import run_in_thread
from tests.test_sessions import Recorder

policy = SafetyPolicy()


def standard(read_only=False, level="standard"):
    return lambda sql, confirmed: policy.check(sql, Dialect.SQLITE, level, read_only, confirmed)


@pytest.fixture
def make_worker():
    workers = []

    def make(**hooks):
        lease = FakeLease()
        worker = SessionWorker("fake", lambda: lease)
        worker.start()
        worker.configure(WorkerHooks(**hooks))
        recorder = Recorder()
        worker.set_emitter(recorder)
        workers.append(worker)
        return worker, lease.connection, recorder

    yield make
    for worker in workers:
        worker.close()


def run(worker, recorder, rid, sql, **fields):
    worker.submit(ExecuteCommand(request_id=rid, sql=sql, **fields))
    return recorder.wait_for(lambda e: e.type == "finished" and e.request_id == rid)


def command(worker, recorder, cmd):
    worker.submit(cmd)
    return recorder.wait_for(lambda e: e.type == "finished" and e.request_id == cmd.request_id)


def last_state(recorder):
    return [e for e in recorder.events if e.type == "session_state"][-1]


def test_confirmation_is_required_every_time_until_confirmed(make_worker):
    worker, conn, rec = make_worker(check=standard())
    done = run(worker, rec, "a", "DELETE FROM t")
    assert done.skipped and conn.runs == []
    [confirm] = rec.of("a", "confirmation_required")
    assert confirm.reasons == ["delete_without_where"] and confirm.classification.kind == "write"
    run(worker, rec, "b", "DELETE FROM t", confirmed=True)
    assert conn.runs == ["DELETE FROM t"]
    assert run(worker, rec, "c", "DELETE FROM t").skipped  # replaying without confirmation is checked again
    assert conn.runs == ["DELETE FROM t"]


def test_read_only_blocks_before_reaching_the_connection(make_worker):
    worker, conn, rec = make_worker(check=standard(read_only=True))
    run(worker, rec, "a", "INSERT INTO t VALUES (1)", confirmed=True)
    [error] = rec.of("a", "error")
    assert error.code == "read_only" and conn.runs == []
    run(worker, rec, "b", "SELECT 1")
    assert conn.runs == ["SELECT 1"]


def test_timeout_cancels_the_statement(make_worker):
    worker, conn, rec = make_worker()
    done = run(worker, rec, "a", "block", timeout_s=0.2)
    assert done.cancelled and done.timed_out and conn.cancel_calls == 1
    assert rec.of("a", "message")[-1].text == "Timed out after 0.2 s"
    assert not run(worker, rec, "b", "rows 1").timed_out  # the session keeps working


def test_timeout_stops_counting_once_the_first_page_is_out(make_worker):
    worker, conn, rec = make_worker()
    worker.submit(ExecuteCommand(request_id="a", sql="rows 50", page_size=10, timeout_s=0.2))
    rec.wait_for(lambda e: e.type == "result_set_end" and e.has_more)
    time.sleep(0.4)
    assert conn.cancel_calls == 0
    worker.submit(FetchMoreCommand(request_id="a", count=100))
    assert not rec.wait_for(lambda e: e.type == "finished" and e.request_id == "a").timed_out


def test_history_records(make_worker):
    records = []
    worker, _, rec = make_worker(check=standard(), on_record=records.append)
    run(worker, rec, "ok", "rows 3")
    run(worker, rec, "err", "fail no such table")
    run(worker, rec, "quiet", "rows 1", history=False)
    run(worker, rec, "skip", "DELETE FROM t")  # needs confirmation: never ran
    run(worker, rec, "slow", "block", timeout_s=0.1)
    assert [(r.request_id, r.status, r.rows) for r in records] == [
        ("ok", "ok", 3), ("err", "error", 0), ("slow", "timeout", 0),
    ]
    assert records[1].error == "no such table" and records[0].sql == "rows 3"


def ddl_aware(sql, confirmed):
    kind = StatementKind.DDL if sql.startswith("ddl") else StatementKind.READ
    return GuardDecision(action=GuardAction.RUN, classification=Classification(kind=kind))


def test_failing_hooks_never_affect_execution(make_worker):
    def explode(*_):
        raise RuntimeError("hook bug")

    worker, _, rec = make_worker(check=ddl_aware, on_record=explode, on_schema_change=explode)
    done = run(worker, rec, "a", "ddl create")
    assert not done.cancelled and rec.of("a", "error") == []
    assert not run(worker, rec, "b", "rows 1").cancelled


def test_schema_change_is_reported_once_other_connections_can_see_it(make_worker):
    changes = []
    worker, _, rec = make_worker(check=ddl_aware, on_schema_change=lambda: changes.append(1))
    run(worker, rec, "a", "ddl create")
    assert len(changes) == 1
    run(worker, rec, "b", "begin")
    run(worker, rec, "c", "ddl alter")
    assert len(changes) == 1  # still inside the transaction
    worker.submit(CommitCommand(request_id="d"))
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "d")
    assert len(changes) == 2


def test_transaction_commands_and_session_state(make_worker):
    worker, conn, rec = make_worker()
    run(worker, rec, "a", "begin")
    state = [e for e in rec.events if e.type == "session_state"][-1]
    assert state.in_transaction and state.transaction_started_at is not None and state.autocommit
    worker.submit(SetAutocommitCommand(request_id="b", enabled=False))
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "b")
    assert rec.of("b", "error")[0].code == "transaction_open"
    worker.submit(RollbackCommand(request_id="c"))
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "c")
    assert conn.rollbacks == 1
    worker.submit(SetAutocommitCommand(request_id="d", enabled=False))
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "d")
    state = [e for e in rec.events if e.type == "session_state"][-1]
    assert not state.in_transaction and not state.autocommit and state.transaction_started_at is None


def test_commit_of_a_failed_transaction_warns_that_it_rolled_back(make_worker):
    worker, conn, rec = make_worker()
    run(worker, rec, "a", "begin")
    run(worker, rec, "b", "fail division by zero")
    assert last_state(rec).transaction_failed
    done = command(worker, rec, CommitCommand(request_id="c"))
    assert [(m.severity, m.text) for m in rec.of("c", "message")] == [
        ("warning", "The transaction had failed, so COMMIT rolled it back"),
    ]
    assert conn.commits == 0 and conn.rollbacks == 1
    assert not done.in_transaction and not done.transaction_failed
    run(worker, rec, "d", "begin")  # a healthy transaction commits without the warning
    command(worker, rec, CommitCommand(request_id="e"))
    assert rec.of("e", "message") == [] and conn.commits == 1


def test_every_request_ends_with_session_state_then_one_finished(make_worker):
    records, recorded_at_finish = [], {}
    worker, conn, rec = make_worker(check=standard(), on_record=records.append)

    def emit(event):  # whoever has seen `finished` can rely on the history record
        if event.type == "finished":
            recorded_at_finish[event.request_id] = [r.request_id for r in records]
        rec(event)

    worker.set_emitter(emit)
    run(worker, rec, "ok", "rows 2")
    run(worker, rec, "confirm", "DELETE FROM t")
    run(worker, rec, "error", "fail boom")
    run(worker, rec, "timeout", "block", timeout_s=0.05)
    command(worker, rec, CommitCommand(request_id="commit"))
    command(worker, rec, FetchMoreCommand(request_id="ghost"))
    worker.submit(ExecuteCommand(request_id="paused", sql="rows 5", page_size=2))
    rec.wait_for(lambda e: e.type == "result_set_end" and e.has_more)
    run(worker, rec, "next", "rows 1")  # discards the paused result first
    conn.blocked.clear()
    worker.submit(ExecuteCommand(request_id="slow", sql="block"))
    assert conn.blocked.wait(5)
    worker.submit(ExecuteCommand(request_id="queued", sql="rows 1"))
    worker.cancel("queued")
    worker.cancel("slow")
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "queued")

    ids = ["ok", "confirm", "error", "timeout", "commit", "ghost", "paused", "next", "slow", "queued"]
    for rid in ids:
        assert len(rec.of(rid, "finished")) == 1, rid
    for i, event in enumerate(rec.events):
        if event.type == "finished":
            assert i > 0 and rec.events[i - 1].type == "session_state", event.request_id
    # one per `finished`, plus one at the pause of "paused" (a paused result may have opened the transaction)
    assert len([e for e in rec.events if e.type == "session_state"]) == len(ids) + 1
    assert recorded_at_finish["ok"] == ["ok"] and recorded_at_finish["error"] == ["ok", "error"]


def test_a_cancel_during_the_policy_check_stops_the_statement_before_it_runs(make_worker):
    checking, release, records = threading.Event(), threading.Event(), []

    def slow_check(sql, confirmed):  # stands in for a slow parse of a long script
        checking.set()
        assert release.wait(5)
        return ddl_aware(sql, confirmed)

    worker, conn, rec = make_worker(check=slow_check, on_record=records.append)
    worker.submit(ExecuteCommand(request_id="a", sql="rows 1"))
    assert checking.wait(5)
    worker.cancel("a")  # the request is running, but no driver call is in flight yet
    release.set()
    done = rec.wait_for(lambda e: e.type == "finished" and e.request_id == "a")
    assert done.cancelled and conn.runs == [] and records == []
    assert [m.text for m in rec.of("a", "message")] == ["Query cancelled"]
    assert not run(worker, rec, "b", "rows 1").cancelled  # the cancel does not leak into the next request


def test_a_paused_result_that_opened_the_transaction_is_reported_and_can_go_idle(make_worker):
    worker, conn, rec = make_worker()
    command(worker, rec, SetAutocommitCommand(request_id="m", enabled=False))
    assert not last_state(rec).in_transaction
    worker.submit(ExecuteCommand(request_id="a", sql="rows 50", page_size=10))  # manual mode: opens a transaction
    pause = rec.wait_for(lambda e: e.type == "result_set_end" and e.has_more)
    before = rec.events[next(i for i, e in enumerate(rec.events) if e is pause) - 1]
    assert before.type == "session_state" and before.in_transaction and before.transaction_started_at is not None
    time.sleep(0.15)
    worker.check_idle_transaction(warn_after_s=0.1, rollback_after_s=0)
    assert [e.text[:16] for e in rec.events if e.type == "message" and e.request_id == ""] == ["Transaction idle"]
    late = Recorder()
    worker.set_emitter(late)
    worker.replay_state()
    assert late.wait_for(lambda e: e.type == "session_state").in_transaction


def test_a_slow_transaction_command_is_not_an_idle_transaction(make_worker):
    worker, conn, rec = make_worker()
    run(worker, rec, "a", "begin")
    time.sleep(0.15)
    committing, release = threading.Event(), threading.Event()
    real_commit = conn.commit

    def slow_commit():
        committing.set()
        assert release.wait(5)
        real_commit()

    conn.commit = slow_commit
    worker.submit(CommitCommand(request_id="c"))
    assert committing.wait(5)
    worker.check_idle_transaction(warn_after_s=0.1, rollback_after_s=0.1)  # COMMIT in progress: not idle
    release.set()
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "c")
    assert [e for e in rec.events if e.type == "message" and e.request_id == ""] == []
    assert conn.commits == 1 and conn.rollbacks == 0


def test_no_idle_rollback_when_a_statement_ran_between_the_check_and_the_rollback(make_worker):
    worker, conn, rec = make_worker()
    run(worker, rec, "a", "begin")
    time.sleep(0.15)
    parked, release = threading.Event(), threading.Event()
    caller, _ = run_in_thread(lambda: worker.call(lambda c: (parked.set(), release.wait(5))))
    assert parked.wait(5)  # the worker thread is busy; nothing below runs until it is released
    worker.submit(ExecuteCommand(request_id="b", sql="rows 1"))  # queued first: runs before the rollback
    worker.check_idle_transaction(warn_after_s=0, rollback_after_s=0.1)  # idle so far: queues a rollback
    release.set()
    caller.join(5)
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "b")
    worker.call(lambda c: None)  # queued behind the rollback: once it returns, the rollback task has run
    assert conn.rollbacks == 0 and conn.tx


def test_replay_state_sends_the_cached_state(make_worker):
    worker, _, _ = make_worker()
    late = Recorder()
    worker.set_emitter(late)
    worker.replay_state()
    assert late.wait_for(lambda e: e.type == "session_state").in_transaction is False


def test_idle_transaction_warning_then_rollback(make_worker):
    worker, conn, rec = make_worker()
    run(worker, rec, "a", "begin")
    time.sleep(0.15)
    worker.check_idle_transaction(warn_after_s=0.1, rollback_after_s=0)
    worker.check_idle_transaction(warn_after_s=0.1, rollback_after_s=0)
    warnings = [e for e in rec.events if e.type == "message" and e.request_id == ""]
    assert len(warnings) == 1 and "idle" in warnings[0].text
    worker.check_idle_transaction(warn_after_s=0.1, rollback_after_s=0.1)
    rec.wait_for(lambda e: e.type == "message" and e.text.startswith("Rolled back"))
    assert conn.rollbacks == 1 and not conn.tx
    assert [e for e in rec.events if e.type == "session_state"][-1].in_transaction is False


def test_no_idle_rollback_when_something_ran_in_between(make_worker):
    worker, conn, rec = make_worker()
    run(worker, rec, "a", "begin")
    time.sleep(0.15)
    conn.blocked.clear()
    worker.submit(ExecuteCommand(request_id="b", sql="block"))
    assert conn.blocked.wait(5)
    worker.check_idle_transaction(warn_after_s=0, rollback_after_s=0.1)  # running: not idle
    worker.cancel("b")
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "b")
    assert conn.rollbacks == 0


# ---- export: the same request lifecycle as an execute ----

def reads(sql, confirmed):
    return GuardDecision(action=GuardAction.RUN, classification=Classification(kind=StatementKind.READ))


def export(worker, recorder, rid, sql, path):
    worker.submit(ExportCommand(request_id=rid, sql=sql, format=FormatKind.CSV, path=str(path)))
    return recorder.wait_for(lambda e: e.type == "finished" and e.request_id == rid)


def test_export_runs_the_statement_once_and_is_not_recorded_in_history(make_worker, tmp_path):
    records = []
    worker, conn, rec = make_worker(check=reads, on_record=records.append)
    done = export(worker, rec, "x", "rows 3", tmp_path / "n.csv")
    assert not done.cancelled and (tmp_path / "n.csv").read_text(encoding="utf-8") == "n\n0\n1\n2\n"
    assert conn.runs == ["rows 3"] and records == []
    assert [m.text for m in rec.of("x", "message")] == [f"Exported 3 rows to {tmp_path / 'n.csv'}"]


def test_export_is_blocked_on_a_read_only_connection(make_worker, tmp_path):
    worker, conn, rec = make_worker(check=standard(read_only=True))
    export(worker, rec, "x", "DELETE FROM t", tmp_path / "n.csv")
    assert rec.of("x", "error")[0].code == "read_only" and conn.runs == [] and not (tmp_path / "n.csv").exists()


def test_a_statement_without_columns_cannot_be_exported_and_its_result_is_closed(make_worker, tmp_path):
    worker, conn, rec = make_worker(check=reads)
    result = FakeResult(0)
    result.columns = lambda: None
    conn.run = lambda sql: result
    export(worker, rec, "x", "anything", tmp_path / "n.csv")
    assert rec.of("x", "error")[0].code == "export_requires_select" and result.closed
    assert list(tmp_path.iterdir()) == []


def test_a_cancel_during_the_policy_check_stops_an_export_before_it_runs(make_worker, tmp_path):
    checking, release = threading.Event(), threading.Event()

    def slow_check(sql, confirmed):
        checking.set()
        assert release.wait(5)
        return reads(sql, confirmed)

    worker, conn, rec = make_worker(check=slow_check)
    worker.submit(ExportCommand(request_id="x", sql="rows 1", format=FormatKind.CSV, path=str(tmp_path / "n.csv")))
    assert checking.wait(5)
    worker.cancel("x")
    release.set()
    done = rec.wait_for(lambda e: e.type == "finished" and e.request_id == "x")
    assert done.cancelled and conn.runs == [] and list(tmp_path.iterdir()) == []
    assert not run(worker, rec, "y", "rows 1").cancelled  # the cancel does not leak into the next request


def test_a_queued_export_can_be_cancelled_before_it_starts(make_worker, tmp_path):
    worker, conn, rec = make_worker(check=reads)
    worker.submit(ExecuteCommand(request_id="slow", sql="block"))
    assert conn.blocked.wait(5)
    worker.submit(ExportCommand(request_id="x", sql="rows 1", format=FormatKind.CSV, path=str(tmp_path / "n.csv")))
    worker.cancel("x")
    worker.cancel("slow")
    done = rec.wait_for(lambda e: e.type == "finished" and e.request_id == "x")
    assert done.cancelled and conn.runs == ["block"] and list(tmp_path.iterdir()) == []
    assert [m.text for m in rec.of("x", "message")] == ["Query cancelled before it started"]


def test_a_running_export_is_not_an_idle_transaction(make_worker, tmp_path):
    worker, conn, rec = make_worker(check=reads)
    run(worker, rec, "a", "begin")
    conn.blocked.clear()
    worker.submit(ExportCommand(request_id="x", sql="block", format=FormatKind.CSV, path=str(tmp_path / "n.csv")))
    assert conn.blocked.wait(5)
    time.sleep(0.15)  # longer than the warning threshold, with the transaction open and the export still running
    worker.check_idle_transaction(warn_after_s=0.1, rollback_after_s=0)
    worker.cancel("x")
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "x")
    assert [e for e in rec.events if e.type == "message" and e.request_id == ""] == []


class EndlessResult(FakeResult):
    """Rows that never block: a driver cancel has nothing to interrupt, only the worker's own check can stop it.
    Bounded, so a worker that fails to stop ends the export instead of filling the disk."""

    def __init__(self, limit: int = 1_000_000) -> None:
        super().__init__(0)
        self._left = limit

    def fetch(self, n):
        taken, self._left = min(n, self._left), max(self._left - n, 0)
        return [[1]] * taken


def test_closing_the_session_stops_a_running_export(make_worker, tmp_path):
    worker, conn, rec = make_worker(check=reads)
    conn.run = lambda sql: EndlessResult()
    worker.submit(ExportCommand(request_id="x", sql="endless", format=FormatKind.CSV, path=str(tmp_path / "n.csv")))
    rec.wait_for(lambda e: e.type == "progress" and e.request_id == "x")
    started = time.monotonic()
    worker.close()
    assert time.monotonic() - started < 2
    assert [e.cancelled for e in rec.of("x", "finished")] == [True]
    assert list(tmp_path.iterdir()) == []  # neither the file nor its temporary file
