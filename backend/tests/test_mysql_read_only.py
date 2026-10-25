"""MySQL's shared read-only guard (spec ⑧ D-08-5, CR-08-11, CR-08-5): M3's function that switches a read-only session
to READ WRITE no longer opens a way to write, in a user's read-only tab or an agent's session, and no function can
blind the guard's own look (fix round 1); an agent's session refuses to go on with a lexer-unsafe sql_mode. The
functions and table are zz_08_t7_*, dropped by exact name (not the names Task 6's reader tests use: runners overlap,
and one's DROP must not meet the other's table)."""

import time
from uuid import UUID

import mysql.connector
import pytest
from loguru import logger

from dbaguy_backend.adapters.base import Endpoint, StatementCancelled, StatementNotSent
from dbaguy_backend.adapters.mysql import read_only
from dbaguy_backend.adapters.mysql.adapter import MysqlAdapter
from dbaguy_backend.adapters.mysql.read_only import (
    SESSION_CHANGED, SESSION_CHANGED_MESSAGE, ReadOnlyGuard, lexer_safe, unsafe_modes,
)
from dbaguy_backend.agent.reader import ReadRequest
from dbaguy_backend.domain.classification import Classification, StatementKind
from dbaguy_backend.domain.execution import ExecuteCommand, RollbackCommand, SetAutocommitCommand
from dbaguy_backend.services.agent_gate import AgentVerdict
from dbaguy_backend.sessions.worker import SessionWorker
from dbaguy_backend.sql.dialects import Dialect
from tests.fake_db import FakeLease
from tests.test_sessions import Recorder, finished

from .agent_support import container, share
from .support import (
    MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, make_mysql_profile, mysql_driver_kwargs, requires_mysql, scaled,
)

__all__ = ["container"]


def test_the_unsafe_modes_and_ansi_which_brings_one_back_are_left_out():
    mode = "REAL_AS_FLOAT,PIPES_AS_CONCAT,ANSI_QUOTES,IGNORE_SPACE,ONLY_FULL_GROUP_BY,ANSI,NO_BACKSLASH_ESCAPES"
    assert lexer_safe(mode) == "REAL_AS_FLOAT,PIPES_AS_CONCAT,IGNORE_SPACE,ONLY_FULL_GROUP_BY"
    assert unsafe_modes(mode) == ["ANSI_QUOTES", "ANSI", "NO_BACKSLASH_ESCAPES"]
    assert lexer_safe("STRICT_TRANS_TABLES") == "STRICT_TRANS_TABLES" and lexer_safe("") == ""


@pytest.fixture
def flipper():
    """zz_08_t7_rw(): a stored function that switches its session to READ WRITE (M3), zz_08_t7_wr(): one that writes (a
    statement a read-only classifier calls a read), zz_08_t7_blind(): one that also sets sql_select_limit = 0, which
    empties every plain SELECT the session sends afterwards (fix round 1), and the table they act on."""
    owner = mysql.connector.connect(**mysql_driver_kwargs(autocommit=True))
    cursor = owner.cursor()
    try:
        cursor.execute("DROP FUNCTION IF EXISTS zz_08_t7_rw")
        cursor.execute("DROP FUNCTION IF EXISTS zz_08_t7_wr")
        cursor.execute("DROP FUNCTION IF EXISTS zz_08_t7_blind")
        cursor.execute("DROP TABLE IF EXISTS zz_08_t7_victim")
        cursor.execute("CREATE TABLE zz_08_t7_victim (id INT)")
        cursor.execute("CREATE FUNCTION zz_08_t7_rw() RETURNS INT NO SQL BEGIN "
                       "SET SESSION TRANSACTION READ WRITE; RETURN 1; END")
        cursor.execute("CREATE FUNCTION zz_08_t7_wr() RETURNS INT MODIFIES SQL DATA BEGIN "
                       "INSERT INTO zz_08_t7_victim VALUES (9); RETURN 1; END")
        cursor.execute("CREATE FUNCTION zz_08_t7_blind() RETURNS INT NO SQL BEGIN "
                       "SET SESSION sql_select_limit = 0; SET SESSION TRANSACTION READ WRITE; RETURN 1; END")
        yield cursor
    finally:
        cursor.execute("DROP FUNCTION IF EXISTS zz_08_t7_rw")
        cursor.execute("DROP FUNCTION IF EXISTS zz_08_t7_wr")
        cursor.execute("DROP FUNCTION IF EXISTS zz_08_t7_blind")
        cursor.execute("DROP TABLE IF EXISTS zz_08_t7_victim")
        owner.close()


