"""The job framework's parts a copy adds (06c Task 3): a helper job on another session (never listed, run inside that
session's transaction), a Cancel held between two tables, the source's session lease, the job list's source profile,
and a copy's source read into the pipe on the dev databases (fetch sizing, a query's checks, its settings set back)."""

import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from dbaguy_backend.adapters.base import StatementError
from dbaguy_backend.adapters.columns import TableColumn
from dbaguy_backend.domain.data_jobs import INVALID_FILTER
from dbaguy_backend.domain.edits import PLAN_MISMATCH, PlanUnavailable
from dbaguy_backend.domain.execution import ExecuteCommand
from dbaguy_backend.domain.transfer import COPY_PLAN_EXPIRED, QUERY_NOT_SELECT
from dbaguy_backend.sessions.jobs.context import Job, JobContext, JobReport, PreparedJob
from dbaguy_backend.sessions.jobs.pipe import RowPipe, SourceFailed
from dbaguy_backend.sessions.jobs.registry import JobRegistry
from dbaguy_backend.sessions.jobs.transfer_source import MANY_RESULTS, TransferSource
from dbaguy_backend.sessions.manager import SessionCreateRequest
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.select_builder import FILTER_NOT_A_CONDITION
from tests.data_support import DataApp, dropping
from tests.edit_support import scratch_db
from tests.fake_db import FakeResult
from tests.support import requires_mysql
from tests.test_job_framework import HOLDS, Env, outcome
from tests.test_sessions import request_events
from tests.transfer_support import planned, profile, table_name


class Helper(Job):
    kind = "copy_data"
    helper = True

    @property
    def title(self) -> str:
        return "a helper"

    def run(self, ctx: JobContext) -> JobReport:
        ctx.drain(ctx.run("x"))
        return JobReport(ctx.outcome("completed", statements=1))


class CopyEnv(Env):
    @staticmethod
    def _build(command):
        return command.job if isinstance(command, PreparedJob) else Env._build(command)


@pytest.fixture
def env():
    made: list[CopyEnv] = []

    def make(**fields) -> CopyEnv:
        made.append(CopyEnv(**fields))
        return made[-1]

    yield make
    for e in made:
        e.worker.close()


def test_a_helper_job_runs_inside_the_sessions_transaction_and_is_never_listed(env) -> None:
    e = env()
    e.conn.tx = True  # a query tab's open transaction: a copy's source reads in it, as an export does
    e.worker.submit(PreparedJob(request_id="h", job=Helper()))
    events = request_events(e.recorder, "h")
    assert (outcome(events).outcome, e.conn.runs) == ("completed", ["x"])
    assert not [item for item in e.listed if item[0] == "announce"]


def test_a_cancel_while_the_statements_cannot_be_cancelled_stops_the_next_table(env) -> None:
    HOLDS["between"] = (threading.Event(), threading.Event())
    e = env()
    e.submit("sql first", "hold between", "resume", "sql second")
    reached, go = HOLDS["between"]
    assert reached.wait(5)
    e.worker.cancel("j")  # its COMMIT, say: kept, never lost
    go.set()
    events = request_events(e.recorder, "j")
    assert (outcome(events).outcome, outcome(events).cancelled, e.conn.runs) == ("stopped", True, ["first"])


def test_without_a_cancel_the_next_table_goes_on(env) -> None:
    HOLDS["calm"] = (threading.Event(), threading.Event())
    e = env()
    e.submit("sql first", "hold calm", "resume", "sql second")
    assert HOLDS["calm"][0].wait(5)
    HOLDS["calm"][1].set()
    events = request_events(e.recorder, "j")
    assert (outcome(events).outcome, e.conn.runs) == ("completed", ["first", "second"])


def test_a_session_that_closes_between_two_tables_starts_no_next_table(env) -> None:
    HOLDS["closing"] = (threading.Event(), threading.Event())
    e = env()
    e.submit("sql first", "hold closing", "resume", "sql second")
    reached, go = HOLDS["closing"]
    assert reached.wait(5)
    closer = threading.Thread(target=e.worker.close)  # a close interrupts a running request only, and none runs
    closer.start()
    time.sleep(0.2)
    go.set()
    events = request_events(e.recorder, "j")
    closer.join(10)
    assert (outcome(events).outcome, outcome(events).cancelled, e.conn.runs) == ("stopped", True, ["first"])


class Breaks(Helper):
    def run(self, ctx: JobContext) -> JobReport:
        ctx.drain(ctx.run("x"))
        raise RuntimeError("bug")


def test_a_helper_that_breaks_never_rolls_back_its_sessions_transaction(env) -> None:  # the tab's, not the job's
    e = env()
    e.conn.tx = True
    e.worker.submit(PreparedJob(request_id="h", job=Breaks()))
    assert outcome(request_events(e.recorder, "h")).outcome == "unknown"
    assert (e.conn.tx, e.conn.rollbacks) == (True, 0)


