"""Run SQL file (spec ⑥ §3.4, R-06-7, R-06-12) on a fake connection, so every way a run ends is forced: what is sent
and never sent, each outcome, the stops that skip-and-continue does not skip, psql lines, GO n, progress and History.
The live engines follow in test_run_file.py."""

from pathlib import Path

import pytest

from dbaguy_backend.adapters.base import AdapterCapabilities, StatementCancelled, StatementError
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.domain.classification import Classification, StatementKind
from dbaguy_backend.domain.dialect import Dialect
from dbaguy_backend.domain.execution import RunFileCommand
from dbaguy_backend.domain.profiles import Engine, SqliteProfile
from dbaguy_backend.services.safety import GuardAction, GuardDecision
from dbaguy_backend.sessions.hooks import SessionHooksFactory
from dbaguy_backend.sessions.jobs import run_file
from dbaguy_backend.sessions.jobs.context import JobHooks
from dbaguy_backend.sessions.jobs.run_file import COPY_HINT, HEX_BLOB_HINT, RunFileJob, summary
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from dbaguy_backend.sql.dump_header import CUT_SHORT, complete_line, header_lines
from dbaguy_backend.sql.statement_stream import UTF16_MESSAGE
from tests.fake_db import FakeConnection, FakeLease, FakeResult
from tests.test_sessions import Recorder, request_events

LOST = StatementError("Connection to the server was lost", code="connection_lost")
FRAME = {Dialect.SQLITE: ("BEGIN IMMEDIATE", "COMMIT"), Dialect.POSTGRES: ("BEGIN", "COMMIT"),
         Dialect.MYSQL: ("START TRANSACTION", "COMMIT"), Dialect.TSQL: ("SET NOCOUNT OFF;\nBEGIN TRANSACTION",
                                                                        "COMMIT TRANSACTION")}


class RunFake(FakeConnection):
    """The frame's BEGIN opens a transaction and its COMMIT ends it (or raises `commit_error`; `commit_keeps` leaves it
    open). Statement words: `rows N`, `block` (waits for a cancel), `fail MSG`, `lost` (the link dies), `endtx` (ends
    the transaction, as MySQL DDL does), `failend` (fails and ends the transaction: a deadlock, XACT_ABORT, or a
    procedure's COMMIT before its error), `again` (fails from its second run on), `copy` (the adapter's COPY refusal),
    `tx` (opens one), anything else: done. `acts` gives a whole text one of those words' behaviour (`CALL p()`)."""

    def __init__(self, dialect: Dialect = Dialect.SQLITE, commit_error: StatementError | None = None,
                 commit_keeps: bool = False, acts: dict[str, str] | None = None) -> None:
        super().__init__()
        self.begin, self.end = FRAME[dialect]
        self.commit_error = commit_error
        self.commit_keeps = commit_keeps
        self.acts = acts or {}
        self.seen: dict[str, int] = {}

    def statement(self, sql, options):
        self.seen[sql] = self.seen.get(sql, 0) + 1
        if sql == self.begin:
            self.runs.append(sql)
            self.tx = True
            return FakeResult(0)
        if sql == self.end:
            self.runs.append(sql)
            if self.commit_error is not None:
                if self.commit_error.code != "connection_lost":
                    self.tx = self.commit_keeps
                raise self.commit_error
            if not self.commit_keeps:
                self.tx = False
                self.commits += 1
            return FakeResult(0)
        acted = self.acts.get(sql, sql)
        word = acted.split()[0] if acted.split() else ""
        if word in ("lost", "endtx", "failend", "tx") or (word == "again" and self.seen[sql] > 1):
            self.runs.append(sql)
        if word == "lost":
            raise LOST
        if word == "endtx":
            self.tx = False
            return FakeResult(0)
        if word == "failend":
            self.tx = False
            raise StatementError("deadlock", code="40001")
        if word == "again" and self.seen[sql] > 1:
            raise StatementError("duplicate", code="23505")
        if word == "copy":
            raise StatementError("COPY FROM STDIN / TO STDOUT is not supported in the console yet",
                                 code="unsupported_copy")
        if word == "tx":
            self.tx = True
            return FakeResult(0)
        return super().statement(acted, options)  # an acted text is recorded as its word's (`fail` …)


def runs_everything(sql: str) -> GuardDecision:
    return GuardDecision(action=GuardAction.RUN, classification=Classification(kind=StatementKind.UNKNOWN))