def _run(worker, recorder, rid, sql):
    worker.submit(ExecuteCommand(request_id=rid, sql=sql))
    recorder.wait_for(finished(rid), timeout=scaled(30))
    errors = recorder.of(rid, "error")
    return errors[0].code if errors else None


@requires_mysql
def test_a_read_only_tab_stays_read_only_after_a_function_switches_it(flipper):
    profile = make_mysql_profile(read_only=True)
    lease = FakeLease(MysqlAdapter().open(profile, Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None))
    worker = SessionWorker("zz_08 tab", lambda: lease)
    worker.start()
    recorder = Recorder()
    worker.set_emitter(recorder)
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        assert _run(worker, recorder, "flip", "SELECT zz_08_t7_rw()") is None
        assert _run(worker, recorder, "write", "INSERT INTO zz_08_t7_victim VALUES (1)") == "1792"  # M3d, closed
    finally:
        logger.remove(sink)
        worker.close()
    assert any("switched to READ WRITE; restored" in text for text in warnings)
    flipper.execute("SELECT COUNT(*) FROM zz_08_t7_victim")
    assert flipper.fetchall() == [(0,)]


@requires_mysql
def test_an_agent_session_stays_read_only_too(container, flipper, monkeypatch):
    allowed = AgentVerdict(classification=Classification(kind=StatementKind.READ))
    monkeypatch.setattr("dbaguy_backend.sessions.hooks.agent_read", lambda sql, dialect, purpose="query": allowed)
    pid = share(container, make_mysql_profile(name="zz_08 mysql"))
    profile = container.profiles.get(UUID(pid))
    read = container.agent_reader.read
    assert read(ReadRequest(profile=profile, dialect=Dialect.MYSQL, sql="SELECT zz_08_t7_rw()")).status == "ok"
    written = read(ReadRequest(profile=profile, dialect=Dialect.MYSQL, sql="INSERT INTO zz_08_t7_victim VALUES (1)"))
    assert written.error_code == "1792"
    flipper.execute("SELECT COUNT(*) FROM zz_08_t7_victim")
    assert flipper.fetchall() == [(0,)]


@requires_mysql
def test_an_agent_session_whose_sql_mode_changed_refuses_and_is_reset(container):
    pid = share(container, make_mysql_profile(name="zz_08 mysql"))
    profile = container.profiles.get(UUID(pid))
    request = ReadRequest(profile=profile, dialect=Dialect.MYSQL, sql="SELECT 1")
    assert container.agent_reader.read(request).rows == [[1]]
    with container.agent_sessions.lease(profile, None, deadline=time.monotonic() + 10) as lease:  # as H2's change
        lease.worker.call(lambda c: c._conn.cmd_query("SET SESSION sql_mode = 'ANSI_QUOTES'"))
    refused = container.agent_reader.read(request)
    assert (refused.status, refused.error_code, refused.reset) == ("error", "session_changed", True)
    assert refused.error_message.startswith("The connection's sql_mode changed")
    assert container.agent_reader.read(request).rows == [[1]]  # a new session, with a clean sql_mode


def _victims(cursor) -> int:
    cursor.execute("SELECT COUNT(*) FROM zz_08_t7_victim")
    return cursor.fetchall()[0][0]


