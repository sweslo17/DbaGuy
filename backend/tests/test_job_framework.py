"""The job framework (spec ⑥ D-06-2, D-06-10, D-06-11) on a fake connection: the event order, exactly one
job_outcome however a job ends, cancels, progress and message limits, History, the replay on attach, the job list and
a busy session that is never reaped. The jobs here are scripted; Run SQL file and dump follow in their own tests."""

import sqlite3
import threading
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from dbaguy_backend.adapters.base import StatementCancelled, StatementError
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.api import sessions as sessions_api
from dbaguy_backend.app import create_app
from dbaguy_backend.config import Settings
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.data_jobs import JobCommand, JobError, JobOutcomeEvent, JobProgressEvent, StoppedAt
from dbaguy_backend.domain.execution import ExecuteCommand, MessageSeverity
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.sessions.hooks import _Recent
from dbaguy_backend.sessions.jobs.context import (
    JOB_ROLLBACK_FAILED, Job, JobContext, JobHooks, JobReport, history_status,
)
from dbaguy_backend.sessions.jobs.registry import SESSION_CLOSED_FIRST, JobRegistry
from dbaguy_backend.sessions.manager import SessionCreateRequest, SessionManager
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.fake_db import FakeConnection, FakeLease
from tests.test_sessions import Recorder, request_events


# `hold <name>` and `wait <name>`: (reached, go) — the job sets `reached` and waits for `go`
HOLDS: dict[str, tuple[threading.Event, threading.Event]] = {}


class Scripted(JobCommand):
    """A job command of the tests: the job does what `steps` says (see ScriptedJob)."""

    job_kind = "run_file"
    type: str = "scripted"
    steps: list[str]
    broke: Literal["default", "fails", "plain"] = "default"  # how ScriptedJob.broke answers


class ScriptedJob(Job):
    """Steps: `sql <text>` runs a statement and drains it (StatementCancelled → stopped, cancelled), `tick` and
    `tick!` progress, `msg <text>`, `msgs <n>`, `crash`, `fail <code>` (an error report), `record`, `hold <name>`
    (stop_cancels(), then wait as a COMMIT would: HOLDS), `wait <name>` (wait, a Cancel still lands), `check`
    (raise_if_cancelled(), let through), `resume` (resume_cancels(): stopped, cancelled, when a Cancel was held). Its
    broke() is the framework's, fails too, or a plain unknown."""

    kind = "run_file"

    def __init__(self, command: Scripted) -> None:
        self._steps = command.steps
        self._broke = command.broke

    @property
    def title(self) -> str:
        return "Scripted job"

    def run(self, ctx: JobContext) -> JobReport:
        done = 0
        for step in self._steps:
            word, _, arg = step.partition(" ")
            if word == "sql":
                try:
                    ctx.drain(ctx.run(arg))
                except StatementCancelled as exc:
                    return JobReport(ctx.outcome("stopped", cancelled=True, statements=done,
                                                 stopped_at=StoppedAt(line=1, statement=done + 1)), exc)
                done += 1
                ctx.progress.statements = done
            elif word in ("tick", "tick!"):
                ctx.tick(force=word == "tick!")
            elif word == "msg":
                ctx.message(MessageSeverity.INFO, arg)
            elif word == "msgs":
                for i in range(int(arg)):
                    ctx.message(MessageSeverity.INFO, f"m{i}")
            elif word == "crash":
                raise RuntimeError("bug")
            elif word == "fail":
                return JobReport(ctx.outcome("stopped", statements=done), StatementError("it failed", code=arg))
            elif word == "record":
                ctx.begin_record("-- scripted")
            elif word in ("hold", "wait"):
                if word == "hold":
                    ctx.stop_cancels()
                reached, go = HOLDS[arg]
                reached.set()
                assert go.wait(5)
            elif word == "check":
                ctx.raise_if_cancelled()
            elif word == "resume":  # a copy's next table (06c): a Cancel held since `hold` stops it here
                if ctx.resume_cancels():
                    return JobReport(ctx.outcome("stopped", cancelled=True, statements=done), StatementCancelled())
        return JobReport(ctx.outcome("completed", statements=done), schema_changed=done > 0)

    def broke(self, ctx: JobContext, exc: Exception) -> JobReport:
        if self._broke == "fails":
            raise RuntimeError("broke() broke too")
        if self._broke == "plain":
            return JobReport(ctx.outcome("unknown"), StatementError("plain"))
        return super().broke(ctx, exc)