class Env:
    def __init__(self, conn: RunFake, *, dialect: Dialect = Dialect.SQLITE, gate=runs_everything, read_only=False,
                 transactions: bool = True, recorder: Recorder | None = None) -> None:
        self.conn = conn
        self.records: list[tuple] = []
        lease = FakeLease(conn)
        self.worker = SessionWorker("run", lambda: lease)
        self.worker.start()
        jobs = JobHooks(build=lambda command: RunFileJob(command, "Fake DB"), gate=gate, read_only=lambda: read_only,
                        begin_record=lambda sql, at: self.records.append(("begin", sql)) or 1,
                        finish_record=lambda *args: self.records.append(("finish", *args)))
        capabilities = AdapterCapabilities(run_file=True, transactions=transactions)
        self.worker.configure(WorkerHooks(dialect=dialect, capabilities=capabilities, jobs=jobs))
        self.recorder = recorder or Recorder()
        self.worker.set_emitter(self.recorder)

    def submit(self, path: Path, rid: str = "r", **fields) -> None:
        fields.setdefault("mode", "all_or_nothing")
        self.worker.submit(RunFileCommand(request_id=rid, path=str(path), **fields))

    def run(self, path: Path, **fields) -> list:
        self.submit(path, **fields)
        return request_events(self.recorder, "r")


@pytest.fixture
def env():
    made: list[Env] = []

    def make(conn: RunFake | None = None, **fields) -> Env:
        made.append(Env(conn or RunFake(fields.get("dialect", Dialect.SQLITE), acts=fields.pop("acts", None)),
                        **fields))
        return made[-1]

    yield make
    for e in made:
        e.worker.close()


@pytest.fixture
def sql_file(tmp_path):
    def write(text: str | bytes, name: str = "run.sql") -> Path:
        path = tmp_path / name
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
        return path

    return write


def outcome(events):
    [found] = [e for e in events if e.type == "job_outcome"]
    return found


def error(events):
    return next((e for e in events if e.type == "error"), None)


def test_all_or_nothing_sends_the_file_between_begin_and_commit(env, sql_file):
    e = env()
    path = sql_file("rows 1;\nrows 2;\n-- done\n")
    events = e.run(path)
    done = outcome(events)
    assert (done.outcome, done.statements, done.cancelled, done.path, done.bytes) == (
        "completed", 2, False, str(path), len("rows 1;\nrows 2;"))
    assert e.conn.runs == ["BEGIN IMMEDIATE", "rows 1", "rows 2", "COMMIT"] and not e.conn.tx
    assert [ev.type for ev in events][-3:] == ["job_outcome", "session_state", "finished"]
    progress = [ev for ev in events if ev.type == "job_progress"]
    assert progress[0].total_bytes == path.stat().st_size and progress[-1].stage == "commit"
    assert e.records[0] == ("begin", f"-- Run SQL file {path} (0.0 MB), all or nothing")
    assert e.records[1][2] == "ok"


@pytest.mark.parametrize(("statement", "code", "rolled_back_by_us"), [
    ("fail boom", "42000", True), ("block", "cancelled", True), ("lost", "connection_lost", False),
])
def test_all_or_nothing_stops_roll_everything_back(env, sql_file, statement, code, rolled_back_by_us):
    e = env()
    e.submit(sql_file(f"rows 1;\n{statement};\nrows 3;"))
    if statement == "block":
        assert e.conn.blocked.wait(5)
        e.worker.cancel("r")
    events = request_events(e.recorder, "r")
    done = outcome(events)
    assert (done.outcome, done.cancelled, done.stopped_at.line, done.stopped_at.statement, done.statements) == (
        "rolled_back", statement == "block", 2, 2, 1)
    assert error(events).code == code
    assert "rows 3" not in e.conn.runs and "COMMIT" not in e.conn.runs
    assert e.conn.rollbacks == (1 if rolled_back_by_us else 0)
    assert events[-1].cancelled == (statement == "block")


def test_a_transaction_the_file_ends_stops_an_all_or_nothing_run_as_partial(env, sql_file):  # MySQL DDL, M8
    e = env(dialect=Dialect.MYSQL)
    events = e.run(sql_file("rows 1;\nendtx;\nrows 3;"))
    done = outcome(events)
    assert (done.outcome, done.stopped_at.line, done.statements) == ("partial", 2, 2)
    assert error(events).code == "transaction_ended" and "MySQL commits around DDL" in error(events).message
    assert e.conn.runs == ["START TRANSACTION", "rows 1", "endtx"]