def _tab(read_only_profile: bool = True):
    """A user's tab on the dev MySQL: a worker over a connection the adapter opened (no safety hooks, so what stops a
    write is the server)."""
    profile = make_mysql_profile(read_only=read_only_profile)
    lease = FakeLease(MysqlAdapter().open(profile, Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None))
    worker = SessionWorker("zz_08 tab", lambda: lease)
    worker.start()
    recorder = Recorder()
    worker.set_emitter(recorder)
    return worker, recorder


@requires_mysql
@pytest.mark.parametrize("how", ["typed", "button"])
def test_a_function_cannot_make_a_read_only_tab_writable_by_a_rollback(flipper, how):
    """The hole the brief's own test cannot show (the INSERT after the flip is refused with or without the guard): the
    flip changes the session's default, and the access mode of the statements that follow is fixed only at the next
    COMMIT or ROLLBACK, typed or sent by the Rollback button (probed: flip; ROLLBACK; a function that writes writes a
    row). The guard puts READ ONLY back before the ROLLBACK statement runs, and before the first statement after the
    button's."""
    worker, recorder = _tab()
    try:
        assert _run(worker, recorder, "flip", "SELECT zz_08_t7_rw()") is None
        if how == "typed":
            assert _run(worker, recorder, "end", "ROLLBACK") is None
        else:
            worker.submit(RollbackCommand(request_id="end"))
            recorder.wait_for(finished("end"), timeout=scaled(30))
        assert _run(worker, recorder, "write", "SELECT zz_08_t7_wr()") == "1792"
    finally:
        worker.close()
    assert _victims(flipper) == 0


@requires_mysql
def test_a_manual_commit_tab_that_flipped_inside_its_transaction_is_read_only_after_the_commit(flipper):
    worker, recorder = _tab()
    try:
        worker.submit(SetAutocommitCommand(request_id="manual", enabled=False))
        recorder.wait_for(finished("manual"), timeout=scaled(30))
        # a statement that touches a table opens the transaction (no check inside it), then COMMIT resets the mode
        assert _run(worker, recorder, "flip", "SELECT zz_08_t7_rw(), (SELECT COUNT(*) FROM zz_08_t7_victim)") is None
        assert _run(worker, recorder, "commit", "COMMIT") is None
        assert _run(worker, recorder, "write", "SELECT zz_08_t7_wr()") == "1792"
    finally:
        worker.close()
    assert _victims(flipper) == 0


@requires_mysql
def test_a_read_only_tab_still_answers_show_warnings_and_show_count(flipper):
    """P11: the check's own SELECT would empty the diagnostics area a SHOW WARNINGS reports."""
    worker, recorder = _tab()
    try:
        assert _run(worker, recorder, "warn", "SELECT CAST('x' AS SIGNED)") is None
        assert _run(worker, recorder, "count", "SHOW COUNT(*) WARNINGS") is None
        assert _run(worker, recorder, "show", "SHOW WARNINGS") is None
    finally:
        worker.close()
    counts = recorder.of("count", "rows")
    shown = recorder.of("show", "rows")
    assert counts[0].rows == [["1"]] or counts[0].rows == [[1]]
    assert [row[1] for row in shown[0].rows] == [1292]  # Truncated incorrect INTEGER value


@requires_mysql
def test_a_writable_tab_is_left_alone(flipper):
    """The guard is for read-only sessions: a writable tab sends nothing extra and can write."""
    worker, recorder = _tab(read_only_profile=False)
    try:
        assert _run(worker, recorder, "w", "SELECT zz_08_t7_wr()") is None
    finally:
        worker.close()
    assert _victims(flipper) == 1
    flipper.execute("DELETE FROM zz_08_t7_victim")


@requires_mysql
def test_an_agent_session_has_read_only_back_for_the_next_call(container, flipper):
    """Task 6 review M-4: a function's SET SESSION TRANSACTION READ WRITE persisted into the agent's later calls (the
    frame's explicit START TRANSACTION READ ONLY still held)."""
    pid = share(container, make_mysql_profile(name="zz_08 mysql"))
    profile = container.profiles.get(UUID(pid))
    read = container.agent_reader.read
    assert read(ReadRequest(profile=profile, dialect=Dialect.MYSQL, sql="SELECT zz_08_t7_rw()")).status == "ok"
    asked = read(ReadRequest(profile=profile, dialect=Dialect.MYSQL, sql="SELECT @@SESSION.transaction_read_only"))
    assert asked.rows == [[1]]