class Env:
    def __init__(self, conn: FakeConnection | None = None, clock=None) -> None:
        self.conn = conn or FakeConnection()
        self.records: list[tuple] = []
        self.listed: list = []
        self.schema_changes = 0
        lease = FakeLease(self.conn)
        self.worker = SessionWorker("jobs", lambda: lease)
        self.worker.start()
        jobs = JobHooks(
            build=self._build, begin_record=lambda sql, at: self.records.append(("begin", sql)) or 7,
            finish_record=lambda *args: self.records.append(("finish", *args)),
            announce=lambda *args: self.listed.append(("announce", *args)),
            report=lambda event: self.listed.append(("report", event.type)),
        )
        self.worker.configure(WorkerHooks(jobs=jobs, on_schema_change=self._changed))
        self.recorder = Recorder()
        self.worker.set_emitter(self.recorder)

    def _changed(self) -> None:
        self.schema_changes += 1

    @staticmethod
    def _build(command: JobCommand) -> Job:
        if not isinstance(command, Scripted):
            raise StatementError("not a test job", code="not_supported")
        return ScriptedJob(command)

    def submit(self, *steps: str, rid: str = "j", **fields) -> None:
        self.worker.submit(Scripted(request_id=rid, steps=list(steps), **fields))

    def run(self, *steps: str, rid: str = "j", **fields) -> list:
        self.submit(*steps, rid=rid, **fields)
        return request_events(self.recorder, rid)


@pytest.fixture
def env():
    made: list[Env] = []

    def make(**fields) -> Env:
        made.append(Env(**fields))
        return made[-1]

    yield make
    for e in made:
        e.worker.close()


def types(events) -> list[str]:
    return [e.type for e in events]


def outcome(events) -> JobOutcomeEvent:
    [found] = [e for e in events if e.type == "job_outcome"]  # exactly one per job (⑤ E1)
    return found


def test_a_job_sends_its_events_in_order_and_one_outcome(env):  # D-06-2
    e = env()
    events = e.run("tick!", "sql rows 3", "msg hello", "tick!", "record")
    assert types(events) == ["started", "job_progress", "message", "job_progress", "job_outcome", "session_state",
                             "finished"]
    done = outcome(events)
    assert (done.job, done.outcome, done.statements, done.cancelled) == ("run_file", "completed", 1, False)
    assert events[3].statements == 1 and not events[-1].cancelled
    assert e.conn.runs == ["rows 3"]
    assert e.records == [("begin", "-- scripted"), ("finish", 7, "ok", e.records[1][3], 0, None)]
    assert e.schema_changes == 1
    assert e.listed[0] == ("announce", "j", "run_file", "Scripted job", None)  # no source profile: not a copy
    assert [item for item in e.listed[1:]] == [("report", "job_progress"), ("report", "job_progress"),
                                               ("report", "job_outcome")]


def test_an_error_goes_out_right_before_the_outcome(env):
    events = env().run("sql x", "fail 42P01")
    assert types(events)[-4:] == ["error", "job_outcome", "session_state", "finished"]
    error = next(ev for ev in events if ev.type == "error")
    assert (error.code, error.message, outcome(events).outcome) == ("42P01", "it failed", "stopped")
    assert outcome(events).error == JobError(code="42P01", message="it failed")  # fix wave F3: kept for a replay
    assert outcome(env().run("sql x")).error is None