@pytest.mark.parametrize("statement", ["COMMIT", "BEGIN TRANSACTION", "END", "ROLLBACK", "SAVEPOINT a"])
def test_the_files_own_transaction_statements_are_never_sent_all_or_nothing(env, sql_file, statement):
    e = env()
    events = e.run(sql_file(f"rows 1;\n{statement};\nrows 3;"))
    assert (outcome(events).outcome, error(events).code) == ("rolled_back", "transaction_in_file")
    assert error(events).message.startswith("The file controls its own transactions (line 2: ")
    assert e.conn.runs == ["BEGIN IMMEDIATE", "rows 1"]


def test_words_that_only_look_like_transactions_run(env, sql_file):
    e = env()
    events = e.run(sql_file("rows 1;\nbegin_x;\n"))  # not a transaction statement: sent
    assert outcome(events).outcome == "completed" and "begin_x" in e.conn.runs


@pytest.mark.parametrize(("commit_error", "keeps", "expected"), [
    (StatementError("deferred constraint", code="23503"), False, "rolled_back"),
    (LOST, False, "unknown"),
    (None, True, "unknown"),  # COMMIT returned with a transaction still open (⑤ G11)
])
def test_what_commit_answers_decides_the_outcome(env, sql_file, commit_error, keeps, expected):
    e = env(RunFake(commit_error=commit_error, commit_keeps=keeps))
    events = e.run(sql_file("rows 1;"))
    assert outcome(events).outcome == expected
    if expected == "rolled_back":
        assert error(events).message == "deferred constraint\nThe transaction was rolled back: nothing was applied"
    if keeps:
        assert error(events).code == "commit_unconfirmed" and e.conn.rollbacks == 1


def test_statement_by_statement_stops_at_an_error_or_skips_it(env, sql_file):
    path = sql_file("rows 1;\nfail boom;\nrows 3;\nfail again;\nrows 5;")
    stop = env()
    events = stop.run(path, mode="statements")
    done = outcome(events)
    assert (done.outcome, done.stopped_at.line, done.statements, stop.conn.runs) == (
        "stopped", 2, 1, ["rows 1", "fail boom"])
    go_on = env()
    events = go_on.run(path, mode="statements", on_error="continue")
    done = outcome(events)
    assert (done.outcome, done.statements, done.error_count) == ("completed", 3, 2)
    assert [(err.line, err.statement, err.code, err.message) for err in done.errors] == [
        (2, 2, "42000", "boom"), (4, 4, "42000", "again")]
    assert go_on.conn.runs == ["rows 1", "fail boom", "rows 3", "fail again", "rows 5"]
    finish = go_on.records[1]  # ("finish", record id, status, ms, rows, error)
    assert (finish[2], finish[5]) == ("ok", "2 error(s) skipped")


def test_statement_by_statement_a_lost_link_is_unknown_and_a_cancel_stops(env, sql_file):
    lost = env()
    done = outcome(lost.run(sql_file("rows 1;\nlost;\nrows 3;"), mode="statements", on_error="continue"))
    assert (done.outcome, done.stopped_at.line) == ("unknown", 2)
    cancelled = env()
    cancelled.submit(sql_file("rows 1;\nblock;\nrows 3;"), mode="statements")
    assert cancelled.conn.blocked.wait(5)
    cancelled.worker.cancel("r")
    done = outcome(request_events(cancelled.recorder, "r"))
    assert (done.outcome, done.cancelled, done.stopped_at.statement, done.statements) == ("stopped", True, 2, 1)


def test_from_statement_reads_the_earlier_statements_without_sending_them(env, sql_file):
    e = env()
    events = e.run(sql_file("rows 1;\nrows 2;\nrows 3;"), mode="statements", from_statement=2)
    assert (outcome(events).statements, e.conn.runs) == (2, ["rows 2", "rows 3"])
    beyond = env()
    done = outcome(beyond.run(sql_file("rows 1;", name="short.sql"), mode="statements", from_statement=5))
    assert done.outcome == "completed" and done.warnings == ["The file has 1 statements: none from statement 5 on was run"]