@requires_mysql
def test_a_tab_with_a_lexer_unsafe_sql_mode_is_not_refused():
    """CR-08-5 is for agents: a user's tab keeps working under the sql_mode its profile asks for (the lexer assumption
    is a documented limitation there)."""
    profile = make_mysql_profile(read_only=True, options={"sql_mode": "ANSI_QUOTES"})
    conn = MysqlAdapter().open(profile, Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None)
    try:
        result = conn.run("SELECT @@SESSION.sql_mode")
        assert result.fetch(1) == [["ANSI_QUOTES"]]
        result.close()
    finally:
        conn.close()


@requires_mysql
def test_an_agent_connection_refuses_a_statement_while_its_sql_mode_is_unsafe_and_sends_nothing():
    """The same refusal one level down, with no reader: StatementNotSent `session_changed`, nothing sent (the next
    statement, with the mode put right, is the first thing the server hears)."""
    profile = make_mysql_profile(read_only=True)
    conn = MysqlAdapter().open(profile, Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None, agent=True)
    try:
        conn._conn.cmd_query("SET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES'")
        with pytest.raises(StatementNotSent) as refused:
            conn.run("SELECT 1")
        assert (refused.value.code, refused.value.message) == (SESSION_CHANGED, SESSION_CHANGED_MESSAGE)
        conn._conn.cmd_query("SET SESSION sql_mode = ''")
        result = conn.run("SELECT 2")
        assert result.fetch(1) == [[2]]
        result.close()
    finally:
        conn.close()


@requires_mysql
def test_a_guard_that_cannot_look_is_a_statement_not_sent():
    """The check's own failure (here the thread was killed) is the statement not sent, with the usual code."""
    profile = make_mysql_profile(read_only=True)
    conn = MysqlAdapter().open(profile, Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None)
    killer = mysql.connector.connect(**mysql_driver_kwargs(autocommit=True))
    try:
        killer.cmd_query(f"KILL {int(conn._thread_id)}")  # the thread this test opened, no other
        with pytest.raises(StatementNotSent) as refused:
            conn.run("SELECT 1")
        assert refused.value.code == "connection_lost"
    finally:
        killer.close()
        conn.close()


@requires_mysql
def test_a_cancel_kept_while_armed_still_means_nothing_is_sent_after_the_look():
    """The guard's round trip is inside the armed window: a cancel in it is kept (no KILL), and run() then stops."""
    profile = make_mysql_profile(read_only=True)
    conn = MysqlAdapter().open(profile, Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None)
    try:
        conn.arm_cancel()
        conn.cancel()
        with pytest.raises(StatementCancelled):
            conn.run("SELECT 1")
        result = conn.run("SELECT 3")  # nothing was killed: the session goes on
        assert result.fetch(1) == [[3]]
        result.close()
    finally:
        conn.close()


@requires_mysql
def test_the_adapter_tells_the_guard_whether_a_transaction_is_open():
    """Inside a transaction (its access mode was fixed at its start, an agent's START TRANSACTION READ ONLY holds,
    M3e) there is no look; the flag the guard gets is the protocol's, with no round trip of its own."""
    profile = make_mysql_profile(read_only=True)
    conn = MysqlAdapter().open(profile, Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None)
    seen: list[bool] = []
    original = conn._guard.before_statement

    def spy(sql, in_transaction):
        seen.append(in_transaction)
        original(sql, in_transaction)

    conn._guard.before_statement = spy
    try:
        for sql in ("START TRANSACTION READ ONLY", "SELECT 1", "ROLLBACK", "SELECT 2"):
            conn.run(sql).close()
    finally:
        conn.close()
    assert seen == [False, True, True, False]