def test_a_job_cancelled_while_queued_or_closed_before_it_started_says_not_started(env):
    e = env()
    e.worker.submit(ExecuteCommand(request_id="busy", sql="block"))
    assert e.conn.blocked.wait(5)
    e.submit("sql x", rid="queued")
    e.worker.cancel("queued")
    e.worker.cancel("busy")
    queued = request_events(e.recorder, "queued")
    assert types(queued) == ["started", "message", "job_outcome", "session_state", "finished"]
    assert (outcome(queued).outcome, outcome(queued).cancelled, queued[-1].cancelled) == ("not_started", True, True)
    assert outcome(queued).warnings == [queued[1].text]  # the warning sent live, kept for a later window (F3)
    assert "x" not in e.conn.runs

    closing = env()
    closing.worker.submit(ExecuteCommand(request_id="busy", sql="block"))
    assert closing.conn.blocked.wait(5)
    closing.submit("sql x", rid="late")
    closing.worker.close()
    late = request_events(closing.recorder, "late")
    assert (outcome(late).outcome, outcome(late).cancelled) == ("not_started", False)
    assert outcome(late).warnings == [next(ev for ev in late if ev.type == "message").text]


def test_a_cancel_reaches_the_running_statement(env):
    e = env()
    e.submit("sql block", "sql after")
    assert e.conn.blocked.wait(5)
    e.worker.cancel("j")
    events = request_events(e.recorder, "j")
    done = outcome(events)
    assert (done.outcome, done.cancelled, done.stopped_at.statement) == ("stopped", True, 1)
    assert events[-1].cancelled and e.conn.runs == ["block"]


def test_an_unclassified_failure_is_unknown_once_something_was_sent(env):
    events = env().run("sql x", "crash")
    assert (outcome(events).outcome, next(ev for ev in events if ev.type == "error").message) == (
        "unknown", "Internal error: bug")
    events = env().run("crash")
    assert outcome(events).outcome == "not_started"


def test_a_job_never_runs_inside_an_open_transaction(env):
    e = env()
    e.conn.tx = True
    events = e.run("sql x")
    error = next(ev for ev in events if ev.type == "error")
    assert (error.code, outcome(events).outcome, e.conn.runs) == ("transaction_open", "not_started", [])


def test_a_command_the_session_cannot_run_is_refused():
    worker = SessionWorker("jobs", lambda: FakeLease())
    worker.start()
    try:
        recorder = Recorder()
        worker.set_emitter(recorder)
        worker.submit(Scripted(request_id="j", steps=["sql x"]))  # the default hooks build no job
        events = request_events(recorder, "j")
        error = next(ev for ev in events if ev.type == "error")
        assert (error.code, outcome(events).outcome) == ("not_supported", "not_started")
    finally:
        worker.close()


def test_progress_is_sent_at_most_every_250_ms_and_always_when_forced():
    now = [0.0]
    sent: list = []
    ctx = JobContext(
        request_id="j", kind="dump", connection=FakeConnection(), dialect=None, capabilities=None, hooks=JobHooks(),
        _run=None, _close=None, _raise_if_cancelled=None, _send=None, _publish=sent.append, _stop_cancels=None,
        _sent=lambda: False,
        _clock=lambda: now[0],
    )
    for step in range(10):  # 10 ticks over 0.45 s
        ctx.progress.rows = step
        ctx.tick()
        now[0] += 0.05
    assert [event.rows for event in sent] == [0, 5]
    ctx.tick(force=True)
    assert sent[-1].rows == 9 and isinstance(sent[-1], JobProgressEvent)


def test_server_messages_past_the_limit_are_counted_not_sent(env):  # D-06-11
    events = env().run("msgs 250")
    assert sum(ev.type == "message" for ev in events) == 200
    assert outcome(events).warnings == ["50 more server messages were not shown"]


def test_a_client_that_attaches_later_gets_the_last_progress_and_the_outcome(env):  # D-06-10
    e = env()
    e.run("tick!", "sql x", "tick!")
    later = Recorder()
    e.worker.attach_emitter(later)
    assert types(later.events) == ["job_progress", "job_outcome"]
    assert later.events[0].statements == 1


def test_a_client_that_attaches_after_a_job_stopped_learns_why(env):  # fix wave F3
    e = env()
    e.submit("sql x", "tick!", "sql block", "sql after")
    assert e.conn.blocked.wait(5)
    e.worker.cancel("j")
    request_events(e.recorder, "j")
    later = Recorder()
    e.worker.attach_emitter(later)
    assert types(later.events) == ["job_progress", "job_outcome"]  # never the error event itself
    assert later.events[1].error == JobError(line=1, statement=2, code="cancelled", message="Query cancelled")


