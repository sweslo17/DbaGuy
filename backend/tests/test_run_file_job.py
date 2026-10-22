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
from dbaguy_backend.domain.data_jobs import JobError
from dbaguy_backend.domain.execution import RunFileCommand
from dbaguy_backend.domain.profiles import Engine, SqliteProfile
from dbaguy_backend.services.safety import GuardAction, GuardDecision
from dbaguy_backend.sessions.hooks import SessionHooksFactory
from dbaguy_backend.sessions.jobs import run_file
from dbaguy_backend.sessions.jobs.context import JOB_ROLLBACK_FAILED, JobHooks
from dbaguy_backend.sessions.jobs.run_file import (
    COPY_HINT, HEX_BLOB_HINT, NON_TRANSACTIONAL, PARTLY_APPLIED, RunFileJob, summary,
)
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from dbaguy_backend.sql.dump_header import CUT_SHORT, complete_line, header_lines
from dbaguy_backend.sql.statement_stream import UTF16_MESSAGE
from tests.fake_db import FakeConnection, FakeLease, FakeResult
from tests.test_sessions import Recorder, request_events

LOST = StatementError("Connection to the server was lost", code="connection_lost")
FRAME = {Dialect.SQLITE: ("BEGIN IMMEDIATE", "COMMIT"), Dialect.POSTGRES: ("BEGIN", "COMMIT"),
         Dialect.MYSQL: ("START TRANSACTION", "COMMIT"), Dialect.TSQL: ("SET NOCOUNT OFF;\nBEGIN TRANSACTION",
                                                                        "COMMIT TRANSACTION")}


class RowsResult(FakeResult):
    """A result with the given rows (CURRENT_TRANSACTION_ID(), SHOW WARNINGS)."""

    def __init__(self, rows: list[list]) -> None:
        super().__init__(0)
        self._rows = rows

    def fetch(self, n: int) -> list[list]:
        taken, self._rows = self._rows[:n], self._rows[n:]
        return taken


class RunFake(FakeConnection):
    """The frame's BEGIN opens a transaction and its COMMIT ends it (or raises `commit_error`; `commit_keeps` leaves it
    open). Statement words: `rows N`, `block` (waits for a cancel), `fail MSG`, `lost` (the link dies), `endtx` (ends
    the transaction, as MySQL DDL does), `failend` (fails and ends the transaction: XACT_ABORT, MySQL's commit before
    an error), `deadlock` (MySQL's 1213: ends it), `trig` (SQL Server's 3609: a trigger ended it), `again` (fails from
    its second run on), `reopen` (commits and begins another: a new CURRENT_TRANSACTION_ID()), `slept` (waits for a
    cancel, then finishes anyway: MySQL's SLEEP() that swallowed the KILL), `copy` (the adapter's COPY refusal), `tx`
    (opens one), anything else: done. `acts` gives a whole text one of those words' behaviour (`CALL p()`). `kept`: a
    ROLLBACK leaves MySQL's warning 1196 (a MyISAM table). `non_transactional`: MySQL's database has a table that
    cannot roll back (the run asks with NON_TRANSACTIONAL, recorded in `runs`)."""

    def __init__(self, dialect: Dialect = Dialect.SQLITE, commit_error: StatementError | None = None,
                 commit_keeps: bool = False, acts: dict[str, str] | None = None, kept: bool = False,
                 non_transactional: bool = False) -> None:
        super().__init__()
        self.non_transactional = non_transactional
        self.begin, self.end = FRAME[dialect]
        self.commit_error = commit_error
        self.commit_keeps = commit_keeps
        self.acts = acts or {}
        self.kept = kept
        self.seen: dict[str, int] = {}
        self.txid = 100
        self.id_reads = 0  # CURRENT_TRANSACTION_ID() reads
        self.rolled_back_kept = False  # the last ROLLBACK left 1196 for SHOW WARNINGS

    def rollback(self) -> None:
        self.rolled_back_kept = self.kept and self.tx
        super().rollback()

    def statement(self, sql, options):
        self.seen[sql] = self.seen.get(sql, 0) + 1
        if sql == "SELECT CURRENT_TRANSACTION_ID()":
            self.id_reads += 1
            return RowsResult([[self.txid]])
        if sql == NON_TRANSACTIONAL:
            self.runs.append(sql)
            return RowsResult([[1 if self.non_transactional else 0]])
        if sql == "SHOW WARNINGS":
            self.runs.append(sql)
            kept = [["Warning", 1196, "Some non-transactional changed tables couldn't be rolled back"]]
            return RowsResult(kept if self.rolled_back_kept else [])
        if sql == self.begin:
            self.runs.append(sql)
            self.tx = True
            self.txid += 1
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
        if word in ("lost", "endtx", "failend", "deadlock", "trig", "tx", "reopen", "slept") or (
                word == "again" and self.seen[sql] > 1):
            self.runs.append(sql)
        if word == "lost":
            raise LOST
        if word == "endtx":
            self.tx = False
            return FakeResult(0)
        if word in ("failend", "deadlock", "trig"):
            self.tx = False
            self.rolled_back_kept = self.kept and word == "deadlock"  # the victim's own warnings (N5)
            code = {"failend": "40001", "deadlock": "1213", "trig": "3609"}[word]
            raise StatementError("the transaction ended", code=code)
        if word == "reopen":
            self.txid += 1
            return FakeResult(0)
        if word == "slept":
            self.blocked.set()
            assert self._unblock.wait(10), "slept was never cancelled"
            raise StatementCancelled(completed=True)
        if word == "again" and self.seen[sql] > 1:
            raise StatementError("duplicate", code="23505")
        if word == "copy":
            raise StatementError("COPY FROM STDIN / TO STDOUT is not supported in the console yet",
                                 code="unsupported_copy")
        if word == "tx":
            self.tx = True
            self.txid += 1
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
        made.append(Env(conn or RunFake(fields.get("dialect", Dialect.SQLITE), acts=fields.pop("acts", None),
                                        kept=fields.pop("kept", False),
                                        non_transactional=fields.pop("non_transactional", False)), **fields))
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