@requires_mysql
@pytest.mark.parametrize("how", ["typed", "button"])
def test_a_function_that_also_empties_every_select_cannot_blind_a_read_only_tab(flipper, how):
    """Fix round 1 (review, Critical): the function flips READ WRITE and sets sql_select_limit = 0 in the same breath. A
    plain probe SELECT then returned no row, the look read that as "nothing to put back", and the tab stayed blind for
    the rest of the connection: the next ROLLBACK or COMMIT, then a plain INSERT, wrote."""
    worker, recorder = _tab()
    try:
        assert _run(worker, recorder, "flip", "SELECT zz_08_t7_blind()") is None
        if how == "typed":
            assert _run(worker, recorder, "end", "ROLLBACK") is None
        else:
            worker.submit(RollbackCommand(request_id="end"))
            recorder.wait_for(finished("end"), timeout=scaled(30))
        assert _run(worker, recorder, "write", "INSERT INTO zz_08_t7_victim VALUES (1)") == "1792"
        assert _run(worker, recorder, "commit", "COMMIT") is None  # and a second boundary: the blindness lasted
        assert _run(worker, recorder, "again", "INSERT INTO zz_08_t7_victim VALUES (2)") == "1792"
    finally:
        worker.close()
    assert _victims(flipper) == 0


@requires_mysql
def test_an_agent_session_has_read_only_back_after_a_function_that_also_empties_every_select(container, flipper):
    """The agent's next frame resets sql_select_limit and looks again, so it healed by itself; the session is right
    straight after the call too (the look before RELEASE_ALL_LOCKS used to see an empty answer), and the next call has
    its rows."""
    pid = share(container, make_mysql_profile(name="zz_08 mysql"))
    profile = container.profiles.get(UUID(pid))
    read = container.agent_reader.read
    assert read(ReadRequest(profile=profile, dialect=Dialect.MYSQL, sql="SELECT zz_08_t7_blind()")).status == "ok"
    with container.agent_sessions.lease(profile, None, deadline=time.monotonic() + 10) as lease:
        state = lease.worker.call(
            lambda c: read_only.catalog.query(c._conn, "SELECT @@SESSION.transaction_read_only LIMIT 1"))
    assert state == [(1,)]
    three = "SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3"
    assert read(ReadRequest(profile=profile, dialect=Dialect.MYSQL, sql=three)).rows == [[1], [2], [3]]


@requires_mysql
def test_an_explicit_limit_overrides_sql_select_limit_in_the_look():
    """Probed on 8.4: with sql_select_limit = 0 a plain SELECT of the session's state returns no row and the same
    SELECT with LIMIT 1 returns it, in the forms 8.4 knows (the documented precedence)."""
    conn = mysql.connector.connect(**mysql_driver_kwargs(autocommit=True))
    try:
        conn.cmd_query("SET SESSION sql_select_limit = 0")
        assert read_only.catalog.query(conn, "SELECT @@SESSION.transaction_read_only") == []
        for probe in (read_only._STATES[0], read_only._STATES[2]):  # tx_read_only is not a variable of 8.4
            rows = read_only.catalog.query(conn, probe)
            assert len(rows) == 1 and probe.endswith("LIMIT 1")
    finally:
        conn.close()


@requires_mysql
@pytest.mark.parametrize("charset", ["ucs2", "utf32", "utf16le", "binary", "latin1"])
def test_the_look_reads_the_same_state_whatever_the_session_sends_its_results_in(charset):
    """A function can set character_set_results: with ucs2, utf32 or utf16le the sql_mode text arrived with a NUL
    between its letters, so an agent's NO_BACKSLASH_ESCAPES was not found (CR-08-5 fooled)."""
    profile = make_mysql_profile(read_only=True)
    conn = MysqlAdapter().open(profile, Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None, agent=True)
    try:
        conn._conn.cmd_query(f"SET SESSION character_set_results = {charset}")
        conn._conn.cmd_query("SET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES'")
        with pytest.raises(StatementNotSent) as refused:
            conn.run("SELECT 1")
        assert (refused.value.code, refused.value.message) == (SESSION_CHANGED, SESSION_CHANGED_MESSAGE)
    finally:
        conn.close()