@pytest.mark.parametrize(("event", "error", "expected"), [
    (dict(outcome="completed"), None, ("ok", None)),
    (dict(outcome="completed", error_count=3), None, ("ok", "3 error(s) skipped")),
    (dict(outcome="rolled_back", cancelled=True), StatementCancelled(), ("cancelled", "Query cancelled")),
    (dict(outcome="rolled_back"), StatementError("boom"), ("error", "Rolled back: boom")),
    (dict(outcome="stopped", stopped_at=StoppedAt(line=12, statement=3)), StatementError("boom"),
     ("error", "Stopped at line 12: boom")),
    (dict(outcome="partial", stopped_at=StoppedAt(line=1_200, statement=40)), None, ("error", "Partly applied at line 1,200")),
    (dict(outcome="unknown"), StatementError("lost"), ("error", "Outcome unknown: lost")),
    # A Cancel that left something applied, or not known, says that first (06c Task 4 review M-1)
    (dict(outcome="partial", cancelled=True), StatementCancelled(),
     ("error", "Partly applied (cancelled): Query cancelled")),
    (dict(outcome="unknown", cancelled=True, stopped_at=StoppedAt(line=9, statement=2)), StatementCancelled(),
     ("error", "Outcome unknown at line 9 (cancelled): Query cancelled")),
])
def test_history_status_of_each_outcome(event, error, expected):  # D-06-9
    assert history_status(JobOutcomeEvent(request_id="j", job="run_file", **event), error) == expected


def test_the_read_only_flag_is_read_again_at_most_once_a_second():
    now, reads = [0.0], []
    flag = _Recent(lambda: reads.append(now[0]) or len(reads) > 1, clock=lambda: now[0])
    assert flag() is False and flag() is False
    now[0] = 0.9
    assert flag() is False
    now[0] = 1.0
    assert flag() is True and reads == [0.0, 1.0]


def test_the_job_list_keeps_running_jobs_and_forgets_finished_ones():
    registry = JobRegistry(limit=2, clock=lambda: datetime(2026, 10, 20, tzinfo=UTC))
    profile = uuid4()
    for rid in ("a", "b", "c"):
        registry.begin("s", profile, rid, "dump", f"Dump {rid}")
    assert [info.request_id for info in registry.list()] == ["c", "b", "a"]  # running jobs are never trimmed
    registry.report("s", JobProgressEvent(request_id="b", rows=5))
    registry.report("s", JobOutcomeEvent(request_id="a", job="dump", outcome="completed"))
    registry.report("other", JobOutcomeEvent(request_id="c", job="dump", outcome="completed"))  # another session's
    assert registry.get("s", "b").progress.rows == 5 and registry.get("s", "c").outcome is None
    registry.begin("s", profile, "d", "run_file", "Run d")
    assert [info.request_id for info in registry.list()] == ["d", "c", "b"]  # the oldest finished one went
    registry.report("s", JobOutcomeEvent(request_id="b", job="dump", outcome="failed"))
    assert registry.clear_finished() == 1 and [info.request_id for info in registry.list()] == ["d", "c"]


def sqlite_sessions(tmp_path, **fields) -> tuple[SessionManager, object]:
    """A SessionManager (no hooks) over a SQLite file, and its profile's id."""
    db = tmp_path / "s.db"
    sqlite3.connect(db).close()
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    entry = profiles.create(ProfileUpsertRequest.model_validate({"profile": {"engine": "sqlite", "name": "s",
                                                                             "path": str(db)}}))
    connections = ConnectionService(AdapterRegistry([SqliteAdapter()]), profiles,
                                    SshTunnelManager(write_askpass_script(tmp_path)))
    return SessionManager(connections, profiles, **fields), entry.profile.id


@pytest.fixture
def manager(tmp_path):
    made, profile_id = sqlite_sessions(tmp_path, idle_timeout=0)
    yield made, profile_id
    made.close_all()