class StuckRollback(RunFake):
    """A ROLLBACK that fails and leaves the transaction open."""

    def rollback(self) -> None:
        raise StatementError("rollback refused", code="08006")


def test_a_run_whose_rollback_fails_says_so_live_and_in_its_outcome(env, sql_file):  # fix wave F3
    events = env(StuckRollback()).run(sql_file("rows 1;\nfail boom;\nrows 3;"))
    assert [e.text for e in events if e.type == "message"] == [JOB_ROLLBACK_FAILED]
    done = outcome(events)
    assert (done.outcome, done.warnings) == ("rolled_back", [JOB_ROLLBACK_FAILED])
    assert done.error == JobError(line=2, statement=2, code=error(events).code, message=error(events).message)


def test_a_transaction_the_file_ends_stops_an_all_or_nothing_run_as_partial(env, sql_file):  # MySQL DDL, M8
    e = env(dialect=Dialect.MYSQL)
    events = e.run(sql_file("rows 1;\nendtx;\nrows 3;"))
    done = outcome(events)
    assert (done.outcome, done.stopped_at.line, done.statements) == ("partial", 2, 2)
    assert error(events).code == "transaction_ended" and "MySQL commits around DDL" in error(events).message
    assert e.conn.runs == ["START TRANSACTION", "rows 1", "endtx", "SHOW WARNINGS"]  # the rollback's warnings (R3)


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


@pytest.mark.parametrize(("text", "hint"), [
    ("PRAGMA foreign_keys = OFF;\nPRAGMA defer_foreign_keys = ON;\nrows 1;", True),  # a DbaGuy dump's header
    ("rows 1;\nPRAGMA foreign_keys(0);\nrows 2;", True),
    ("PRAGMA foreign_keys = ON;\nrows 1;", False),
    ("rows 1;", False),
])
def test_a_sqlite_commit_refused_for_a_foreign_key_says_how_the_file_loads(env, sql_file, text, hint):  # F4
    refused = StatementError("FOREIGN KEY constraint failed", code="SQLITE_CONSTRAINT_FOREIGNKEY")
    events = env(RunFake(commit_error=refused)).run(sql_file(text))
    line = 1 if text.startswith("PRAGMA foreign_keys = OFF") else 2
    said = f"\n{run_file.FOREIGN_KEYS_OFF_HINT.format(line)}" if hint else ""
    assert (outcome(events).outcome, error(events).message) == (
        "rolled_back", f"FOREIGN KEY constraint failed\nThe transaction was rolled back: nothing was applied{said}")
    other = env(RunFake(commit_error=StatementError("UNIQUE constraint failed", code="SQLITE_CONSTRAINT_UNIQUE")))
    assert "foreign key" not in error(other.run(sql_file(text))).message  # only a foreign key's refusal


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
    assert done.warnings == [  # 06b L9: a run again ends the same way unless the file is fixed
        "The file ended with its transaction from line 2 (statement 2) still open (no COMMIT: is it cut short?): it was "
        "rolled back, so the statements from there on are not applied"]


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