def test_psql_lines_are_skipped_or_stop_the_run(env, sql_file):
    e = env(dialect=Dialect.POSTGRES)
    events = e.run(sql_file("\\restrict abc\nrows 1;\n\\unrestrict abc\n"), mode="statements")
    assert outcome(events).outcome == "completed"
    assert [ev.text for ev in events if ev.type == "message"] == [
        "Skipped line 1: \\restrict is a psql command", "Skipped line 3: \\unrestrict is a psql command"]
    stop = env(dialect=Dialect.POSTGRES)
    events = stop.run(sql_file("rows 1;\n\\connect other\nrows 3;"), mode="statements", on_error="continue")
    assert (outcome(events).outcome, error(events).code, stop.conn.runs) == ("stopped", "psql_meta_command", ["rows 1"])
    assert error(events).message == "Line 2 is a psql command (\\connect): run this file with psql"


def test_copy_from_stdin_stops_even_a_run_that_skips_errors(env, sql_file):
    e = env(dialect=Dialect.POSTGRES)
    events = e.run(sql_file("rows 1;\ncopy t FROM stdin;\n1\tx\n\\.\nrows 2;"), mode="statements",
                   on_error="continue")
    assert (outcome(events).outcome, error(events).code) == ("stopped", "unsupported_copy")
    assert error(events).message.endswith(COPY_HINT)


def test_a_line_that_is_not_utf8_stops_the_run_there(env, sql_file):
    e = env(dialect=Dialect.MYSQL)
    events = e.run(sql_file(b"rows 1;\nrows '\xff';\n"), mode="statements")
    done = outcome(events)
    assert (done.outcome, done.stopped_at.line, error(events).code) == ("stopped", 2, "file_not_utf8")
    assert error(events).message == f"Line 2 is not UTF-8 text ({HEX_BLOB_HINT})"


def test_go_n_sends_a_batch_n_times_and_go_0_never(env, sql_file):
    e = env(dialect=Dialect.TSQL)
    events = e.run(sql_file("rows 1\nGO 3\nrows 2\nGO 0\nrows 3\nGO\n"), mode="statements")
    # GO 0's batch is neither sent nor counted (pre-flight L7); a job session of SQL Server turns implicit
    # transactions off first (L8)
    assert (outcome(events).statements, e.conn.runs) == (
        2, ["SET IMPLICIT_TRANSACTIONS OFF"] + ["rows 1"] * 3 + ["rows 3"])


def test_a_read_only_connection_mid_run_stops_it(env, sql_file):
    def gate(sql: str) -> GuardDecision:
        action = GuardAction.BLOCK if sql == "rows 2" else GuardAction.RUN
        return GuardDecision(action=action, classification=Classification(kind=StatementKind.WRITE))

    e = env(gate=gate)
    events = e.run(sql_file("rows 1;\nrows 2;"), mode="statements", on_error="continue")
    assert (outcome(events).outcome, error(events).code) == ("stopped", "read_only")
    assert e.conn.runs == ["rows 1"]


@pytest.mark.parametrize(("setup", "fields", "code"), [
    ({}, {"path": "/nowhere/x.sql"}, "file_not_found"),
    ({"read_only": True}, {}, "read_only"),
    ({"transactions": False}, {}, "not_supported"),
    ({}, {"search_path": "public"}, "unsupported_option"),  # SQLite: no search path
])
def test_what_refuses_a_run_before_anything_is_sent(env, sql_file, setup, fields, code):
    e = env(**setup)
    path = fields.pop("path", sql_file("rows 1;"))
    events = e.run(path, **fields)
    assert (outcome(events).outcome, error(events).code, e.conn.runs) == ("not_started", code, [])
    assert e.records == []  # never started: no History record


def test_a_transaction_the_file_leaves_open_is_rolled_back_and_said(env, sql_file):
    e = env()
    done = outcome(e.run(sql_file("rows 1;\ntx;\nrows 3;"), mode="statements"))
    assert (done.outcome, done.statements) == ("completed", 1) and e.conn.rollbacks == 1  # pre-flight H2
    assert done.warnings == ["The file's transaction from line 2 (statement 2) was still open: it was rolled back, so "
                             "the statements from there on are not applied; run again from statement 2"]


def test_the_search_path_goes_first(env, sql_file):
    e = env(dialect=Dialect.POSTGRES)
    e.run(sql_file("rows 1;"), search_path="Sales Data")
    assert e.conn.runs == ['SET search_path TO "Sales Data"', "BEGIN", "rows 1", "COMMIT"]