def test_a_session_running_a_request_is_never_reaped(manager):  # D-06-10
    sessions, profile_id = manager
    info = sessions.create(SessionCreateRequest(profile_id=profile_id))
    recorder = Recorder()
    worker = sessions.attach(info.session_id, recorder)
    slow = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 100000000) SELECT count(*) FROM n"
    worker.submit(ExecuteCommand(request_id="slow", sql=slow))
    recorder.wait_for(lambda event: event.type == "started")
    sessions.detach(info.session_id, recorder)  # no WebSocket any more: the events still reach the test
    worker.set_emitter(recorder)
    assert worker.busy()
    assert sessions.reap_idle() == 0  # detached and past the idle time, but running
    worker.cancel("slow")
    request_events(recorder, "slow")
    assert sessions.reap_idle() == 1  # idle once its request ended (idle_timeout 0 here)


def test_the_job_routes(tmp_path):
    settings = Settings(token="t", data_dir=tmp_path / "data", log_to_file=False)
    container = AppContainer(settings, secret_store=InMemorySecretStore())
    auth = {"Authorization": "Bearer t"}
    with TestClient(create_app(settings, container), base_url="http://127.0.0.1") as client:
        assert client.get("/data/jobs", headers=auth).json() == []
        registry = container.sessions.jobs
        registry.begin("s1", uuid4(), "r1", "dump", "Dump public")
        registry.report("s1", JobOutcomeEvent(request_id="r1", job="dump", outcome="completed", path="/x.sql"))
        [listed] = client.get("/data/jobs", headers=auth).json()
        assert (listed["title"], listed["outcome"]["outcome"], listed["outcome"]["path"]) == (
            "Dump public", "completed", "/x.sql")
        missing = client.post("/data/jobs/cancel", json={"session_id": "s1", "request_id": "nope"}, headers=auth)
        assert missing.status_code == 404
        gone = client.post("/data/jobs/cancel", json={"session_id": "s1", "request_id": "r1"}, headers=auth)
        assert (gone.status_code, gone.json()["error"]["code"]) == (404, "session_not_found")
        assert client.post("/data/jobs/clear", headers=auth).status_code == 204
        assert client.get("/data/jobs", headers=auth).json() == []
        assert client.get("/data/jobs").status_code == 401


def test_a_cancel_from_another_thread_while_the_job_runs_is_safe(env):
    e = env()
    e.submit("sql block")
    assert e.conn.blocked.wait(5)
    threading.Thread(target=e.worker.cancel, args=("j",)).start()
    assert outcome(request_events(e.recorder, "j")).cancelled


def test_a_client_attaching_as_the_outcome_goes_out_gets_it_once_and_nothing_after_it(env):  # pre-flight M2
    e = env()
    keeping, release = threading.Event(), threading.Event()
    keep = e.worker._keep_job_event

    def slow_keep(event) -> None:
        if event.type == "job_outcome":
            keeping.set()
            assert release.wait(5)
        keep(event)

    e.worker._keep_job_event = slow_keep
    e.submit("tick!", "sql x")
    assert keeping.wait(5)
    later = Recorder()
    attach = threading.Thread(target=e.worker.attach_emitter, args=(later,))
    attach.start()
    attach.join(0.2)
    assert attach.is_alive()  # it waits until the outcome is kept and sent
    release.set()
    attach.join(5)
    e.worker.call(lambda connection: None)  # the job has ended
    assert [ev.type for ev in later.events if ev.type.startswith("job_")] == ["job_progress", "job_outcome"]
    assert types(e.recorder.events).count("job_outcome") == 1  # the live one went to the client it was sent to


def test_a_job_past_its_last_cancel_keeps_the_session_busy(env):  # pre-flight L1: a COMMIT is not idle time
    e = env()
    reached, go = HOLDS["commit"] = (threading.Event(), threading.Event())
    e.submit("sql x", "hold commit")
    assert reached.wait(5)
    assert e.worker.busy()  # no request "runs" any more (stop_cancels), yet the job does
    go.set()
    assert outcome(request_events(e.recorder, "j")).outcome == "completed"
    e.worker.call(lambda connection: None)
    assert not e.worker.busy()