@pytest.mark.parametrize(("statement", "code", "says"), [
    ("CREATE TABLE u (a int)", "implicit_commit", "Line 2 (CREATE) commits MySQL's transaction"),
    ("DROP TABLE IF EXISTS u", "implicit_commit", "Line 2 (DROP) commits"),
    ("LOCK TABLES u WRITE", "implicit_commit", "Line 2 (LOCK) commits"),
    ("/*!40000 ALTER TABLE u DISABLE KEYS */", "implicit_commit", "Line 2 (ALTER) commits"),
    ("START REPLICA", "implicit_commit", "Line 2 (START REPLICA) commits"),  # R1: 1227 after committing
    ("STOP SLAVE", "implicit_commit", "Line 2 (STOP SLAVE) commits"),
    ("SET PASSWORD FOR 'x'@'%' = 'y'", "implicit_commit", "Line 2 (SET PASSWORD) commits"),  # R1: 1044
    ("SET autocommit = 1", "implicit_commit", "Line 2 sets autocommit, and turning it on commits"),  # nit
    ("CALL p()", "procedure_call", "Line 2 calls a procedure or a prepared statement"),  # R7
    ("EXECUTE s", "procedure_call", "Line 2 calls a procedure or a prepared statement"),
    ("CREATE TEMPORARY TABLE u (a int)", None, None),
])
def test_mysql_all_or_nothing_refuses_what_commits_before_sending_it(env, sql_file, statement, code, says):  # H1
    e = env(dialect=Dialect.MYSQL)
    events = e.run(sql_file(f"rows 1;\n{statement};\nrows 3;"))
    if code is None:
        assert outcome(events).outcome == "completed" and statement in e.conn.runs
        return
    done = outcome(events)
    assert (done.outcome, done.stopped_at.line, error(events).code) == ("rolled_back", 2, code)
    assert error(events).message.startswith(says) and error(events).message.endswith("run it statement by statement")
    assert e.conn.runs == ["START TRANSACTION", "rows 1", "SHOW WARNINGS"] and e.conn.rollbacks == 1


@pytest.mark.parametrize(("statement", "dialect", "expected"), [
    ("failend", Dialect.SQLITE, "rolled_back"),  # the server rolled it all back (XACT_ABORT, a trigger's RAISE)
    ("failend COMMIT", Dialect.POSTGRES, "unknown"),  # it holds COMMIT: what ran before may be committed
    ("failend", Dialect.MYSQL, "unknown"),  # R1: MySQL commits before START REPLICA, SET PASSWORD … then fails
    ("deadlock", Dialect.MYSQL, "rolled_back"),  # R1: 1213 rolled back the whole transaction
    ("fail boom", Dialect.MYSQL, "rolled_back"),
    ("trig", Dialect.TSQL, "unknown"),  # R2: 3609, a trigger committed inside the statement
])
def test_a_failure_that_may_have_committed_is_unknown_all_or_nothing(env, sql_file, statement, dialect, expected):  # H1
    e = env(dialect=dialect)
    text = f"rows 1\nGO\n{statement}\nGO\nrows 3\nGO\n" if dialect is Dialect.TSQL else f"rows 1;\n{statement};\nrows 3;"
    events = e.run(sql_file(text))
    done = outcome(events)
    assert (done.outcome, done.stopped_at.statement) == (expected, 2)
    assert ("what ran before it may be applied" in error(events).message) == (expected == "unknown")
    assert not e.conn.tx and "rows 3" not in e.conn.runs


def test_a_lost_link_on_a_statement_that_may_commit_is_unknown_all_or_nothing(env, sql_file):  # H1, for the link
    e = env(dialect=Dialect.POSTGRES)
    done = outcome(e.run(sql_file("rows 1;\nlost COMMIT;\nrows 3;")))
    assert (done.outcome, done.stopped_at.statement) == ("unknown", 2)


@pytest.mark.parametrize(("dialect", "statement", "expected"), [
    (Dialect.TSQL, "endtx", "unknown"),  # H3: a T-SQL batch may have rolled it back
    (Dialect.POSTGRES, "endtx", "unknown"),
    (Dialect.MYSQL, "endtx", "partial"),  # only MySQL's commit around a statement is known to be a commit
])
def test_a_transaction_that_ends_outside_mysqls_commit_is_unknown(env, sql_file, dialect, statement, expected):
    e = env(dialect=dialect)
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
                             "the statements from there on are not applied"]  # where to run again: resume_at (L4)
    assert (done.resume_at.line, done.resume_at.statement) == (2, 2)


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
                             "rolled it back, so lines 1–3 are not applied; the statements after it ran on their own, "
                             "outside a transaction"]  # 06b H1: running again from statement 1 would repeat `rows 4`


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


# ---- fix round 1 (rulings R1–R9) ------------------------------------------------------------------------------


def test_a_trigger_that_commits_statement_by_statement_is_unknown_and_inside_the_files_transaction_said(env, sql_file):
    alone = env(dialect=Dialect.TSQL)  # R2: 3609 outside any transaction: the statement's own work may stand
    events = alone.run(sql_file("rows 1\nGO\ntrig\nGO\nrows 3\nGO\n"), mode="statements")
    assert (outcome(events).outcome, outcome(events).stopped_at.statement) == ("unknown", 2)
    assert error(events).message.endswith("A trigger ended the transaction inside line 3: what it did may be applied")
    inside = env(dialect=Dialect.TSQL)
    done = outcome(inside.run(sql_file("tx\nGO\nrows 2\nGO\ntrig\nGO\n"), mode="statements"))
    assert (done.outcome, done.statements) == ("unknown", 2)  # not counted out: it may have been committed
    assert done.warnings == ["Line 5 may have committed or rolled back the file's transaction from line 1 (statement "
                             "1): the statements from there on may or may not be applied"]