def test_the_history_summary():
    command = RunFileCommand(request_id="r", path="/x/a.sql", mode="statements", on_error="continue",
                             from_statement=12)
    assert summary(command, 155_400_000) == (
        "-- Run SQL file /x/a.sql (148.2 MB), statement by statement, skipping errors, from statement 12")


# ---- pre-flight findings (06a ruling J1) ----------------------------------------------------------------------


@pytest.mark.parametrize(("statement", "sent"), [
    ("CREATE TABLE u (a int)", False), ("DROP TABLE IF EXISTS u", False), ("LOCK TABLES u WRITE", False),
    ("/*!40000 ALTER TABLE u DISABLE KEYS */", False), ("CREATE TEMPORARY TABLE u (a int)", True),
])
def test_mysql_all_or_nothing_refuses_what_commits_before_sending_it(env, sql_file, statement, sent):  # H1
    e = env(dialect=Dialect.MYSQL)
    events = e.run(sql_file(f"rows 1;\n{statement};\nrows 3;"))
    if sent:
        assert outcome(events).outcome == "completed" and statement in e.conn.runs
        return
    done = outcome(events)
    assert (done.outcome, done.stopped_at.line, error(events).code) == ("rolled_back", 2, "implicit_commit")
    assert error(events).message.startswith("Line 2 (") and "commits MySQL's transaction" in error(events).message
    assert e.conn.runs == ["START TRANSACTION", "rows 1"] and e.conn.rollbacks == 1


@pytest.mark.parametrize(("statement", "dialect", "expected"), [
    ("failend", Dialect.SQLITE, "rolled_back"),  # the server rolled it all back (a deadlock, XACT_ABORT)
    ("failend COMMIT", Dialect.POSTGRES, "unknown"),  # it holds COMMIT: what ran before may be committed
    ("CALL p()", Dialect.MYSQL, "unknown"),  # a procedure: MySQL's error packet cannot say
    ("fail boom", Dialect.MYSQL, "rolled_back"),
])
def test_a_failure_that_may_have_committed_is_unknown_all_or_nothing(env, sql_file, statement, dialect, expected):  # H1
    e = env(dialect=dialect, acts={"CALL p()": "failend"})
    events = e.run(sql_file(f"rows 1;\n{statement};\nrows 3;"))
    done = outcome(events)
    assert (done.outcome, done.stopped_at.line) == (expected, 2)
    assert ("what ran before it may be applied" in error(events).message) == (expected == "unknown")
    assert not e.conn.tx and "rows 3" not in e.conn.runs


def test_a_lost_link_on_a_statement_that_may_commit_is_unknown_all_or_nothing(env, sql_file):  # H1, for the link
    e = env(dialect=Dialect.POSTGRES)
    done = outcome(e.run(sql_file("rows 1;\nlost COMMIT;\nrows 3;")))
    assert (done.outcome, done.stopped_at.statement) == ("unknown", 2)


@pytest.mark.parametrize(("dialect", "statement", "expected"), [
    (Dialect.TSQL, "endtx", "unknown"),  # H3: a T-SQL batch may have rolled it back
    (Dialect.POSTGRES, "endtx", "unknown"),
    (Dialect.MYSQL, "CALL p()", "unknown"),  # a procedure may have committed or rolled back
    (Dialect.MYSQL, "endtx", "partial"),  # only MySQL's commit around a statement is known to be a commit
])
def test_a_transaction_that_ends_outside_mysqls_commit_is_unknown(env, sql_file, dialect, statement, expected):
    e = env(dialect=dialect, acts={"CALL p()": "endtx"})
    text = f"rows 1\nGO\n{statement}\nGO\nrows 3\nGO\n" if dialect is Dialect.TSQL else f"rows 1;\n{statement};\nrows 3;"
    events = e.run(sql_file(text))
    done = outcome(events)
    assert (done.outcome, done.statements, error(events).code) == (expected, 2, "transaction_ended")
    why = "MySQL commits around DDL" if expected == "partial" else "a statement in the file committed or rolled it back"
    assert why in error(events).message