def test_a_job_that_breaks_inside_its_transaction_rolls_it_back(env):  # pre-flight L2
    e = env()
    events = e.run("sql begin", "crash")
    assert outcome(events).outcome == "unknown"
    assert (e.conn.tx, e.conn.rollbacks) == (False, 1)
    assert events[-1].in_transaction is False


def test_a_job_whose_session_closed_before_its_outcome_is_listed_as_unknown():  # pre-flight L3
    registry = JobRegistry()
    registry.begin("s", uuid4(), "a", "dump", "Dump a")
    registry.begin("s", uuid4(), "b", "run_file", "Run b")
    registry.begin("other", uuid4(), "c", "dump", "Dump c")
    registry.report("s", JobOutcomeEvent(request_id="b", job="run_file", outcome="completed"))
    assert registry.abandon("s") == 1
    a, b, c = (registry.get(sid, rid) for sid, rid in (("s", "a"), ("s", "b"), ("other", "c")))
    assert (a.outcome.outcome, a.outcome.job, a.outcome.warnings) == ("unknown", "dump", [SESSION_CLOSED_FIRST])
    assert (b.outcome.outcome, c.outcome) == ("completed", None)
    registry.report("s", JobOutcomeEvent(request_id="a", job="dump", outcome="failed"))  # the worker's, after all
    assert registry.get("s", "a").outcome.outcome == "failed"


def test_closing_a_session_lists_its_unfinished_job_as_unknown(manager):  # pre-flight L3, through the manager
    sessions, profile_id = manager
    info = sessions.create(SessionCreateRequest(profile_id=profile_id))
    sessions.jobs.begin(info.session_id, profile_id, "r", "run_file", "Run r")  # as if its worker never reported
    sessions.close(info.session_id)
    assert sessions.jobs.get(info.session_id, "r").outcome.outcome == "unknown"


class _Dump(JobCommand):
    """A job command the WebSocket accepts (the real ones arrive with Run SQL file and dump)."""

    job_kind = "dump"
    type: Literal["dump"] = "dump"


@pytest.fixture
def api(tmp_path):
    settings = Settings(token="t", data_dir=tmp_path / "data", log_to_file=False)
    container = AppContainer(settings, secret_store=InMemorySecretStore())
    db = tmp_path / "api.db"
    sqlite3.connect(db).close()
    with TestClient(create_app(settings, container), base_url="http://127.0.0.1") as client:
        body = {"profile": {"engine": "sqlite", "name": "local", "path": str(db)}}
        auth = {"Authorization": "Bearer t"}
        profile_id = client.post("/profiles", json=body, headers=auth).json()["profile"]["id"]
        session_id = client.post("/sessions", json={"profile_id": profile_id}, headers=auth).json()["session_id"]
        yield client, container, session_id


def _receive(ws, count: int) -> list[dict]:
    """The next `count` events that are not a `session_state`."""
    events: list[dict] = []
    while len(events) < count:
        if (event := ws.receive_json())["type"] != "session_state":
            events.append(event)
    return events


def test_a_job_command_refused_by_the_websocket_still_gets_its_outcome(api, monkeypatch):  # pre-flight M1, R-06-12
    client, container, session_id = api
    with client.websocket_connect(f"ws://127.0.0.1/sessions/{session_id}/ws?token=t") as ws:
        ws.send_json({"type": "run_file", "request_id": "r", "path": "/x.sql", "mode": "all_or_nothing",
                      "on_error": "continue"})  # the protocol refuses it
        invalid = _receive(ws, 3)
        monkeypatch.setattr(sessions_api, "_COMMANDS", TypeAdapter(_Dump))
        container.sessions.worker(session_id).close()  # the socket stays attached to a dead worker
        ws.send_json({"type": "dump", "request_id": "late"})
        busy = _receive(ws, 3)
    cases = ((invalid, "r", "invalid_command", "run_file"), (busy, "late", "session_busy", "dump"))
    for events, rid, code, job in cases:
        assert [(e["type"], e["request_id"]) for e in events] == [
            ("error", rid), ("job_outcome", rid), ("finished", rid)]
        assert (events[0]["code"], events[1]["job"], events[1]["outcome"]) == (code, job, "not_started")
        assert events[1]["error"] == {"line": None, "statement": None, "code": code,
                                      "message": events[0]["message"]}  # fix wave F3: as the worker's carry it