@pytest.mark.parametrize("mode", ["all_or_nothing", "statements"])
def test_a_rollback_that_left_a_tables_changes_is_unknown(env, sql_file, mode):  # R3 (MyISAM)
    e = env(dialect=Dialect.MYSQL, kept=True)
    text = "rows 1;\nfail boom;" if mode == "all_or_nothing" else "START TRANSACTION;\nrows 2;\nfail boom;"
    done = outcome(e.run(sql_file(text), mode=mode))
    assert done.outcome == "unknown" and done.warnings[-1] == (
        "Some tables cannot roll back (non-transactional): their changes stayed")
    assert e.conn.runs[-1] == ("SHOW WARNINGS" if mode == "all_or_nothing" else "UNLOCK TABLES")


def test_a_rollback_that_kept_nothing_stays_rolled_back(env, sql_file):  # R3: no 1196, no change
    e = env(dialect=Dialect.MYSQL)
    assert outcome(e.run(sql_file("rows 1;\nfail boom;"))).outcome == "rolled_back"


@pytest.mark.parametrize(("dialect", "text", "sent", "from_statement"), [
    (Dialect.POSTGRES, "SET search_path TO s;\nrows 1;\nSELECT pg_catalog.set_config('search_path', '', false);\n"
     "SET LOCAL lock_timeout = 0;\nrows 5;", ["SET search_path TO s",
                                              "SELECT pg_catalog.set_config('search_path', '', false)", "rows 5"], 5),
    (Dialect.MYSQL, "/*!40103 SET TIME_ZONE='+00:00' */;\nSET GLOBAL x = 1;\nUSE d;\nrows 4;\nrows 5;",
     ["/*!40103 SET TIME_ZONE='+00:00' */", "USE d", "rows 5"], 5),
    (Dialect.TSQL, "SET ANSI_NULLS ON\nGO\nSET NOCOUNT ON\nrows 2\nGO\nrows 3\nGO\n",
     ["SET IMPLICIT_TRANSACTIONS OFF", "SET ANSI_NULLS ON", "rows 3"], 3),
    (Dialect.SQLITE, "PRAGMA foreign_keys=OFF;\nPRAGMA journal_mode=WAL;\nrows 3;",
     ["PRAGMA foreign_keys=OFF", "rows 3"], 3),
])
def test_a_run_from_statement_n_still_sends_the_session_settings_before_it(env, sql_file, dialect, text, sent,
                                                                           from_statement):  # R4
    e = env(dialect=dialect)
    done = outcome(e.run(sql_file(text), mode="statements", from_statement=from_statement))
    assert (done.outcome, done.statements, e.conn.runs) == ("completed", 1, sent)
    settings = len(sent) - 1 - (dialect is Dialect.TSQL)
    assert done.warnings == [f"{settings} session settings before statement {from_statement} were run again (SET, "
                             "USE, PRAGMA); the other statements before it were skipped"]


def test_a_resent_setting_all_or_nothing_is_refused_like_the_file(env, sql_file):  # R4 with the nit's SET autocommit
    e = env(dialect=Dialect.MYSQL)
    events = e.run(sql_file("SET autocommit = 1;\nrows 2;"), from_statement=2)
    assert (outcome(events).outcome, error(events).code, e.conn.runs) == (
        "not_started", "implicit_commit", ["START TRANSACTION", "SHOW WARNINGS"])


@pytest.mark.parametrize(("dialect", "chain"), [(Dialect.POSTGRES, "COMMIT AND CHAIN"), (Dialect.MYSQL, "COMMIT")])
def test_a_commit_that_leaves_a_transaction_open_restarts_it_there(env, sql_file, dialect, chain):  # R5
    # MySQL: a COMMIT under completion_type=CHAIN leaves one open too (`commit_keeps`)
    e = env(RunFake(dialect, commit_keeps=dialect is Dialect.MYSQL), dialect=dialect)
    done = outcome(e.run(sql_file(f"tx;\nrows 2;\n{chain};\nrows 4;\nfail boom;"), mode="statements"))
    assert (done.outcome, done.statements, done.stopped_at.statement) == ("stopped", 3, 5)
    # the chained transaction is tracked from its first statement: a run again starts there, not at the COMMIT AND
    # CHAIN, which PostgreSQL refuses outside a transaction block (06b review M1)
    assert done.warnings == ["The file's transaction from line 4 (statement 4) was still open: it was rolled back, so "
                             "the statements from there on are not applied"]
    assert (done.resume_at.line, done.resume_at.statement) == (4, 4)


