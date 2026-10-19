"""The worker's apply (spec ⑤ §3.5) on a fake connection, so every race is forced: the event order, each outcome, what
is sent and never sent, what a Cancel reaches, History, the log, and the hooks' binding of plans to sessions. The live
engines follow in test_edit_flows.py."""

import threading
from uuid import uuid4

import pytest
from loguru import logger

from dbaguy_backend.adapters.base import StatementCancelled, StatementError
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.domain.classification import Classification, StatementKind
from dbaguy_backend.domain.dialect import Dialect
from dbaguy_backend.domain.edits import (
    PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, PLAN_MISMATCH, EditPlan, PlannedStatement, PlanUnavailable,
)
from dbaguy_backend.domain.execution import ApplyEditsCommand, ExecuteCommand, MessageSeverity, ServerMessage
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.services.edit_plans import EditPlanStore
from dbaguy_backend.services.history_service import HistoryService
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.services.safety import GuardAction, GuardDecision, SafetyPolicy
from dbaguy_backend.sessions.edit_apply import (
    COMMIT_LEFT_OPEN, ROLLBACK_FAILED, SQL_MODE_CHANGED, SQL_MODE_CHANGED_MESSAGE,
)
from dbaguy_backend.sessions.hooks import SessionHooksFactory
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.fake_db import FakeConnection, FakeLease, FakeResult
from tests.test_session_wiring import FakeCache
from tests.test_sessions import Recorder, paused, request_events

BLOCK = object()
PROFILE = uuid4()
LOST = StatementError("Connection to the server was lost", code="connection_lost")


class Counted(FakeResult):
    """One plan statement's answer: `rows` matched (None: no count), and the messages it brought."""

    def __init__(self, rows: int | None, messages=()) -> None:
        super().__init__(0)
        self._matched = rows
        self._messages = list(messages)

    def columns(self):
        return None

    def affected_rows(self):
        return self._matched

    def take_messages(self):
        taken, self._messages = self._messages, []
        return taken


class EditFake(FakeConnection):
    """Answers a plan's statements by their text from `answers`: an int or None (rows matched), a Counted, an exception
    (raised), BLOCK (waits for a cancel, then StatementCancelled) or a callable (called first; its result is the
    answer); any other text matches one row, and `rows N` gives N rows to page through. `BEGIN` opens the transaction;
    `COMMIT` ends it — after `commit_gate`, when set — or raises `commit_error` (any but connection_lost ends the
    transaction too, as PostgreSQL after a deferred-constraint failure)."""

    def __init__(self, answers=None, commit_error=None, commit_gate=None) -> None:
        super().__init__()
        self.answers = answers or {}
        self.commit_error = commit_error
        self.commit_gate = commit_gate
        self.committing = threading.Event()

    def statement(self, sql, options):
        self.runs.append(sql)
        if sql.startswith("rows "):
            return FakeResult(int(sql.split()[1]))
        if sql == "BEGIN":
            self.tx = True
            return Counted(None)
        if sql == "COMMIT":
            self.committing.set()
            if self.commit_gate is not None:
                assert self.commit_gate.wait(10)
            if self.commit_error is not None:
                if self.commit_error.code != "connection_lost":
                    self.tx = False
                raise self.commit_error
            self.tx = False
            self.commits += 1
            return Counted(None)
        answer = self.answers.get(sql, 1)
        if callable(answer):
            answer = answer()
        if answer is BLOCK:
            self.blocked.set()
            assert self._unblock.wait(10), "the blocked statement was never cancelled"
            raise StatementCancelled()
        if isinstance(answer, BaseException):
            raise answer
        return answer if isinstance(answer, Counted) else Counted(answer)


def make_plan(*statements: tuple[str, str], transactional: bool = True, plan_id: str = "p1",
              backslash_escapes: bool = True) -> EditPlan:
    planned = tuple(PlannedStatement(change_id=f"c{i}", kind=kind, sql=sql, row=f"id = {i}")
                    for i, (kind, sql) in enumerate(statements))
    script = "BEGIN;\n" + "".join(f"{s.sql};\n" for s in planned) + "COMMIT;"
    return EditPlan(plan_id=plan_id, profile_id=PROFILE, database=None, table="t", begin="BEGIN", statements=planned,
                    commit="COMMIT", transactional=transactional, script=script, backslash_escapes=backslash_escapes)