def test_a_cancel_a_job_lets_through_is_its_outcomes_flag(env):  # fix round 1: not_started, or unknown once sent
    for steps, expected in ((("wait w", "check"), "not_started"), (("sql x", "wait w", "check"), "unknown")):
        e = env()
        reached, go = HOLDS["w"] = (threading.Event(), threading.Event())
        e.submit(*steps)
        assert reached.wait(5)
        e.worker.cancel("j")  # nothing in the driver to interrupt: the flag waits for the job's own check
        go.set()
        events = request_events(e.recorder, "j")
        assert (outcome(events).outcome, outcome(events).cancelled, events[-1].cancelled) == (expected, True, True)


def test_a_broke_override_that_fails_too_still_ends_with_one_outcome(env):  # fix round 1
    events = env().run("sql x", "crash", broke="fails")
    assert types(events)[-4:] == ["error", "job_outcome", "session_state", "finished"]
    assert (outcome(events).outcome, next(ev for ev in events if ev.type == "error").message) == (
        "unknown", "Internal error: bug")


def test_a_broke_override_keeps_the_cancel_flag(env):  # fix round 1: the framework sets it whatever broke() says
    e = env()
    reached, go = HOLDS["w2"] = (threading.Event(), threading.Event())
    e.submit("sql x", "wait w2", "check", broke="plain")
    assert reached.wait(5)
    e.worker.cancel("j")
    go.set()
    done = outcome(request_events(e.recorder, "j"))
    assert (done.outcome, done.cancelled) == ("unknown", True)


class StuckConnection(FakeConnection):
    """A ROLLBACK that fails and leaves the transaction open."""

    def rollback(self) -> None:
        raise StatementError("rollback refused", code="08006")


def test_a_broken_jobs_rollback_that_fails_says_so_in_a_jobs_words(env):  # fix round 1 (L2's text)
    events = env(conn=StuckConnection()).run("sql begin", "crash")
    assert [ev.text for ev in events if ev.type == "message"] == [JOB_ROLLBACK_FAILED]
    assert outcome(events).outcome == "unknown" and events[-1].in_transaction
    assert outcome(events).warnings == [JOB_ROLLBACK_FAILED]  # in the outcome too, for a later window (F3)


def test_idle_time_counts_from_the_last_finished_not_from_the_detach(tmp_path):  # D-06-10, fix round 1
    now = [1000.0]
    sessions, profile_id = sqlite_sessions(tmp_path, idle_timeout=600, clock=lambda: now[0])
    try:
        info = sessions.create(SessionCreateRequest(profile_id=profile_id))
        recorder = Recorder()
        worker = sessions.attach(info.session_id, recorder)
        sessions.detach(info.session_id, recorder)  # at 1000
        worker.set_emitter(recorder)
        now[0] = 1300.0
        worker.submit(ExecuteCommand(request_id="q", sql="SELECT 1"))
        request_events(recorder, "q")  # finished at 1300
        now[0] = 1700.0  # 700 s after the detach, 400 s after the request ended
        assert sessions.reap_idle() == 0
        now[0] = 1900.0
        assert sessions.reap_idle() == 1
    finally:
        sessions.close_all()


@pytest.mark.parametrize("kind", [["run_file"], {"a": 1}, 7, None])
def test_a_command_whose_type_is_not_a_string_is_refused_and_the_socket_lives(api, kind):  # fix round 1
    client, _, session_id = api
    with client.websocket_connect(f"ws://127.0.0.1/sessions/{session_id}/ws?token=t") as ws:
        ws.send_json({"type": kind, "request_id": "r"})
        refused = _receive(ws, 2)
        assert [(e["type"], e["request_id"]) for e in refused] == [("error", "r"), ("finished", "r")]
        assert refused[0]["code"] == "invalid_command"
        ws.send_json({"type": "execute", "request_id": "q", "sql": "SELECT 1"})
        assert _receive(ws, 1)[0]["type"] == "started"