def test_a_do_block_that_fails_may_have_applied_part_of_its_work(env, sql_file):  # R6
    e = env(dialect=Dialect.POSTGRES, acts={"DO $$ BEGIN rows; COMMIT; END $$": "fail"})
    done = outcome(e.run(sql_file("DO $$ BEGIN rows; COMMIT; END $$;\n"), mode="statements"))
    assert done.outcome == "stopped" and done.warnings[0].startswith("Line 1 runs several statements")


RESTARTED = ("The transaction was ended and restarted inside the file (a trigger or a procedure): some changes may "
             "stand")


@pytest.mark.parametrize("statement", ["EXEC p", "INSERT INTO t VALUES (99)"])  # a procedure, a trigger
def test_a_commit_and_restart_inside_the_file_is_unknown_at_a_failure(env, sql_file, statement):  # R10
    acts = {statement: "reopen"}
    whole = env(dialect=Dialect.TSQL, acts=acts)
    done = outcome(whole.run(sql_file(f"rows 1\nGO\n{statement}\nGO\nfail boom\nGO\n")))
    assert (done.outcome, done.stopped_at.statement, done.warnings) == ("unknown", 3, [RESTARTED])
    each = env(dialect=Dialect.TSQL, acts=acts)  # inside the file's own BEGIN TRAN: not counted out
    done = outcome(each.run(sql_file(f"tx\nGO\nrows 2\nGO\n{statement}\nGO\nfail boom\nGO\n"), mode="statements"))
    assert (done.outcome, done.statements, done.warnings) == (
        "unknown", 3, ["The file's transaction from line 1 (statement 1) was still open: it was rolled back", RESTARTED])


def test_a_restart_on_a_run_that_completes_is_unknown(env, sql_file):  # N4: one more read, right before COMMIT
    e = env(dialect=Dialect.TSQL, acts={"EXEC p": "reopen"})
    script = "rows 1\nGO\n" + "EXEC p\nGO\n" * 5 + "rows 3\nGO\n"
    done = outcome(e.run(sql_file(script)))
    assert (done.outcome, done.warnings) == ("unknown", [RESTARTED])
    assert e.conn.id_reads == 2 and e.conn.commits == 1  # after the run's BEGIN and before its COMMIT: never per EXEC


def test_a_run_that_completes_in_one_transaction_is_completed(env, sql_file):  # N4: the same id
    e = env(dialect=Dialect.TSQL)
    done = outcome(e.run(sql_file("rows 1\nGO\n" + "EXEC p\nGO\n" * 5)))
    assert (done.outcome, done.warnings, e.conn.id_reads) == ("completed", [], 2)


def test_a_failure_with_the_same_transaction_stays_rolled_back(env, sql_file):  # R10: the ids agree
    e = env(dialect=Dialect.TSQL)
    done = outcome(e.run(sql_file("rows 1\nGO\nEXEC p\nGO\nfail boom\nGO\n")))
    assert (done.outcome, done.warnings) == ("rolled_back", [])


def test_a_statement_that_finished_before_its_cancel_counts_as_applied(env, sql_file):  # R8
    e = env()
    e.submit(sql_file("rows 1;\nslept;\nrows 3;"), mode="statements")
    assert e.conn.blocked.wait(5)
    e.worker.cancel("r")
    done = outcome(request_events(e.recorder, "r"))
    assert (done.outcome, done.cancelled, done.statements, done.stopped_at.statement) == ("stopped", True, 2, 3)
    assert "rows 3" not in e.conn.runs


def test_a_stopped_mysql_run_unlocks_its_tables_after_the_rollback(env, sql_file):  # R9
    e = env(dialect=Dialect.MYSQL)
    e.run(sql_file("START TRANSACTION;\nrows 2;\nfail boom;"), mode="statements")
    assert e.conn.runs[-2:] == ["SHOW WARNINGS", "UNLOCK TABLES"] and e.conn.rollbacks == 1


# ---- fix round 3 (rulings N1–N6) ------------------------------------------------------------------------------


def test_a_procedure_inside_the_files_transaction_makes_its_rollback_unknown(env, sql_file):  # N1
    e = env(dialect=Dialect.MYSQL)
    done = outcome(e.run(sql_file("tx;\nrows 2;\nCALL p();\nrows 4;\nfail boom;"), mode="statements"))
    assert (done.outcome, done.statements) == ("unknown", 4)  # not counted out: the CALL may have committed
    assert done.warnings == ["Line 3 may have committed or rolled back the file's transaction from line 1 (statement "
                             "1): the statements from there on may or may not be applied"]


def test_a_statement_that_began_the_transaction_inside_it_stays_counted(env, sql_file):  # N2
    e = env(dialect=Dialect.MYSQL, acts={"CALL p()": "tx"})  # a procedure doing work, then START TRANSACTION
    done = outcome(e.run(sql_file("CALL p();\nrows 2;\nfail boom;"), mode="statements"))
    assert (done.outcome, done.statements) == ("stopped", 1)
    assert done.warnings == [
        "The file's transaction from line 1 (statement 1) was still open: it was rolled back, so the statements after "
        "it are not applied",
        "Line 1 runs several statements and began the transaction inside them: what it did before that may be applied"]