@requires_mysql
def test_a_one_millisecond_max_execution_time_does_not_stop_the_look():
    """Probed with the other settings a function can leave (fix round 1): a 1 ms limit does not stop a SELECT of
    constants, so the look and the statement still run."""
    profile = make_mysql_profile(read_only=True)
    conn = MysqlAdapter().open(profile, Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None)
    try:
        conn._conn.cmd_query("SET SESSION max_execution_time = 1")
        result = conn.run("SELECT 1")
        assert result.fetch(1) == [[1]]
        result.close()
    finally:
        conn.close()


# ---- the guard without a server ----


class _Conn:
    """What the guard sends to the connection: only cmd_query."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def cmd_query(self, sql: str) -> None:
        self.sent.append(sql)


class _State:
    """read_only.catalog.query, answering from a script: a row (one-row list), a list of rows, or an exception."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, conn, sql, params=()):
        self.asked.append(sql)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, list) else [answer]


def _guard(monkeypatch, *answers, agent=False) -> tuple[ReadOnlyGuard, _Conn, _State]:
    state = _State(*answers)
    monkeypatch.setattr(read_only.catalog, "query", state)
    conn = _Conn()
    return ReadOnlyGuard(conn, 7, agent=agent), conn, state


def test_the_guard_puts_read_only_back_with_a_warning_and_leaves_a_read_only_session_alone(monkeypatch):
    guard, conn, state = _guard(monkeypatch, (0, "STRICT_TRANS_TABLES"))
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        guard.before_statement("SELECT 1", in_transaction=False)
    finally:
        logger.remove(sink)
    assert conn.sent == ["SET SESSION TRANSACTION READ ONLY"] and len(state.asked) == 1
    assert warnings == ["MySQL thread 7: a read-only session was switched to READ WRITE; restored"]
    guard, conn, state = _guard(monkeypatch, (1, "STRICT_TRANS_TABLES"))
    guard.before_statement("SELECT 1", in_transaction=False)
    assert conn.sent == [] and len(state.asked) == 1


def test_the_guard_does_not_look_inside_a_transaction_or_before_a_diagnostic_statement(monkeypatch):
    guard, conn, state = _guard(monkeypatch, (0, ""))
    guard.before_statement("INSERT INTO t VALUES (1)", in_transaction=True)
    for sql in ("SHOW WARNINGS", "show   errors", "SHOW COUNT(*) WARNINGS", "SHOW COUNT(*) ERRORS LIMIT 1",
                "/* why */ SHOW WARNINGS LIMIT 5", "-- x\nSHOW WARNINGS", "GET DIAGNOSTICS @n = NUMBER",
                "GET CURRENT DIAGNOSTICS @n = NUMBER", "get stacked diagnostics @n = number"):
        guard.before_statement(sql, in_transaction=False)
    assert state.asked == [] and conn.sent == []


@pytest.mark.parametrize("sql", [
    "SELECT 1", "SHOW TABLES", "SHOW WARNINGS_X", "SHOW", "SHOW COUNTS", "GET LOCK", "/*!50000 SHOW WARNINGS */",
    "(SELECT 1)", "", "   ", "-- only a comment", "DO 0", "SHOW\nWARNINGS_TABLE",
])
def test_everything_else_is_looked_at(monkeypatch, sql):
    guard, conn, state = _guard(monkeypatch, (1, ""))
    guard.before_statement(sql, in_transaction=False)
    assert len(state.asked) == 1


