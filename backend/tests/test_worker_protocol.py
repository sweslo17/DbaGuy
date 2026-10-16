"""Execution protocol on top of the worker: policy, timeout, transaction commands, session state, history and schema
hooks. Driven by the fake connection so every ordering is checked deterministically."""

import time

import pytest

from dbaguy_backend.domain.execution import (
    CommitCommand,
    ExecuteCommand,
    FetchMoreCommand,
    RollbackCommand,
    SetAutocommitCommand,
)
from dbaguy_backend.services.safety import GuardAction, GuardDecision, SafetyPolicy
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from dbaguy_backend.sql.classifier import Classification, StatementKind
from dbaguy_backend.sql.dialects import Dialect
from tests.fake_db import FakeLease
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
    assert len([e for e in rec.events if e.type == "session_state"]) == len(ids)
    assert recorded_at_finish["ok"] == ["ok"] and recorded_at_finish["error"] == ["ok", "error"]


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