def test_a_deadlock_victim_whose_table_cannot_roll_back_is_unknown(env, sql_file):  # N5
    e = env(dialect=Dialect.MYSQL, kept=True)
    done = outcome(e.run(sql_file("rows 1;\ndeadlock;")))
    assert (done.outcome, done.warnings) == ("unknown", [
        "Some tables cannot roll back (non-transactional): their changes stayed"])
    plain = env(dialect=Dialect.MYSQL)
    assert outcome(plain.run(sql_file("rows 1;\ndeadlock;"))).outcome == "rolled_back"


@pytest.mark.parametrize(("text", "fields", "dialect", "expected"), [
    ("rows 1;\nfail boom;\nrows 3;", {}, Dialect.SQLITE, ("stopped", (2, 2))),
    ("rows 1;\ntx;\nrows 3;\nfail boom;\nrows 5;", {}, Dialect.SQLITE, ("stopped", (2, 2))),  # its transaction undone
    ("rows 1;\ntx;\nrows 3;", {}, Dialect.SQLITE, ("completed", (2, 2))),  # left open: rolled back at the end
    # pre-flight H1: `rows 4` took effect after the server rolled the file's transaction back
    ("tx;\nrows 2;\nfailend;\nrows 4;", {"on_error": "continue"}, Dialect.SQLITE, ("completed", None)),
    ("tx;\nrows 2;\nfailend;", {"on_error": "continue"}, Dialect.SQLITE, ("completed", (1, 1))),  # nothing after it
    ("tx;\nrows 2;\ndeadlock;\nrows 4;\nCOMMIT;\nrows 6;", {"on_error": "continue"}, Dialect.MYSQL,
     ("completed", None)),
    ("tx;\nrows 2;\ndeadlock;\nrows 4;\nfail x;", {}, Dialect.MYSQL, ("stopped", (1, 1))),  # stopped at the deadlock
    ("tx;\nrows 2;\nCALL p();\nrows 4;\ndeadlock;", {}, Dialect.MYSQL, ("unknown", None)),  # N1, then a deadlock
    ("tx;\nrows 2;\nCALL p();\ndeadlock;", {"on_error": "continue"}, Dialect.MYSQL, ("unknown", None)),
    ("tx;\nrows 2;\nfail boom;\nrows 4;\nCOMMIT;", {"on_error": "continue"}, Dialect.SQLITE, ("stopped", (1, 1))),
    ("rows 1;\nlost;\nrows 3;", {"on_error": "continue"}, Dialect.SQLITE, ("unknown", (2, 2))),
    ("rows 1;\ntx;\nrows 3;\nlost;", {}, Dialect.SQLITE, ("unknown", (2, 2))),  # the server undid its transaction
    ("rows 1;\nfail boom;\nrows 3;\nfail again;", {"on_error": "continue"}, Dialect.SQLITE, ("completed", None)),
    ("tx;\nrows 2;\nCALL p();\nrows 4;\nfail boom;", {}, Dialect.MYSQL, ("unknown", None)),  # N1: not known
    ("rows 1\nrows 2\nGO\n", {}, Dialect.TSQL, ("stopped", None)),  # a batch that may have applied part of it
    ("rows 1;\nfail boom;", {"mode": "all_or_nothing"}, Dialect.SQLITE, ("rolled_back", None)),
    ("rows 1;", {"from_statement": 5}, Dialect.SQLITE, ("completed", None)),
])
def test_resume_at_is_where_a_run_again_starts_without_skipping_anything(env, sql_file, text, fields, dialect,
                                                                         expected):  # 06b B1
    acts = {"rows 1\nrows 2": "fail"} if dialect is Dialect.TSQL else None
    e = env(dialect=dialect, acts=acts)
    done = outcome(e.run(sql_file(text), **{"mode": "statements", **fields}))
    at = (done.resume_at.line, done.resume_at.statement) if done.resume_at is not None else None
    assert (done.outcome, at) == expected


def test_a_go_n_that_failed_midway_has_no_resume_point(env, sql_file):  # 06b B1, pre-flight H1
    e = env(dialect=Dialect.TSQL, acts={"INSERT INTO t DEFAULT VALUES": "again"})
    done = outcome(e.run(sql_file("INSERT INTO a VALUES (1)\nGO\nINSERT INTO t DEFAULT VALUES\nGO 3\n"),
                         mode="statements"))
    assert (done.outcome, done.stopped_at.statement, done.resume_at) == ("stopped", 2, None)


def test_a_transaction_begun_inside_a_statement_that_the_server_rolled_back_has_no_resume_point(env, sql_file):
    e = env(dialect=Dialect.MYSQL, acts={"CALL p()": "tx"})  # N2: a procedure doing work, then START TRANSACTION
    done = outcome(e.run(sql_file("CALL p();\nrows 2;\ndeadlock;"), mode="statements"))  # 06b B1
    assert (done.outcome, done.statements, done.resume_at) == ("stopped", 1, None)
    assert done.warnings == [
        "The file's transaction from line 1 (statement 1) ended when line 3 failed: the server rolled it back, so the "
        "statements after it are not applied",
        "Line 1 runs several statements and began the transaction inside them: what it did before that may be applied"]