THREE = (("delete", "d0"), ("update", "u1"), ("insert", "i2"))


def writes(sql, confirmed):
    return GuardDecision(action=GuardAction.RUN, classification=Classification(kind=StatementKind.WRITE))


def read_only(sql, confirmed):
    return GuardDecision(action=GuardAction.BLOCK, classification=Classification(kind=StatementKind.WRITE))


class Env:
    def __init__(self, conn: EditFake, *plans: EditPlan, check=writes, dialect: Dialect = Dialect.SQLITE) -> None:
        self.conn = conn
        self.records = []
        self.plans = {plan.plan_id: plan for plan in plans}
        lease = FakeLease(conn)
        self.worker = SessionWorker("edits", lambda: lease)
        self.worker.start()
        self.worker.configure(WorkerHooks(dialect=dialect, check=check, on_record=self.records.append,
                                          take_edit_plan=self._take))
        self.recorder = Recorder()
        self.worker.set_emitter(self.recorder)

    def _take(self, plan_id: str) -> EditPlan:
        plan = self.plans.pop(plan_id, None)
        if plan is None:
            raise PlanUnavailable(PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE)
        return plan

    def submit(self, plan_id: str = "p1", rid: str = "a") -> None:
        self.worker.submit(ApplyEditsCommand(request_id=rid, plan_id=plan_id))

    def events(self, rid: str = "a") -> list:
        """The request's events, from its `started` to its `finished`, with the session_state among them."""
        return request_events(self.recorder, rid)

    def apply(self, plan_id: str = "p1", rid: str = "a") -> list:
        self.submit(plan_id, rid)
        return self.events(rid)


@pytest.fixture
def env_of():
    made: list[Env] = []

    def make(conn: EditFake, *plans: EditPlan, **fields) -> Env:
        env = Env(conn, *plans, **fields)
        made.append(env)
        return env

    yield make
    for env in made:
        env.worker.close()


def outcome(events):
    return next(e for e in events if e.type == "edit_outcome")


def error(events):
    return next((e for e in events if e.type == "error"), None)


def types(events) -> list[str]:
    return [e.type for e in events]


def test_a_commit_sends_the_plan_and_only_the_plan(env_of):  # R-05-7, spec §3.5 event order
    env = env_of(EditFake({"d0": 0}), make_plan(*THREE))
    events = env.apply()
    assert types(events) == ["started", "affected", "affected", "affected", "edit_outcome", "session_state", "finished"]
    assert [(e.index, e.rows) for e in events if e.type == "affected"] == [(0, 0), (1, 1), (2, 1)]
    done = outcome(events)
    assert (done.outcome, done.applied, done.already_gone, done.failed_change, done.statement) == (
        "committed", ["c0", "c1", "c2"], ["c0"], None, None)
    assert env.conn.runs == ["BEGIN", "d0", "u1", "i2", "COMMIT"] and env.conn.commits == 1 and not env.conn.tx
    [record] = env.records
    assert (record.sql, record.status, record.rows, record.error) == (make_plan(*THREE).script, "ok", 2, None)
    assert not events[-1].in_transaction and not events[-1].cancelled


@pytest.mark.parametrize(("answer", "code"), [
    (StatementError("duplicate key value", code="23505"), "23505"), (0, "edit_conflict"), (2, "edit_conflict"),
    (None, "row_count_unknown"), (LOST, "connection_lost"),
])
def test_a_statement_that_fails_or_matches_wrongly_rolls_everything_back(env_of, answer, code):  # R-05-5, R-05-6
    env = env_of(EditFake({"u1": answer}), make_plan(*THREE))
    events = env.apply()
    counted = ["affected", "affected"] if isinstance(answer, int) else ["affected"]
    assert types(events) == ["started", *counted, "error", "edit_outcome", "session_state", "finished"]
    assert error(events).code == code
    done = outcome(events)
    assert (done.outcome, done.applied, done.failed_change, done.statement) == ("rolled_back", [], "c1", 1)
    assert env.conn.runs == ["BEGIN", "d0", "u1"] and env.conn.commits == 0 and env.conn.rollbacks == 1
    assert env.records[0].status == "error"