def test_the_job_list_names_a_copys_source_connection() -> None:
    registry, source = JobRegistry(), uuid4()
    registry.begin("s", uuid4(), "r", "copy_data", "Copy t from a to b", source)
    assert registry.list()[0].source_profile_id == source


def test_a_copys_source_is_its_query_tabs_session_or_one_of_its_own(tmp_path: Path) -> None:
    app = DataApp(tmp_path)
    lite = scratch_db("sqlite", tmp_path)
    try:
        mine, other = profile(app, lite, "mine"), profile(app, lite, "other")
        manager = app.container.sessions
        tab = manager.create(SessionCreateRequest(profile_id=mine))
        copying = manager.create(SessionCreateRequest(profile_id=mine)).session_id  # the copy's own job session
        borrowed = manager._source(mine, None, tab.session_id, from_session=copying)
        assert borrowed.worker is manager.worker(tab.session_id)
        assert manager._sessions[tab.session_id].attached == 1  # leased: never reaped under the copy (review M-6)
        borrowed.release()
        assert manager.worker(tab.session_id) is borrowed.worker  # a query tab's session stays the tab's
        assert manager._sessions[tab.session_id].attached == 0
        with pytest.raises(PlanUnavailable) as mismatch:
            manager._source(other, None, tab.session_id, from_session=copying)  # the plan's source is another one
        assert mismatch.value.code == PLAN_MISMATCH
        assert mismatch.value.message == COPY_PLAN_EXPIRED  # a copy's words, not a grid edit's (pre-flight L-a)
        with pytest.raises(PlanUnavailable) as itself:  # its read would wait behind the copy until Cancel (M-7)
            manager._source(mine, None, copying, from_session=copying)
        assert (itself.value.code, manager._sessions[copying].attached) == (PLAN_MISMATCH, 0)
        own = manager._source(other, None, None, from_session=copying)
        opened = [sid for sid in manager._sessions if manager.worker(sid) is own.worker]
        assert manager._sessions[opened[0]].attached == 1  # leased: never reaped as idle between two tables
        own.release()
        assert opened and opened[0] not in manager._sessions  # the copy's own session ends with it
    finally:
        lite.close()
        app.close()


def _column(name: str) -> TableColumn:
    return TableColumn(name=name, type_name="", family="other", nullable=True, has_default=False, kind="other",
                       compare="none", writable=True)


def _drain(pipe: RowPipe, within_s: float = 30.0) -> list[list]:
    deadline = time.monotonic() + within_s

    def waited_too_long() -> None:  # a test that would hang fails instead
        assert time.monotonic() < deadline, "the source never ended the pipe"

    chunks = []
    while (chunk := pipe.take(waited_too_long)) is not None:
        chunks.append(chunk)
    return chunks


def test_a_source_reads_its_rows_a_chunk_at_a_time_sized_by_their_text(tmp_path: Path) -> None:
    app = DataApp(tmp_path)
    lite = scratch_db("sqlite", tmp_path)
    try:
        with dropping(lite, before_drop=app.close_sessions) as made:
            name = table_name()
            lite.execute(f"CREATE TABLE {lite.table(name)} (n INTEGER)")
            made.append(("TABLE", name))
            lite.execute(f"WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 2500) "
                         f"INSERT INTO {lite.table(name)} SELECT n FROM s")
            tab = app.job_session(profile(app, lite, "lite"))
            pipe = RowPipe("t", poll_s=0.01)
            tab.worker.submit(PreparedJob(request_id="src", job=TransferSource(
                pipe, planned(f"SELECT n FROM {lite.table(name)}", [_column("n")]), query=False, label="t")))
            chunks = _drain(pipe)
        assert [len(chunk) for chunk in chunks] == [1, 1000, 1000, 499]  # the first read is one row (06a M1)
        assert chunks[0][0] == [1] and chunks[-1][-1] == [2500]
    finally:
        lite.close()
        app.close()