class StateUnreadable(RunFake):
    """The transaction's state cannot be read once `rows 2` ran (SQL Server reads it with a round trip)."""

    def in_transaction(self) -> bool:
        if "rows 2" in self.runs:
            raise StatementError("the state could not be read", code="08S01")
        return super().in_transaction()


def test_a_stop_whose_transaction_state_cannot_be_read_has_no_resume_point(env, sql_file):  # 06b B1
    def gate(sql: str) -> GuardDecision:
        action = GuardAction.BLOCK if sql == "rows 3" else GuardAction.RUN
        return GuardDecision(action=action, classification=Classification(kind=StatementKind.WRITE))

    e = env(StateUnreadable(), gate=gate)  # rolled back at the stop: whether that undid `tx` and `rows 2` is not known
    done = outcome(e.run(sql_file("tx;\nrows 2;\nrows 3;"), mode="statements"))
    assert (done.outcome, done.stopped_at.statement, done.resume_at, e.conn.rollbacks) == ("stopped", 3, None, 1)


def test_a_cancelled_run_resumes_at_the_statement_it_did_not_finish(env, sql_file):  # 06b B1
    e = env()
    e.submit(sql_file("rows 1;\nblock;\nrows 3;"), mode="statements")
    assert e.conn.blocked.wait(5)
    e.worker.cancel("r")
    done = outcome(request_events(e.recorder, "r"))
    assert (done.outcome, done.cancelled, done.resume_at.statement, done.resume_at.line) == ("stopped", True, 2, 2)


# ---- 06b Task 2, fix round 1 (the live review's H1–M3, L4) ----------------------------------------------------


@pytest.mark.parametrize(("dialect", "text", "acts"), [
    # SQL Server: XACT_ABORT rolls the file's transaction back at line 5, then a batch applies two INSERTs in
    # autocommit and fails on its third (skipped): a run again from the BEGIN would repeat them (review H1)
    (Dialect.TSQL, "BEGIN TRANSACTION\nGO\nINSERT INTO t VALUES (1)\nGO\nINSERT INTO g VALUES (1)\nGO\n"
     "INSERT INTO t VALUES (2)\nINSERT INTO t VALUES (3)\nINSERT INTO g VALUES (2)\nGO\n",
     {"BEGIN TRANSACTION": "tx", "INSERT INTO g VALUES (1)": "failend",
      "INSERT INTO t VALUES (2)\nINSERT INTO t VALUES (3)\nINSERT INTO g VALUES (2)": "fail boom"}),
    (Dialect.MYSQL, "tx;\nrows 2;\ndeadlock;\nCALL p();\n", {"CALL p()": "fail boom"}),  # a skipped CALL
])
def test_a_skipped_statement_that_runs_several_after_a_rolled_back_transaction_has_no_resume_point(
        env, sql_file, dialect, text, acts):
    done = outcome(env(dialect=dialect, acts=acts).run(sql_file(text), mode="statements", on_error="continue"))
    assert (done.outcome, done.error_count, done.resume_at) == ("completed", 2, None)


def test_a_failing_mysql_call_inside_the_files_transaction_may_have_committed_it(env, sql_file):  # review H2, N1
    e = env(dialect=Dialect.MYSQL, acts={"CALL p()": "fail boom"})  # the CALL committed, began again, then failed
    done = outcome(e.run(sql_file("rows 1;\nSTART TRANSACTION;\nrows 3;\nCALL p();\nrows 5;\nCOMMIT;"),
                         mode="statements"))
    assert (done.outcome, done.stopped_at.statement, done.statements, done.resume_at) == ("unknown", 4, 3, None)
    assert done.warnings == ["Line 4 may have committed or rolled back the file's transaction from line 2 (statement "
                             "2): the statements from there on may or may not be applied"]


def test_a_go_n_that_failed_midway_inside_the_files_transaction_resumes_at_its_begin(env, sql_file):  # review L4
    e = env(dialect=Dialect.TSQL, acts={"BEGIN TRANSACTION": "tx", "INSERT INTO t DEFAULT VALUES": "again"})
    done = outcome(e.run(sql_file("BEGIN TRANSACTION\nGO\nINSERT INTO t DEFAULT VALUES\nGO 3\n"), mode="statements"))
    # its one run that stood went with the transaction's rollback
    assert (done.outcome, done.stopped_at.statement, done.statements, e.conn.rollbacks) == ("stopped", 2, 0, 1)
    assert (done.resume_at.line, done.resume_at.statement) == (1, 1)


WRITE = "INSERT INTO t VALUES (1), (2), (3)"