def test_cancel_while_a_statement_waits_rolls_back(env_of):  # spec §6, R-05-6
    env = env_of(EditFake({"u1": BLOCK}), make_plan(*THREE))
    env.submit()
    assert env.conn.blocked.wait(5)
    env.worker.cancel("a")
    events = env.events()
    assert (error(events).code, error(events).message) == ("cancelled", "Cancelled: nothing was applied")
    assert (outcome(events).outcome, outcome(events).failed_change) == ("rolled_back", "c1")
    assert events[-1].cancelled and env.conn.runs == ["BEGIN", "d0", "u1"] and env.conn.rollbacks == 1
    assert env.records[0].status == "cancelled"


def test_a_cancel_that_lands_before_the_commit_rolls_back(env_of):  # R-05-6, ruling E12
    env = env_of(EditFake(), make_plan(("update", "u0")))
    env.conn.answers["u0"] = lambda: (env.worker.cancel("a"), 1)[1]  # nothing for the driver to stop by then
    events = env.apply()
    assert (outcome(events).outcome, error(events).code) == ("rolled_back", "cancelled")
    assert env.conn.runs == ["BEGIN", "u0"] and env.conn.commits == 0 and env.conn.rollbacks == 1


def test_a_cancel_after_the_commit_went_out_never_reaches_the_driver(env_of):  # R-05-6
    gate = threading.Event()
    env = env_of(EditFake(commit_gate=gate), make_plan(("update", "u0")))
    env.submit()
    assert env.conn.committing.wait(5)
    env.worker.cancel("a")
    gate.set()
    events = env.events()
    assert outcome(events).outcome == "committed" and env.conn.cancel_calls == 0 and not events[-1].cancelled


def test_a_commit_the_server_refuses_is_rolled_back(env_of):  # R-05-6 (a deferred constraint, A13)
    refusal = StatementError("insert or update violates foreign key constraint", code="23503")
    env = env_of(EditFake(commit_error=refusal), make_plan(("update", "u0")))
    events = env.apply()
    assert (outcome(events).outcome, outcome(events).statement, error(events).code) == ("rolled_back", None, "23503")
    assert error(events).message.endswith("\nThe transaction was rolled back: nothing was applied")
    assert env.conn.runs == ["BEGIN", "u0", "COMMIT"]


def test_a_commit_whose_answer_is_lost_is_unknown_and_never_sent_again(env_of):  # R-05-6
    env = env_of(EditFake(commit_error=LOST), make_plan(("update", "u0")))
    events = env.apply()
    assert (outcome(events).outcome, outcome(events).applied, error(events).code) == ("unknown", [], "connection_lost")
    assert env.conn.runs.count("COMMIT") == 1 and env.conn.rollbacks == 0
    assert env.records[0].error == "Outcome unknown: Connection to the server was lost"


def test_a_table_that_cannot_roll_back_says_what_took_effect(env_of):  # D-05-6, ruling E11
    env = env_of(EditFake({"u1": 0}), make_plan(*THREE, transactional=False),
                 make_plan(*THREE, transactional=False, plan_id="p2"))
    done = outcome(env.apply())
    assert (done.outcome, done.applied, done.already_gone, done.failed_change) == ("partial", ["c0"], [], "c1")
    env.conn.answers["u1"] = LOST
    lost = outcome(env.apply("p2", rid="b"))
    assert (lost.outcome, lost.applied) == ("unknown", ["c0"])


def test_a_worker_bug_after_the_begin_is_an_unknown_outcome(env_of):  # R-05-6
    env = env_of(EditFake({"u1": RuntimeError("bug")}), make_plan(*THREE))
    events = env.apply()
    assert (outcome(events).outcome, error(events).message) == ("unknown", "Internal error: bug")
    assert env.conn.rollbacks == 1  # what the transaction held is released


def test_a_worker_bug_on_a_table_that_cannot_roll_back_still_ends_its_transaction(env_of):  # ruling E12
    env = env_of(EditFake({"u1": RuntimeError("bug")}), make_plan(*THREE, transactional=False))
    done = outcome(env.apply())
    assert (done.outcome, done.applied, env.conn.rollbacks, env.conn.tx) == ("unknown", ["c0"], 1, False)