def test_a_query_that_writes_or_changed_its_columns_is_never_read(tmp_path: Path) -> None:
    app = DataApp(tmp_path)
    lite = scratch_db("sqlite", tmp_path)
    try:
        with dropping(lite, before_drop=app.close_sessions) as made:
            name = table_name()
            lite.execute(f"CREATE TABLE {lite.table(name)} (a INTEGER)", f"INSERT INTO {lite.table(name)} VALUES (1)")
            made.append(("TABLE", name))
            tab = app.job_session(profile(app, lite, "lite"))
            causes = []
            for sql, columns in ((f"DELETE FROM {lite.table(name)} RETURNING a", ["a"]),
                                 (f"SELECT a AS b FROM {lite.table(name)}", ["a"])):
                pipe = RowPipe("Query 1", poll_s=0.01)
                tab.worker.submit(PreparedJob(request_id=f"q{len(causes)}", job=TransferSource(
                    pipe, planned(sql, [_column(c) for c in columns]), query=True, label="q")))
                with pytest.raises(SourceFailed) as failed:
                    _drain(pipe)
                causes.append(failed.value.cause.code)
            assert lite.rows(f"SELECT a FROM {lite.table(name)}") == [(1,)]  # the DELETE never ran
        assert causes == ["query_not_select", "columns_changed"]
    finally:
        lite.close()
        app.close()


@requires_mysql
def test_a_mysql_source_reads_in_utc_without_a_write_timeout_and_its_tab_gets_its_settings_back(
        tmp_path: Path) -> None:
    app = DataApp(tmp_path)
    my = scratch_db("mysql", tmp_path)
    try:
        tab = app.job_session(profile(app, my, "my"))
        tab.run(lambda rid: ExecuteCommand(request_id=rid, sql="SET SESSION time_zone = '+02:00'"))
        pipe = RowPipe("Query 1", poll_s=0.01)
        sql = "SELECT @@SESSION.time_zone AS tz, @@SESSION.net_write_timeout AS w"
        tab.worker.submit(PreparedJob(request_id="src", job=TransferSource(
            pipe, planned(sql, [_column("tz"), _column("w")]), query=True, label="q")))
        assert _drain(pipe) == [[["+00:00", 3600]]]  # A11: a slow target cannot outwait the server's write
        after = tab.run(lambda rid: ExecuteCommand(request_id=rid, sql=sql))
        [rows] = [event.rows for event in after.events if event.type == "rows"]
        assert rows == [["+02:00", 60]]  # the tab's own session as it was
    finally:
        my.close()
        app.close()


# ---- 06c pre-flight M1 (the framework's half) and M4 (the source's half), on a fake connection ----


def _source(pipe: RowPipe, sql: str = "SELECT n FROM t", *, query: bool = False) -> TransferSource:
    return TransferSource(pipe, planned(sql, [_column("n")]), query=query, label="t")


def test_a_source_that_never_starts_tells_the_pipe(env) -> None:  # M1: its sink must not wait for rows
    e = env()
    e.worker.submit(ExecuteCommand(request_id="busy", sql="block"))
    assert e.conn.blocked.wait(5)
    queued = RowPipe("t", poll_s=0.01)
    e.worker.submit(PreparedJob(request_id="queued", job=_source(queued)))
    e.worker.cancel("queued")  # cancelled while it waited behind the tab's query
    e.worker.cancel("busy")
    with pytest.raises(SourceFailed) as failed:
        _drain(queued, within_s=10)
    assert (failed.value.cause.code, outcome(request_events(e.recorder, "queued")).outcome) == (
        "not_started", "not_started")

    closing = env()
    closing.worker.submit(ExecuteCommand(request_id="busy", sql="block"))
    assert closing.conn.blocked.wait(5)
    late = RowPipe("t", poll_s=0.01)
    closing.worker.submit(PreparedJob(request_id="late", job=_source(late)))
    closing.worker.close()  # the tab's session closed first
    with pytest.raises(SourceFailed) as closed:
        _drain(late, within_s=10)
    assert (closed.value.cause.code, outcome(request_events(closing.recorder, "late")).outcome) == (
        "not_started", "not_started")


class _TwoResults(FakeResult):
    """A T-SQL batch of two SELECTs: one statement, two results of one row each."""

    def __init__(self) -> None:
        super().__init__(1)
        self._second: list | None = [7]

    def next_result(self) -> bool:
        if self._second is None:
            return False
        self._rows, self._second = self._second, None
        return True


def test_a_query_with_more_than_one_result_is_never_copied(env) -> None:  # M4: its second result is not the first
    e = env()
    e.conn.statement = lambda sql, options: _TwoResults()
    pipe = RowPipe("Query 1", poll_s=0.01)
    e.worker.submit(PreparedJob(request_id="q", job=_source(pipe, query=True)))
    with pytest.raises(SourceFailed) as failed:
        _drain(pipe, within_s=10)
    assert (failed.value.cause.code, failed.value.cause.message) == (QUERY_NOT_SELECT, MANY_RESULTS)
    assert outcome(request_events(e.recorder, "q")).outcome == "failed"