@pytest.mark.parametrize(("batch", "refused"), [
    ("BEGIN TRY\n  rows 1\nEND TRY\nBEGIN CATCH\n  IF @@TRANCOUNT > 0 ROLLBACK;\nEND CATCH", "ROLLBACK"),
    ("rows 1\nCOMMIT", "COMMIT"),
    ("rows 1\nBEGIN TRAN\nrows 2", "BEGIN TRAN"),
    ("CREATE PROCEDURE p AS\nBEGIN TRAN\nCOMMIT", None),  # defined now, run later
    ("SELECT [commit], 'ROLLBACK' FROM t", None),
])
def test_a_tsql_batch_holding_a_transaction_statement_is_never_sent_all_or_nothing(env, sql_file, batch, refused):
    e = env(dialect=Dialect.TSQL)
    events = e.run(sql_file(f"rows 1\nGO\n{batch}\nGO\n"))
    if refused is None:
        assert outcome(events).outcome == "completed" and batch in e.conn.runs
        return
    assert (outcome(events).outcome, error(events).code) == ("rolled_back", "transaction_in_file")
    assert error(events).message.startswith(f"The file controls its own transactions (line 3: {refused})")
    assert batch not in e.conn.runs and e.conn.rollbacks == 1


def test_a_stop_inside_the_files_own_transaction_rolls_it_back_and_counts_it_out(env, sql_file):  # H2
    e = env()
    events = e.run(sql_file("rows 1;\ntx;\nrows 3;\nfail boom;\nrows 5;"), mode="statements")
    done = outcome(events)
    assert (done.outcome, done.stopped_at.statement, done.statements, e.conn.rollbacks) == ("stopped", 4, 1, 1)
    assert done.warnings == ["The file's transaction from line 2 (statement 2) was still open: it was rolled back, so "
                             "the statements from there on are not applied; run again from statement 2"]


def test_skipping_an_error_that_aborted_the_files_transaction_stops(env, sql_file):  # H2 (PostgreSQL's 25P02)
    e = env()
    events = e.run(sql_file("tx;\nrows 2;\nfail boom;\nrows 4;\nCOMMIT;"), mode="statements", on_error="continue")
    done = outcome(events)
    assert (done.outcome, done.stopped_at.statement, done.statements, done.error_count) == ("stopped", 3, 0, 0)
    assert error(events).message == ("boom\nThis error aborted the file's own transaction: nothing after it can run "
                                     "until that transaction ends")
    assert "rows 4" not in e.conn.runs and e.conn.rollbacks == 1


def test_skipping_an_error_the_server_rolled_the_files_transaction_back_for(env, sql_file):  # H2
    e = env()
    done = outcome(e.run(sql_file("tx;\nrows 2;\nfailend;\nrows 4;"), mode="statements", on_error="continue"))
    assert (done.outcome, done.statements, done.error_count) == ("completed", 1, 1)  # only `rows 4` is applied
    assert done.warnings == ["The file's transaction from line 1 (statement 1) ended when line 3 failed: the server "
                             "rolled it back, so the statements from there on are not applied; run again from "
                             "statement 1"]


def test_a_lost_link_inside_the_files_transaction_says_what_the_server_undid(env, sql_file):  # H2
    e = env()
    done = outcome(e.run(sql_file("rows 1;\ntx;\nrows 3;\nlost;"), mode="statements"))
    assert (done.outcome, done.statements, done.stopped_at.statement) == ("unknown", 1, 4)
    assert done.warnings[0].startswith("The connection was lost inside the file's transaction from line 2")


def test_a_failed_tsql_batch_of_several_statements_may_have_applied_some(env, sql_file):  # statement by statement
    e = env(dialect=Dialect.TSQL, acts={"rows 1\nrows 2": "fail"})
    done = outcome(e.run(sql_file("rows 1\nrows 2\nGO\n"), mode="statements"))
    assert done.outcome == "stopped" and done.warnings == [
        "Line 1 runs several statements (a batch or a procedure), each taking effect on its own: those that ran "
        "before it stopped may be applied"]


def test_a_line_that_is_not_utf8_stops_after_the_statements_before_it(env, sql_file):  # M3
    e = env(dialect=Dialect.POSTGRES)
    done = outcome(e.run(sql_file(b"rows 1;\nrows 2;\nrows '\xff';\nrows 4;\n"), mode="statements"))
    assert (done.outcome, done.stopped_at.line, done.stopped_at.statement, done.statements) == ("stopped", 3, 3, 2)
    assert e.conn.runs == ["rows 1", "rows 2"]