def test_before_the_commit_a_failed_transaction_rolls_back_and_a_vanished_one_is_unknown(env_of):  # ruling E12
    tainted = env_of(EditFake(), make_plan(("update", "u0")))
    tainted.conn.answers["u0"] = lambda: (setattr(tainted.conn, "failed", True), 1)[1]
    events = tainted.apply()
    assert (outcome(events).outcome, error(events).code, tainted.conn.runs) == (
        "rolled_back", "transaction_failed", ["BEGIN", "u0"])
    vanished = env_of(EditFake(), make_plan(("update", "u0")))
    vanished.conn.answers["u0"] = lambda: (setattr(vanished.conn, "tx", False), 1)[1]
    events = vanished.apply()
    assert (outcome(events).outcome, error(events).code, vanished.conn.runs) == (
        "unknown", "outcome_unknown", ["BEGIN", "u0"])


def test_an_expired_plan_sends_nothing(env_of):  # Review Focus 4, R-05-6
    env = env_of(EditFake(), make_plan(("update", "u0")))
    first, second = env.apply(), env.apply(rid="b")
    assert outcome(first).outcome == "committed"
    assert types(second) == ["started", "error", "edit_outcome", "session_state", "finished"]
    assert (error(second).code, error(second).message) == ("plan_expired", PLAN_EXPIRED_MESSAGE)
    assert outcome(second).outcome == "rolled_back" and env.conn.runs == ["BEGIN", "u0", "COMMIT"]
    assert len(env.records) == 1  # nothing was sent: no History entry


def test_a_read_only_profile_and_an_open_transaction_send_nothing(env_of):  # R-05-7, R-05-4
    blocked = env_of(EditFake(), make_plan(("update", "u0")), check=read_only)
    events = blocked.apply()
    assert (error(events).code, outcome(events).outcome, blocked.conn.runs) == ("read_only", "rolled_back", [])
    busy = env_of(EditFake(), make_plan(("update", "u0")))
    busy.conn.tx = True
    events = busy.apply()
    assert (error(events).code, outcome(events).outcome, busy.conn.runs) == ("transaction_open", "rolled_back", [])


class ReadsBackslashes(EditFake):
    """A session whose last answer says how it reads a backslash (MySQL's OK packet; None: the engine does not say)."""

    def __init__(self, escapes: bool | None) -> None:
        super().__init__()
        self.escapes = escapes

    def backslash_escapes(self) -> bool | None:
        return self.escapes


@pytest.mark.parametrize(("session", "planned", "expected"), [
    (False, True, "rolled_back"), (True, False, "rolled_back"), (True, True, "committed"), (False, False, "committed"),
    (None, False, "committed"),
])
def test_a_session_that_reads_backslashes_unlike_the_plan_sends_no_dml(env_of, session, planned, expected):  # M2
    env = env_of(ReadsBackslashes(session), make_plan(("update", "u0"), backslash_escapes=planned))
    events = env.apply()
    done = outcome(events)
    assert done.outcome == expected
    if expected == "rolled_back":
        assert types(events) == ["started", "error", "edit_outcome", "session_state", "finished"]
        assert (error(events).code, error(events).message) == (SQL_MODE_CHANGED, SQL_MODE_CHANGED_MESSAGE)
        assert (done.applied, done.failed_change, done.statement) == ([], None, None)
        assert (env.conn.runs, env.conn.rollbacks, env.conn.tx) == (["BEGIN"], 1, False)  # no DML went out
        assert [record.status for record in env.records] == ["error"]


def test_an_apply_cancelled_before_it_started_still_says_nothing_was_applied(env_of):  # ruling E1
    env = env_of(EditFake({"hold": BLOCK}), make_plan(("update", "u0")))
    env.worker.submit(ExecuteCommand(request_id="q", sql="hold"))
    assert env.conn.blocked.wait(5)
    env.submit()
    env.worker.cancel("a")  # still queued behind q
    env.worker.cancel("q")
    events = env.events()
    assert types(events) == ["started", "message", "edit_outcome", "session_state", "finished"]
    assert outcome(events).outcome == "rolled_back" and events[-1].cancelled and "u0" not in env.conn.runs