def test_an_agents_guard_refuses_each_lexer_unsafe_mode_before_anything_else(monkeypatch):
    for mode in ("NO_BACKSLASH_ESCAPES", "ANSI_QUOTES", "ANSI", "STRICT_TRANS_TABLES,ansi_quotes"):
        guard, conn, _ = _guard(monkeypatch, (0, mode), agent=True)
        with pytest.raises(StatementNotSent) as refused:
            guard.before_statement("SELECT 1", in_transaction=False)
        assert (refused.value.code, refused.value.message) == (SESSION_CHANGED, SESSION_CHANGED_MESSAGE)
        assert conn.sent == []  # nothing: not even the READ ONLY restore, the session is about to be closed
    guard, conn, _ = _guard(monkeypatch, (0, "STRICT_TRANS_TABLES,IGNORE_SPACE"), agent=True)
    guard.before_statement("SELECT 1", in_transaction=False)  # a safe mode: only the access mode is put back
    assert conn.sent == ["SET SESSION TRANSACTION READ ONLY"]
    guard, conn, _ = _guard(monkeypatch, (1, "NO_BACKSLASH_ESCAPES"), agent=False)  # a tab: no refusal
    guard.before_statement("SELECT 1", in_transaction=False)
    assert conn.sent == []


def _unknown_variable() -> mysql.connector.Error:
    return mysql.connector.errors.ProgrammingError(msg="Unknown system variable", errno=1193)


def test_an_older_server_is_asked_for_tx_read_only_and_a_server_without_either_for_sql_mode_alone(monkeypatch):
    guard, conn, state = _guard(monkeypatch, _unknown_variable(), (0, "STRICT_TRANS_TABLES"), (1, ""))
    guard.before_statement("SELECT 1", in_transaction=False)  # 5.7.8–5.7.19: the old name
    assert "transaction_read_only" in state.asked[0] and "tx_read_only" in state.asked[1]
    assert conn.sent == ["SET SESSION TRANSACTION READ ONLY"]
    guard.before_statement("SELECT 1", in_transaction=False)  # the name that worked is the one asked from then on
    assert len(state.asked) == 3 and "tx_read_only" in state.asked[2]
    assert conn.sent == ["SET SESSION TRANSACTION READ ONLY"]

    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        guard, conn, state = _guard(monkeypatch, _unknown_variable(), _unknown_variable(), (None, "STRICT"))
        guard.before_statement("SELECT 1", in_transaction=False)
        guard.before_statement("SELECT 1", in_transaction=False)
    finally:
        logger.remove(sink)
    assert conn.sent == [] and len(state.asked) == 4  # unknown twice, then the sql_mode-only form, twice
    assert len([text for text in warnings if "not re-checked" in text]) == 1  # said once
    guard, conn, _ = _guard(monkeypatch, _unknown_variable(), _unknown_variable(), (None, "ANSI_QUOTES"), agent=True)
    with pytest.raises(StatementNotSent):  # an agent's sql_mode is checked wherever the access mode cannot be
        guard.before_statement("SELECT 1", in_transaction=False)


def test_an_error_other_than_an_unknown_variable_is_not_swallowed(monkeypatch):
    lost = mysql.connector.errors.OperationalError(msg="gone", errno=2013)
    guard, conn, _ = _guard(monkeypatch, lost)
    with pytest.raises(mysql.connector.Error):
        guard.before_statement("SELECT 1", in_transaction=False)
    assert conn.sent == []


UNREADABLE = "The session's read-only state could not be read: reconnect"


@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize("answer", [
    [],  # no row: what a function that empties every SELECT used to produce
    [(1, "STRICT_TRANS_TABLES"), (1, "STRICT_TRANS_TABLES")],  # more than one
    [("x", "STRICT_TRANS_TABLES")],  # a flag that is not 0 or 1
    [(2, "STRICT_TRANS_TABLES")],
    [(None, "STRICT_TRANS_TABLES")],  # transaction_read_only is never NULL
    [(1, None)],  # sql_mode is never NULL
    [(1, "\x00S\x00T\x00R\x00I\x00C\x00T")],  # text that is not a mode list (a character set it was not asked in)
    [(1,)],  # not two values
], ids=["no row", "two rows", "flag x", "flag 2", "flag NULL", "mode NULL", "mode text", "one value"])
def test_a_look_that_gets_anything_but_one_readable_row_fails_closed(monkeypatch, agent, answer):
    guard, conn, state = _guard(monkeypatch, answer, agent=agent)
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        with pytest.raises(StatementNotSent) as refused:
            guard.before_statement("SELECT 1", in_transaction=False)
    finally:
        logger.remove(sink)
    assert refused.value.message == UNREADABLE
    assert conn.sent == [] and len(state.asked) == 1
    assert len(warnings) == 1 and "STRICT" not in warnings[0]  # said once, with no value in it