@pytest.mark.parametrize(("non_transactional", "on_error", "expected"), [
    (True, "stop", ("stopped", None)),  # MyISAM may keep the rows before the error: no place is known (review M2)
    (False, "stop", ("stopped", (2, 2))),
    (True, "continue", ("completed", None)),
    (False, "continue", ("completed", None)),  # read to its end, nothing undone
])
def test_a_mysql_write_that_fails_where_tables_cannot_roll_back_may_be_partly_applied(env, sql_file,
                                                                                      non_transactional, on_error,
                                                                                      expected):
    e = env(dialect=Dialect.MYSQL, acts={WRITE: "fail boom"}, non_transactional=non_transactional)
    done = outcome(e.run(sql_file(f"rows 1;\n{WRITE};\nrows 3;"), mode="statements", on_error=on_error))
    at = (done.resume_at.line, done.resume_at.statement) if done.resume_at is not None else None
    assert (done.outcome, at) == expected
    assert done.warnings == ([PARTLY_APPLIED.format(2)] if non_transactional else [])
    assert e.conn.runs.count(NON_TRANSACTIONAL) == 1


def test_which_mysql_failures_ask_whether_tables_can_roll_back(env, sql_file):  # review M2: once, again after DDL
    e = env(dialect=Dialect.MYSQL, acts={WRITE: "fail boom", "UPDATE t SET a = 1": "fail boom"})
    text = f"{WRITE};\nfail x;\nUPDATE t SET a = 1;\nCREATE TABLE m (a int) ENGINE=MyISAM;\n{WRITE};\n"
    done = outcome(e.run(sql_file(text), mode="statements", on_error="continue"))
    assert (done.outcome, done.error_count) == ("completed", 4)
    assert e.conn.runs.count(NON_TRANSACTIONAL) == 2  # the first failed write, and the first after the CREATE
    other = env(dialect=Dialect.POSTGRES, acts={WRITE: "fail boom"})
    other.run(sql_file(f"rows 1;\n{WRITE};"), mode="statements")
    assert NON_TRANSACTIONAL not in other.conn.runs  # MySQL only; and all or nothing has its rollback's 1196 (R3)
    whole = env(dialect=Dialect.MYSQL, acts={WRITE: "fail boom"}, non_transactional=True)
    whole.run(sql_file(f"rows 1;\n{WRITE};"))
    assert NON_TRANSACTIONAL not in whole.conn.runs
    inside = env(dialect=Dialect.MYSQL, acts={WRITE: "fail boom"}, non_transactional=True)
    done = outcome(inside.run(sql_file(f"START TRANSACTION;\n{WRITE};"), mode="statements"))
    assert NON_TRANSACTIONAL not in inside.conn.runs  # inside the file's transaction, the ROLLBACK's 1196 tells (R3)
    assert (done.outcome, done.resume_at.statement) == ("stopped", 1)


def test_a_cancelled_mysql_write_where_tables_cannot_roll_back_has_no_resume_point(env, sql_file):  # review M2
    e = env(dialect=Dialect.MYSQL, acts={WRITE: "block"}, non_transactional=True)
    e.submit(sql_file(f"rows 1;\n{WRITE};\nrows 3;"), mode="statements")
    assert e.conn.blocked.wait(5)
    e.worker.cancel("r")
    done = outcome(request_events(e.recorder, "r"))
    assert (done.outcome, done.cancelled, done.resume_at, done.warnings) == (
        "stopped", True, None, [PARTLY_APPLIED.format(2)])


@pytest.mark.parametrize(("mode", "from_line", "expected", "sent"), [
    ("statements", 3, ("completed", None), ["rows 3", "rows 4"]),  # statement 3 still starts at line 3
    ("statements", 2, ("not_started", "file_changed"), []),
    ("all_or_nothing", 2, ("not_started", "file_changed"), ["BEGIN IMMEDIATE"]),  # rolled back, nothing of the file
])
def test_a_run_again_from_a_line_is_refused_when_the_file_changed(env, sql_file, mode, from_line, expected, sent):
    e = env()  # review M3: Run again from line N sends that line with the statement's number
    events = e.run(sql_file("rows 1;\nrows 2;\nrows 3;\nrows 4;"), mode=mode, from_statement=3, from_line=from_line)
    done = outcome(events)
    assert (done.outcome, error(events).code if error(events) else None) == expected and e.conn.runs == sent
    if expected[0] == "not_started":
        assert error(events).message == "The file changed: statement 3 now starts at line 3"
    short = env()
    events = short.run(sql_file("rows 1;", name="short.sql"), mode="statements", from_statement=3, from_line=3)
    assert (outcome(events).outcome, error(events).message) == (
        "not_started", "The file changed: it has 1 statements now, fewer than 3")


def test_from_line_is_a_line_number():
    with pytest.raises(ValueError):
        RunFileCommand(request_id="r", path="/x.sql", mode="statements", from_statement=2, from_line=0)