def test_a_paused_result_is_ended_before_the_apply_starts(env_of):  # R-05-4
    env = env_of(EditFake(), make_plan(("update", "u0")))
    env.worker.submit(ExecuteCommand(request_id="browse", sql="rows 1000", page_size=500))
    env.recorder.wait_for(paused("browse", 500))
    events = env.apply()
    order = [(e.type, e.request_id) for e in env.recorder.events if e.type in ("started", "finished")]
    assert order.index(("finished", "browse")) < order.index(("started", "a"))
    assert outcome(events).outcome == "committed"


def test_a_statements_messages_reach_the_apply(env_of):  # a trigger's NOTICE or PRINT
    notice = ServerMessage(severity=MessageSeverity.INFO, text="trigger says hi")
    env = env_of(EditFake({"u0": Counted(1, [notice])}), make_plan(("update", "u0")))
    assert [e.text for e in env.apply() if e.type == "message"] == ["trigger says hi"]


def test_keys_and_values_never_reach_the_info_log(env_of):  # spec §5
    statement = PlannedStatement(change_id="c0", kind="update", sql="UPDATE t SET v = 'zz-secret' WHERE id = 7",
                                 row="id = 'zz-secret-key'")
    plan = make_plan(("update", "x")).model_copy(update={"statements": (statement,)})
    env = env_of(EditFake({statement.sql: 0}), plan)
    records = []
    sink = logger.add(lambda message: records.append((message.record["level"].no, message.record["message"])),
                      level=0)
    try:
        events = env.apply()
    finally:
        logger.remove(sink)
    assert "zz-secret-key" in error(events).message
    assert not [text for level, text in records if level >= 20 and "zz-secret" in text]


def test_the_hooks_hand_a_session_only_plans_of_its_profile_and_database(tmp_path):  # CR-2, ruling E3
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    body = {"profile": {"engine": "sqlite", "name": "s", "path": str(tmp_path / "x.db")}}
    pid = profiles.create(ProfileUpsertRequest.model_validate(body)).profile.id
    history = HistoryService(tmp_path / "history.db")
    store = EditPlanStore()
    registry = AdapterRegistry([SqliteAdapter()])
    try:
        hooks = SessionHooksFactory(registry, profiles, SafetyPolicy(), history, FakeCache(), store).build(pid, "main")
        base = make_plan(("update", "u0"))
        store.put(base.model_copy(update={"plan_id": "theirs"}))
        store.put(base.model_copy(update={"plan_id": "other_db", "profile_id": pid, "database": "elsewhere"}))
        store.put(base.model_copy(update={"plan_id": "mine", "profile_id": pid}))
        for plan_id in ("theirs", "other_db"):
            with pytest.raises(PlanUnavailable) as raised:
                hooks.take_edit_plan(plan_id)
            assert raised.value.code == PLAN_MISMATCH
        assert hooks.take_edit_plan("mine").plan_id == "mine"
        planless = SessionHooksFactory(registry, profiles, SafetyPolicy(), history, FakeCache()).build(pid, "main")
        with pytest.raises(PlanUnavailable) as raised:
            planless.take_edit_plan("mine")
        assert raised.value.code == PLAN_EXPIRED
    finally:
        history.close()


# ---- pre-flight items and the paths the brief's tests do not force ----


class NestedCommit(EditFake):
    """COMMIT TRANSACTION under a trigger's BEGIN TRAN: the count only goes down and a transaction stays open (as does
    MySQL's COMMIT under completion_type=CHAIN, which did commit)."""

    def statement(self, sql, options):
        if sql != "COMMIT":
            return super().statement(sql, options)
        self.runs.append(sql)
        return Counted(None)


@pytest.mark.parametrize(("transactional", "expected"), [(True, "unknown"), (False, "committed")])
def test_a_commit_that_leaves_a_transaction_open_is_rolled_back_and_never_reads_committed(
        env_of, transactional, expected):  # pre-flight L7, ruling E11
    env = env_of(NestedCommit(), make_plan(("update", "u0"), transactional=transactional))
    events = env.apply()
    assert outcome(events).outcome == expected
    assert (env.conn.runs.count("COMMIT"), env.conn.rollbacks, env.conn.tx, events[-1].in_transaction) == (
        1, 1, False, False)
    if transactional:
        assert (error(events).code, error(events).message) == ("outcome_unknown", COMMIT_LEFT_OPEN)
    else:
        assert error(events) is None