def test_the_sql_mode_alone_form_fails_closed_too(monkeypatch):
    """The fallback's last step (a server with neither variable) has no access mode to read, and still needs its row."""
    for answer in ([], [(None, "A"), (None, "B")], [(None, None)], [(1, "STRICT_TRANS_TABLES")]):
        guard, conn, _ = _guard(monkeypatch, _unknown_variable(), _unknown_variable(), answer)
        with pytest.raises(StatementNotSent) as refused:
            guard.before_statement("SELECT 1", in_transaction=False)
        assert refused.value.message == UNREADABLE and conn.sent == []


def test_every_probe_has_an_explicit_limit_and_reads_the_mode_in_a_form_no_charset_changes(monkeypatch):
    for probe in read_only._STATES:
        assert probe.endswith(" LIMIT 1") and "CAST(@@SESSION.sql_mode AS BINARY)" in probe


@requires_mysql
def test_the_auto_commit_read_is_not_emptied_by_sql_select_limit():
    """T7 re-review: the adapter's other plain-SELECT state reads have LIMIT 1 too; with sql_select_limit = 0 the
    auto-commit read came back empty and the cached mode stayed the old one."""
    profile = make_mysql_profile()
    conn = MysqlAdapter().open(profile, Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None)
    try:
        for sql in ("SET SESSION sql_select_limit = 0", "SET autocommit = 0"):
            conn.run(sql).close()
        assert conn.autocommit() is False
    finally:
        conn.close()


def test_the_agent_sql_mode_read_has_an_explicit_limit(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(read_only.catalog, "first_row", lambda conn, sql: sent.append(sql) or ("",))
    read_only.make_agent_sql_mode(object())
    assert sent and all(sql.endswith(" LIMIT 1") for sql in sent)


@requires_mysql
def test_completion_type_and_the_gipk_display_a_function_sets_are_set_back(container):
    """T6 re-review 2 N-2 and observation 4: a function's `completion_type = CHAIN` and
    `show_gipk_in_create_table_and_information_schema = OFF` outlived the ROLLBACK (the second silently hides a
    generated invisible primary key from information_schema.COLUMNS on the next call)."""
    pid = share(container, make_mysql_profile(name="zz_08_ff mysql"))
    state = ("SELECT @@SESSION.completion_type, @@SESSION.show_gipk_in_create_table_and_information_schema")
    root = mysql.connector.connect(**mysql_driver_kwargs(user="root", autocommit=True))
    cursor = root.cursor()
    try:
        cursor.execute("DROP FUNCTION IF EXISTS dbaguy.zz_08_ff_chain")
        cursor.execute("CREATE FUNCTION dbaguy.zz_08_ff_chain() RETURNS INT NO SQL BEGIN SET SESSION completion_type "
                       "= 'CHAIN', show_gipk_in_create_table_and_information_schema = OFF; RETURN 1; END")
        before = _read_agent(container, pid, state).rows
        assert _read_agent(container, pid, "SELECT dbaguy.zz_08_ff_chain()").status == "ok"
        assert _read_agent(container, pid, state).rows == before
    finally:
        cursor.execute("DROP FUNCTION IF EXISTS dbaguy.zz_08_ff_chain")
        root.close()


def _read_agent(container, pid: str, sql: str):
    return container.agent_reader.read(ReadRequest(profile=container.profiles.get(UUID(pid)), dialect=Dialect.MYSQL,
                                                   sql=sql))