def test_a_read_stops_at_its_next_chunk_once_its_sink_stops(env) -> None:  # the pipe closed, nothing cancelled
    e = env()
    result = FakeResult(5_000)
    e.conn.statement = lambda sql, options: result
    pipe = RowPipe("t", capacity=1, poll_s=0.01)
    e.worker.submit(PreparedJob(request_id="src", job=_source(pipe)))
    assert pipe.take(lambda: None) == [[0]]  # the first read is one row
    pipe.close()
    done = outcome(request_events(e.recorder, "src"))
    assert (done.outcome, done.cancelled, result.closed, e.conn.cancel_calls) == ("failed", True, True, 0)


# ---- 06c Task 3 review M-1…M-7, applied with Task 4 ----


def test_a_source_whose_sink_stopped_before_it_began_sends_nothing(env) -> None:  # M-1 (a)
    e = env()
    pipe = RowPipe("t", poll_s=0.01)
    pipe.close()  # its copy stopped while the read waited in the queue
    e.worker.submit(PreparedJob(request_id="src", job=_source(pipe)))
    done = outcome(request_events(e.recorder, "src"))
    assert (done.outcome, done.cancelled, e.conn.runs) == ("not_started", True, [])


def test_a_queued_request_is_withdrawn_and_a_running_one_never_is(env) -> None:  # M-1 (b): H2's way to stop a read
    e = env()
    e.worker.submit(ExecuteCommand(request_id="busy", sql="block"))
    assert e.conn.blocked.wait(5)
    queued = RowPipe("t", poll_s=0.01)
    e.worker.submit(PreparedJob(request_id="queued", job=_source(queued)))
    assert (e.worker.withdraw("busy"), e.conn.cancel_calls) == (False, 0)  # it runs: never cancelled by a withdraw
    assert e.worker.withdraw("queued") is True
    e.worker.cancel("busy")
    with pytest.raises(SourceFailed) as failed:
        _drain(queued, within_s=10)
    assert (failed.value.cause.code, outcome(request_events(e.recorder, "queued")).outcome) == (
        "not_started", "not_started")
    assert e.conn.runs == ["block"] and e.worker.withdraw("queued") is False  # nothing of it ran; it is over


def test_a_cancel_that_found_nothing_to_stop_survives_resuming(env) -> None:  # M-2: never lost
    HOLDS["m2"] = (threading.Event(), threading.Event())
    e = env()
    e.submit("sql first", "wait m2", "resume", "sql second")
    reached, go = HOLDS["m2"]
    assert reached.wait(5)
    e.worker.cancel("j")  # it is running, and no driver call is there to stop
    go.set()
    events = request_events(e.recorder, "j")
    assert (outcome(events).outcome, outcome(events).cancelled, e.conn.runs) == ("stopped", True, ["first"])


def test_a_query_that_may_not_run_leaves_the_tabs_settings_untouched(env) -> None:  # M-3: refused before anything
    e = env()
    e.worker.configure(replace(e.worker._hooks, dialect=Dialect.POSTGRES))  # a source with settings to set and reset
    pipe = RowPipe("Query 1", poll_s=0.01)
    e.worker.submit(PreparedJob(request_id="q", job=_source(pipe, "DELETE FROM t RETURNING n", query=True)))
    with pytest.raises(SourceFailed) as failed:
        _drain(pipe, within_s=10)
    assert (failed.value.cause.code, e.conn.runs) == (QUERY_NOT_SELECT, [])
    assert outcome(request_events(e.recorder, "q")).outcome == "not_started"


def test_a_filter_that_is_not_a_condition_is_said_in_a_filters_words(env) -> None:  # M-4: the dump's words
    e = env()
    pipe = RowPipe("t", poll_s=0.01)
    table = planned("SELECT n FROM t WHERE 1 = 1; DELETE FROM t", [_column("n")], filtered=True)
    e.worker.submit(PreparedJob(request_id="f", job=TransferSource(pipe, table, query=False, label="t")))
    with pytest.raises(SourceFailed) as failed:
        _drain(pipe, within_s=10)
    assert (failed.value.cause.code, failed.value.cause.message, e.conn.runs) == (
        INVALID_FILTER, FILTER_NOT_A_CONDITION, [])


class _RefusesAtOnce(Helper):
    """A helper whose run raises before it sends anything; it records what its maker heard."""

    def __init__(self) -> None:
        self.heard: list[str | None] = []

    def run(self, ctx: JobContext) -> JobReport:
        raise StatementError("refused", code="refused")

    def not_run(self, error: StatementError) -> None:
        self.heard.append(error.code)


def test_a_helper_whose_run_raises_before_it_sends_anything_tells_its_maker(env) -> None:  # M-5
    e = env()
    helper = _RefusesAtOnce()
    e.worker.submit(PreparedJob(request_id="h", job=helper))
    assert outcome(request_events(e.recorder, "h")).outcome == "not_started"
    assert (helper.heard, e.conn.runs) == (["refused"], [])