class StuckRollback(EditFake):
    """A ROLLBACK the server refuses, the connection still there: the transaction stays open."""

    def rollback(self):
        raise StatementError("cannot roll back 'zz-secret'", code="XX000")


def test_a_rollback_that_fails_on_a_live_connection_says_the_transaction_is_still_open(env_of):  # pre-flight L8
    env = env_of(StuckRollback({"u1": 0}), make_plan(*THREE))
    events = env.apply()
    warnings = [e.text for e in events if e.type == "message" and e.severity == MessageSeverity.WARNING]
    assert warnings == [ROLLBACK_FAILED]
    assert (outcome(events).outcome, error(events).code, events[-1].in_transaction) == (
        "rolled_back", "edit_conflict", True)


class StateFails(EditFake):
    """The transaction state cannot be read, once: before COMMIT (transaction_failed) or after it (in_transaction)."""

    def __init__(self, when: str, **fields) -> None:
        super().__init__(**fields)
        self.when = when

    def transaction_failed(self):
        if self.when == "before" and self.tx and self.runs[-1] != "COMMIT":
            self.when = ""
            raise StatementError("Connection to the server was lost", code="connection_lost")
        return super().transaction_failed()

    def in_transaction(self):
        if self.when == "after" and self.runs and self.runs[-1] == "COMMIT":
            self.when = ""
            raise StatementError("Connection to the server was lost", code="connection_lost")
        return super().in_transaction()


def test_a_state_that_cannot_be_read_before_the_commit_rolls_back_and_after_it_is_unknown(env_of):  # L7, L8
    before = env_of(StateFails("before"), make_plan(("update", "u0")))
    events = before.apply()
    assert (outcome(events).outcome, error(events).code, before.conn.runs, before.conn.rollbacks) == (
        "rolled_back", "connection_lost", ["BEGIN", "u0"], 1)
    after = env_of(StateFails("after"), make_plan(("update", "u0")))
    events = after.apply()
    assert (outcome(events).outcome, error(events).code, after.conn.commits, after.conn.rollbacks) == (
        "unknown", "connection_lost", 1, 0)


class TriggerRows(Counted):
    """A SQL Server trigger's SELECT answers first (no count), then the statement's own count follows with a PRINT."""

    def __init__(self) -> None:
        super().__init__(None)
        self.sets = 2
        self.later = [ServerMessage(severity=MessageSeverity.INFO, text="printed after the rows")]

    def next_result(self):
        self.sets -= 1
        if self.sets <= 0:
            return False
        self._messages = self.later
        return True


def test_an_answer_with_a_result_set_first_is_read_to_its_end_and_fails_closed(env_of):  # ruling E9, pre-flight M6
    answer = TriggerRows()
    env = env_of(EditFake({"u0": answer}), make_plan(("update", "u0")))
    events = env.apply()
    assert (outcome(events).outcome, error(events).code) == ("rolled_back", "row_count_unknown")
    assert [e.text for e in events if e.type == "message"] == ["printed after the rows"]
    assert answer.sets <= 0 and answer.closed and env.conn.rollbacks == 1


def test_an_unreadable_count_on_a_table_that_cannot_roll_back_is_unknown_not_rolled_back(env_of):  # pre-flight L3
    env = env_of(EditFake({"u0": None}), make_plan(("update", "u0"), ("update", "u1"), transactional=False))
    events = env.apply()
    done = outcome(events)
    assert (done.outcome, done.applied, done.failed_change, error(events).code) == (
        "unknown", [], "c0", "row_count_unknown")
    assert "this change may have taken effect" in error(events).message and env.conn.runs == ["BEGIN", "u0"]


def test_a_cancel_during_the_checks_stops_the_begin_before_it_is_sent(env_of):  # ruling E13, the cancel window
    holder = {}

    def cancels(sql, confirmed):
        holder["env"].worker.cancel("a")  # the request is running: the worker keeps it for its next statement
        return writes(sql, confirmed)

    env = holder["env"] = env_of(EditFake(), make_plan(("update", "u0")), check=cancels)
    events = env.apply()
    assert (error(events).code, error(events).message) == ("cancelled", "Cancelled: nothing was applied")
    assert (outcome(events).outcome, events[-1].cancelled, env.conn.runs, env.records) == ("rolled_back", True, [], [])