def test_a_utf16_file_says_so_without_the_hex_blob_hint(env, sql_file):  # L5
    e = env(dialect=Dialect.MYSQL)
    events = e.run(sql_file("rows 1;\n".encode("utf-16")), mode="statements")
    assert (outcome(events).outcome, outcome(events).stopped_at.line, error(events).message) == (
        "stopped", 1, UTF16_MESSAGE)


class CancelAtFirstProgress(Recorder):
    """Cancels the job from inside the worker's first job_progress: deterministically while the run still skips."""

    worker: SessionWorker

    def __call__(self, event) -> None:
        super().__call__(event)
        if event.type == "job_progress" and not getattr(self, "cancelled", False):
            self.cancelled = True
            self.worker.cancel(event.request_id)


@pytest.mark.parametrize(("mode", "runs"), [("all_or_nothing", ["BEGIN IMMEDIATE"]), ("statements", [])])
def test_a_cancel_while_skipping_to_from_statement_stops_the_run(env, sql_file, mode, runs):  # M4
    recorder = CancelAtFirstProgress()
    e = env(recorder=recorder)
    recorder.worker = e.worker
    path = sql_file("".join(f"rows {i};\n" for i in range(1, 3001)))
    done = outcome(e.run(path, mode=mode, from_statement=3000))
    assert (done.outcome, done.cancelled, done.statements) == ("not_started", True, 0)
    assert e.conn.runs == runs and e.conn.rollbacks == (1 if runs else 0)


def test_a_statement_over_the_limit_stops_the_run_where_it_starts(env, sql_file, monkeypatch):  # M11
    monkeypatch.setattr(run_file, "MAX_STATEMENT_CHARS", 1_048_576)  # 256 MiB in the app
    e = env()
    events = e.run(sql_file("rows 1;\n\nrows '" + "x" * 1_500_000 + "';\nrows 3;"), mode="statements")
    done = outcome(events)
    assert (done.outcome, done.stopped_at.line, done.stopped_at.statement, error(events).code) == (
        "stopped", 3, 2, "statement_too_large")
    assert error(events).message == "The statement starting at line 3 is larger than 1 MiB"
    assert e.conn.runs == ["rows 1"]


def test_a_go_n_batch_failing_on_a_later_run_says_how_many_ran(env, sql_file):  # L7
    e = env(dialect=Dialect.TSQL)
    events = e.run(sql_file("again\nGO 3\n"), mode="statements")
    assert error(events).message == "duplicate (after 1 of 3 runs)" and outcome(events).statements == 0


def dbaguy_dump(trailer: bool) -> str:
    head = "\n".join(header_lines(engine=Engine.SQLITE, server="SQLite 3.53.4", scope="database main",
                                  created="2026-10-20T12:00:00+08:00", content="schema and data", snapshot="one",
                                  not_included=[], drop_first=False))
    return head + "\nrows 1;\n" + (complete_line(0, 0) + "\n" if trailer else "")


@pytest.mark.parametrize("trailer", [True, False])
def test_a_dbaguy_dump_cut_short_is_refused_all_or_nothing_and_said_otherwise(env, sql_file, trailer):  # M15
    path = sql_file(dbaguy_dump(trailer))
    whole = env()
    events = whole.run(path)
    if trailer:
        assert outcome(events).outcome == "completed" and outcome(events).warnings == []
        return
    assert (outcome(events).outcome, error(events).code, error(events).message) == (
        "not_started", "file_truncated", CUT_SHORT)
    assert whole.conn.runs == [] and whole.records == []
    each = env()
    done = outcome(each.run(path, mode="statements"))
    assert (done.outcome, done.warnings) == ("completed", [CUT_SHORT])


def test_a_connection_without_the_tool_is_refused_in_sections_words():  # L4
    class NoRun(SqliteAdapter):
        capabilities = AdapterCapabilities(row_editing=True)

    factory = SessionHooksFactory(AdapterRegistry([NoRun()]), None, None, None, None)  # build_job reads the registry
    profile = SqliteProfile(id="00000000-0000-0000-0000-000000000001", engine="sqlite", name="s", path="/x.db")
    with pytest.raises(StatementError) as refused:
        factory.build_job(RunFileCommand(request_id="r", path="/x.sql", mode="statements"), profile, None)
    assert (refused.value.code, refused.value.message) == ("not_supported", "SQLite connections cannot run SQL files")