def test_the_info_line_counts_each_kind_and_a_close_error_keeps_its_text_at_debug(env_of):  # pre-flight L5, L9
    class Unclosable(Counted):
        def close(self):
            raise StatementError("closing echoed 'zz-secret'", code="HY000")

    env = env_of(EditFake({"d0": Unclosable(1)}), make_plan(*THREE))
    records = []
    sink = logger.add(lambda message: records.append((message.record["level"].no, message.record["message"])),
                      level=0)
    try:
        assert outcome(env.apply()).outcome == "committed"
    finally:
        logger.remove(sink)
    assert any("1 delete(s), 1 update(s), 1 insert(s) on t → committed" in text for level, text in records
               if level == 20)
    assert any("Error closing result on edits (HY000)" in text for level, text in records if level == 30)
    assert not [text for level, text in records if level >= 20 and "zz-secret" in text]
    assert [text for level, text in records if level == 10 and "zz-secret" in text]


# ---- fix round 1: ruling G13, SQL Server 3609, a cancel between statements ----


@pytest.mark.parametrize("transactional", [False, True])
def test_a_cancel_caught_between_statements_sends_nothing_more_and_names_no_change(env_of, transactional):  # item 4
    env = env_of(EditFake(), make_plan(*THREE, transactional=transactional))
    env.conn.answers["d0"] = lambda: (env.worker.cancel("a"), 1)[1]  # d0 is done: the flag stops u1 before it is sent
    events = env.apply()
    done = outcome(events)
    assert (done.failed_change, done.statement, env.conn.runs, env.conn.rollbacks) == (None, None, ["BEGIN", "d0"], 1)
    if transactional:
        assert (done.outcome, error(events).message, events[-1].cancelled) == (
            "rolled_back", "Cancelled: nothing was applied", True)
        assert env.records[0].status == "cancelled"
    else:  # G13's exception: u1 never went out, so only d0 took effect
        assert (done.outcome, done.applied, error(events).message) == (
            "partial", ["c0"], "Cancelled: the statements before it stay applied (this table cannot roll back)")
        assert env.records[0].error.startswith("Partly applied (1 of 3): Cancelled")


def test_on_a_table_that_cannot_roll_back_a_failure_after_sending_is_unknown(env_of):  # ruling G13
    trigger = StatementError("Duplicate entry '1' for key 'PRIMARY'", code="1062")  # an AFTER trigger's, say
    plans = [make_plan(*THREE, transactional=False, plan_id=plan_id) for plan_id in ("p1", "p2", "p3")]
    env = env_of(EditFake({"u1": trigger}), *plans)
    failed = env.apply()
    assert (outcome(failed).outcome, outcome(failed).applied, outcome(failed).failed_change, error(failed).code) == (
        "unknown", ["c0"], "c1", "1062")
    env.conn.answers["u1"] = BLOCK  # a cancel that reaches the statement once it was sent
    env.submit("p2", rid="b")
    assert env.conn.blocked.wait(5)
    env.worker.cancel("b")
    cancelled = env.events("b")
    assert (outcome(cancelled).outcome, outcome(cancelled).applied, outcome(cancelled).failed_change) == (
        "unknown", ["c0"], "c1")
    assert error(cancelled).message.startswith("Cancelled: this change may have taken effect")
    env.conn.answers["u1"] = 0  # the exception: a conflict that matched no row changed nothing
    conflict = outcome(env.apply("p3", rid="c"))
    assert (conflict.outcome, conflict.applied, conflict.failed_change) == ("partial", ["c0"], "c1")


@pytest.mark.parametrize(("dialect", "expected"), [(Dialect.TSQL, "unknown"), (Dialect.SQLITE, "rolled_back")])
def test_a_trigger_that_ended_the_transaction_makes_the_outcome_unknown(env_of, dialect, expected):  # item 2: 3609
    ended = StatementError("The transaction ended in the trigger. The batch has been aborted.", code="3609")
    env = env_of(EditFake({"u1": ended}), make_plan(*THREE), dialect=dialect)
    events = env.apply()
    assert (outcome(events).outcome, outcome(events).failed_change, outcome(events).statement) == (expected, "c1", 1)
    assert env.conn.runs == ["BEGIN", "d0", "u1"] and env.conn.commits == 0
