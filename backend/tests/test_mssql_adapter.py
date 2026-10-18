import json
import random
import threading
import time

import pytest

from dbaguy_backend.adapters.base import Endpoint, StatementCancelled, StatementError
from dbaguy_backend.adapters.mssql import adapter as mssql_adapter
from dbaguy_backend.adapters.mssql import cancel as mssql_adapter_cancel
from dbaguy_backend.adapters.mssql import catalog, driver
from dbaguy_backend.adapters.mssql.adapter import MssqlAdapter
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.values import encode_row
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.container import default_registry
from dbaguy_backend.domain.errors import ConnectionFailed
from dbaguy_backend.domain.metadata import PathSegment
from dbaguy_backend.domain.profiles import SshConfig, SslConfig
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.profile_service import ProfileService, ResolvedSecrets
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.formatting import value_condition
from dbaguy_backend.sql.select_builder import SortKey, build_browse_select
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.support import (
    BASTION_PORT, MSSQL_HOST, MSSQL_PASSWORD, MSSQL_PORT, MSSQL_SA_PASSWORD, kill_mssql_session, make_mssql_profile,
    requires_mssql, requires_mssql_bastion,
)
from tests.test_ssh_tunnel import TEST_SSH_OPTIONS

pytestmark = requires_mssql
ENDPOINT = Endpoint(MSSQL_HOST, MSSQL_PORT)
ENDLESS = "SELECT a.id FROM dbo.big_table a CROSS JOIN dbo.big_table b"  # 10^10 rows: streams until stopped
TS = Dialect.TSQL


def open_mssql(password=MSSQL_PASSWORD, **overrides):
    return MssqlAdapter().open(make_mssql_profile(**overrides), ENDPOINT, password, None)


@pytest.fixture
def conn():
    connection = open_mssql()
    yield connection
    connection.close()


def drain(result):
    rows = []
    while batch := result.fetch(5000):
        rows += batch
    return rows


def one(connection, sql):
    """The first rows of a small batch, read to its end as the worker reads it (so closing sends no attention)."""
    result = connection.run(sql)
    rows = result.fetch(10)
    while result.next_result():
        pass
    result.close()
    return rows


def seg(kind, name):
    return PathSegment(kind=kind, name=name)


def test_wrong_password_twice():  # Review Focus 5: pymssql's login-error state is sticky
    for _ in range(2):
        with pytest.raises(ConnectionFailed, match="refused the login"):
            open_mssql(password="wrong")


def test_unreachable_server():
    with pytest.raises(ConnectionFailed, match="Could not reach SQL Server at 127.0.0.1:14398"):
        MssqlAdapter().open(make_mssql_profile(port=14398), Endpoint(MSSQL_HOST, 14398), MSSQL_PASSWORD, None)


def test_server_info(conn):
    info = conn.server_info()
    assert info.version.startswith("Microsoft SQL Server 2022") and "\n" not in info.version
    assert (info.current_database, info.current_user) == ("dbaguy", "dbaguy")
    assert conn.current_database() == "dbaguy"


def test_stream_big_table_in_pages(conn):
    result = conn.run("SELECT id, label, value FROM dbo.big_table ORDER BY id")
    assert [(c.name, c.type_name, c.category) for c in result.columns()] == [
        ("id", "int", "number"), ("label", "nvarchar(50)", "text"), ("value", "float", "number"),
    ]
    first = result.fetch(500)
    assert len(first) == 500 and first[0] == [1, "row 1", 1.5]
    assert len(first) + len(drain(result)) == 100_000
    assert not result.next_result()
    result.close()


def test_types(conn):
    result = conn.run("SELECT * FROM dbo.types_demo ORDER BY id")
    columns = result.columns()
    assert [c.type_name for c in columns] == [
        "int", "bigint", "decimal(38,10)", "money", "float", "bit", "uniqueidentifier", "date", "datetime",
        "datetime2(7)", "datetimeoffset(7)", "time(7)", "varbinary(max)", "nvarchar(max)", "xml", "sql_variant",
        "varchar(50)", "timestamp",
    ]
    assert [c.category for c in columns] == [
        "number", "number", "number", "number", "number", "bool", "text", "datetime", "datetime", "datetime",
        "datetime", "datetime", "binary", "text", "text", "binary", "text", "binary",
    ]
    full, empty = result.fetch(2)
    assert full[:17] == [
        1, 9223372036854775807, "12345678901234567890.1234567890", "12.3456", 1.5, True,
        "B9F3C1D2-0000-4000-8000-000000000001", "2026-10-18", "2026-10-18 12:34:56.123", "2026-10-18 12:34:56.123456",
        "2026-10-18 12:34:56.123456+08:00", "12:34:56.123456", b"\xde\xad\xbe\xef", "it's 中文 😀", '<a x="1"/>',
        b"*\x00\x00\x00", "中文",
    ]
    assert isinstance(full[17], bytes) and len(full[17]) == 8  # rowversion
    assert empty[:16] == [2] + [None] * 14 + [b"abc"]
    assert [m.text for m in result.take_messages()] == [
        "sql_variant values are shown as raw bytes: CAST them to read them, e.g. CAST(col AS nvarchar(4000))"]
    result.close()


def test_later_result_sets_and_temp_tables_guess_their_types(conn):
    result = conn.run("SELECT 1 AS a; SELECT CAST('2026-10-18' AS date) AS d, NEWID() AS g, CAST(NULL AS int) AS n")
    assert result.fetch(5) == [[1]]
    assert result.next_result()
    assert [(c.name, c.type_name, c.category) for c in result.columns()] == [
        ("d", "date", "datetime"), ("g", "uniqueidentifier", "text"), ("n", "int", "number"),
    ]
    result.close()
    temp = conn.run("CREATE TABLE #y (a int); INSERT #y VALUES (7); SELECT a FROM #y")  # the describe query fails (11525)
    assert [(c.name, c.type_name) for c in temp.columns()] == [("a", "int")] and temp.fetch(5) == [[7]]
    temp.close()
    assert [c.name for c in conn.run("SELECT 1, 2 AS b").columns()] == ["(No column name)", "b"]


def test_batches_over_64_kib_are_not_described(conn):  # their column types are guessed from the rows
    short = conn.run("SELECT CAST(1 AS smallint) AS a")
    assert [c.type_name for c in short.columns()] == ["smallint"]
    short.close()
    long = conn.run("SELECT CAST(1 AS smallint) AS a -- " + "x" * (64 * 1024))
    assert [c.type_name for c in long.columns()] == ["int"] and long.fetch(5) == [[1]]
    long.close()


def test_zero_rows_and_affected_rows(conn):
    result = conn.run("SELECT id, label FROM dbo.big_table WHERE id < 0")
    assert [c.name for c in result.columns()] == ["id", "label"] and result.fetch(10) == []
    result.close()
    insert = conn.run("DECLARE @t TABLE (a int); INSERT @t VALUES (1), (2)")
    assert insert.columns() is None and insert.affected_rows() == 2
    insert.close()
    nocount = conn.run("SET NOCOUNT ON; DECLARE @t TABLE (a int); INSERT @t VALUES (1); SET NOCOUNT OFF")
    assert nocount.affected_rows() is None
    nocount.close()


def test_procedure_with_two_result_sets_and_a_message(conn):
    result = conn.run("EXEC dbo.two_results 3")
    assert result.fetch(10) == [[1, "row 1"], [2, "row 2"], [3, "row 3"]]
    assert result.next_result() and result.fetch(10) == [[100000]]
    assert not result.next_result()
    assert [m.text for m in result.take_messages()] == ["two_results starts"]
    result.close()
    assert one(conn, "SELECT 1") == [[1]]


def test_messages_arrive_live_and_in_order(conn):  # Review Focus 5
    started, heard = time.monotonic(), []
    conn.set_message_listener(lambda m: heard.append((time.monotonic() - started, m.text)))
    result = conn.run("EXEC dbo.noisy")
    assert result.fetch(5) == [[4]]
    result.close()
    conn.set_message_listener(None)
    assert [text for _, text in heard] == ["one", "two", "three"]
    assert heard[1][0] < 1.5 <= heard[2][0]  # "two" WITH NOWAIT came before the 2 s wait ended


def test_error_after_a_result_set_leaves_the_connection_usable(conn):
    result = conn.run("EXEC dbo.fail_after_select")
    assert result.fetch(5) == [[1]]
    with pytest.raises(StatementError) as info:
        result.next_result()
    assert info.value.code == "50000" and info.value.message.startswith("boom after the first result")
    assert "fail_after_select" in info.value.message and info.value.position is None  # a procedure line: no editor mark
    result.close()
    assert one(conn, "SELECT 2") == [[2]]


def test_errors_carry_number_and_line(conn):  # Review Focus 5
    result = conn.run("SELECT 1\nSELECT 2\nSELECT * FROM nope")
    assert result.fetch(5) == [[1]] and result.next_result() and result.fetch(5) == [[2]]
    with pytest.raises(StatementError) as missing:
        result.next_result()
    assert missing.value.code == "208" and missing.value.message == "Invalid object name 'nope'."
    assert (missing.value.position.line, missing.value.position.column) == (3, 1)
    result.close()
    with pytest.raises(StatementError) as syntax:
        conn.run("SELECT 1,\nFROM t")
    assert syntax.value.code == "156" and syntax.value.position.line == 2


def test_text_is_sent_verbatim(conn):
    assert one(conn, "SELECT '100%' AS pct, N'GO' AS go_text, '%s' AS s") == [["100%", "GO", "%s"]]


def test_text_that_cannot_be_sent_is_reported_with_its_position(conn):  # as MySQL: half an emoji from a broken paste
    with pytest.raises(StatementError) as info:
        conn.run("SELECT 1,\n N'\ud83d'")
    assert info.value.code == "invalid_text" and (info.value.position.line, info.value.position.column) == (2, 4)
    assert one(conn, "SELECT 1") == [[1]]


def test_cancel_while_waiting_for_the_server(conn):  # Review Focus 2
    threading.Timer(0.5, conn.cancel).start()
    started = time.monotonic()
    with pytest.raises(StatementCancelled):
        conn.run("WAITFOR DELAY '00:00:30'")
    assert time.monotonic() - started < 2.5
    assert one(conn, "SELECT 1") == [[1]]


def test_cancel_mid_stream_then_reuse(conn):  # Review Focus 2
    result = conn.run(ENDLESS)
    assert len(result.fetch(1000)) == 1000
    started = time.monotonic()
    conn.cancel()  # rows are streaming: only the flag, the next fetch cancels on this thread
    with pytest.raises(StatementCancelled):
        drain(result)
    assert time.monotonic() - started < 2
    result.close()
    assert one(conn, "SELECT 1") == [[1]]


def test_cancel_with_nothing_running_is_harmless(conn):  # Review Focus 2
    conn.cancel()
    assert one(conn, "SELECT 1") == [[1]]


def test_a_cancel_never_relabels_later_errors(conn):  # Review Focus 2, R-03a-P4
    threading.Timer(0.5, conn.cancel).start()
    with pytest.raises(StatementCancelled):
        conn.run("WAITFOR DELAY '00:00:30'")
    with pytest.raises(StatementError) as failing:
        conn.run("SELECT * FROM nope")
    assert failing.value.code == "208"
    [[spid]] = one(conn, "SELECT @@SPID")
    conn.cancel()  # nothing running
    kill_mssql_session(spid)
    with pytest.raises(StatementError) as lost:
        conn.commit()
    assert lost.value.code == "connection_lost"


def test_a_killed_connection_is_reported_as_lost(conn):
    [[spid]] = one(conn, "SELECT @@SPID")
    kill_mssql_session(spid)
    with pytest.raises(StatementError) as info:
        conn.run("SELECT 1")
    assert info.value.code == "connection_lost"
    assert not conn.in_transaction()


def test_cancel_keeps_the_transaction_and_temp_tables(conn):
    conn.set_autocommit(False)
    one(conn, "CREATE TABLE #k (a int); INSERT #k VALUES (1)")
    assert conn.in_transaction()
    threading.Timer(0.5, conn.cancel).start()
    with pytest.raises(StatementCancelled):
        conn.run("INSERT #k VALUES (2); WAITFOR DELAY '00:00:30'")
    assert conn.in_transaction()
    assert one(conn, "SELECT COUNT(*) FROM #k") == [[2]]  # the attention ends the statement, not the transaction
    conn.rollback()


def test_closing_a_partly_read_stream_inside_a_transaction_keeps_it(conn):
    conn.set_autocommit(False)
    one(conn, "CREATE TABLE #c (a int); INSERT #c VALUES (1)")
    result = conn.run(ENDLESS)
    assert len(result.fetch(500)) == 500
    result.close()
    assert conn.in_transaction()
    one(conn, "INSERT #c VALUES (2)")
    conn.commit()
    assert not conn.in_transaction() and one(conn, "SELECT COUNT(*) FROM #c") == [[2]]


def test_manual_commit_mode_uses_implicit_transactions(conn):  # Review Focus 4
    assert conn.autocommit() and not conn.in_transaction()
    conn.set_autocommit(False)
    assert not conn.autocommit()
    conn.run("SELECT 1 AS a").close()
    assert not conn.in_transaction()  # no table read, and the describe query opened none
    one(conn, "SELECT TOP 1 id FROM dbo.big_table")
    assert conn.in_transaction()  # reading a table opens one in IMPLICIT_TRANSACTIONS mode
    with pytest.raises(StatementError) as info:
        conn.set_autocommit(True)
    assert info.value.code == "transaction_open"
    conn.rollback()
    assert not conn.in_transaction()
    one(conn, "BEGIN TRAN; BEGIN TRAN")
    conn.commit()
    assert not conn.in_transaction()  # every nesting level committed
    one(conn, "SET IMPLICIT_TRANSACTIONS OFF")  # typed by the user: read back from @@OPTIONS
    assert conn.autocommit()


def test_a_paused_result_in_manual_mode_reports_a_transaction(conn):  # Review Focus 4, Ruling 17
    conn.set_autocommit(False)
    result = conn.run(ENDLESS)
    assert len(result.fetch(500)) == 500
    assert conn.in_transaction()  # answered without a query while rows are unread
    result.close()
    assert conn.in_transaction()  # now read from the server
    conn.rollback()


def test_dbaguys_own_queries_never_abort_the_users_transaction(conn):  # Review Focus 4
    one(conn, "SET XACT_ABORT ON")
    one(conn, "BEGIN TRAN")
    result = conn.run("CREATE TABLE #x (a int); INSERT #x VALUES (1); SELECT a FROM #x")  # the describe fails on #x
    assert result.fetch(5) == [[1]] and not result.next_result()  # read to the end: closing sends no attention
    result.close()
    assert conn.in_transaction()
    conn.rollback()
    one(conn, "SET XACT_ABORT OFF")


def test_a_cancel_during_the_start_is_kept(conn, monkeypatch):  # R-03b-P5
    real, calls = catalog.read_rows, []

    def cancel_once(c):
        if not calls:
            calls.append(1)
            conn.cancel()  # lands while the describe's rows are read: before the batch is sent
        return real(c)

    monkeypatch.setattr(catalog, "read_rows", cancel_once)
    started = time.monotonic()
    with pytest.raises(StatementCancelled):
        conn.run("WAITFOR DELAY '00:00:30'")
    assert time.monotonic() - started < 2  # the WAITFOR was never sent
    monkeypatch.setattr(catalog, "read_rows", real)
    assert one(conn, "SELECT 1") == [[1]]


def test_a_failed_describe_restores_manual_mode(conn, monkeypatch):  # R-03b-P6
    conn.set_autocommit(False)
    real = catalog.read_rows

    def stop_after_switching_off(c):
        c.execute_non_query("SET IMPLICIT_TRANSACTIONS OFF")  # as if the wrapper ended after its first SET
        raise driver.DriverError("simulated")

    monkeypatch.setattr(catalog, "read_rows", stop_after_switching_off)
    result = conn.run("SELECT 1 AS a")
    monkeypatch.setattr(catalog, "read_rows", real)
    result.close()
    assert one(conn, "SELECT @@OPTIONS & 2") == [[2]] and not conn.autocommit()
    one(conn, "SELECT TOP 1 id FROM dbo.big_table")
    assert conn.in_transaction()  # still manual: reading a table opened a transaction
    conn.rollback()


def test_cancel_hammered_during_the_describe_never_wedges(conn):  # R-03b-P4 (and P6: manual mode survives)
    conn.set_autocommit(False)
    rng = random.Random(3)
    for i in range(100):
        outcome = []

        def attempt():
            try:
                result = conn.run("SELECT name, object_id FROM sys.objects")  # a describe, then a small result
                result.fetch(5)
                result.close()
                outcome.append("ran")
            except StatementCancelled:
                outcome.append("cancelled")

        timer = threading.Timer(rng.uniform(0, 0.004), conn.cancel)
        runner = threading.Thread(target=attempt, daemon=True)
        timer.start()
        runner.start()
        runner.join(10)
        timer.cancel()
        timer.join()
        assert not runner.is_alive(), f"wedged in iteration {i}"
        assert outcome, f"iteration {i} raised something else"
        assert one(conn, "SELECT @@OPTIONS & 2") == [[2]], f"manual mode lost in iteration {i}"
    conn.rollback()


def test_cancel_hammered_while_executing_and_streaming_never_wedges(conn):
    """R-03b-P4 beyond the describe: cancels land while the batch waits for its first response (the wait callback),
    while rows stream (flag only), or after the end. A cross-thread dbcancel() at those moments read alongside the
    worker inside execute_query(): about one wedged or killed session in 500 cancels (43c3f2c's review)."""
    rng = random.Random(5)
    for i in range(100):
        outcome = []

        def attempt():
            try:
                result = conn.run("SELECT TOP 2000 a.name FROM sys.all_objects a CROSS JOIN sys.all_objects b")
                while result.fetch(500):
                    pass
                result.close()
                outcome.append("ran")
            except StatementCancelled:
                outcome.append("cancelled")

        timer = threading.Timer(rng.uniform(0, 0.02), conn.cancel)
        runner = threading.Thread(target=attempt, daemon=True)
        timer.start()
        runner.start()
        runner.join(10)
        timer.cancel()
        timer.join()
        assert not runner.is_alive(), f"wedged in iteration {i}"
        assert outcome, f"iteration {i} raised something else"
        assert one(conn, "SELECT 'mine' AS m") == [["mine"]], f"desynchronised in iteration {i}"


def test_a_cancel_that_stops_the_describes_last_set_restores_manual_mode(conn, monkeypatch):  # R-03b-P6
    conn.set_autocommit(False)
    real = catalog.read_rows

    def cancelled_after_reading(c):
        found = real(c)
        c.execute_non_query("SET IMPLICIT_TRANSACTIONS OFF")  # as if an attention had stopped the wrapper's last SET
        conn.cancel()  # ... and the flag of that cancel (the rows are read in STARTING: only the flag)
        return found

    monkeypatch.setattr(catalog, "read_rows", cancelled_after_reading)
    with pytest.raises(StatementCancelled):
        conn.run("SELECT 1 AS a")
    monkeypatch.setattr(catalog, "read_rows", real)
    assert one(conn, "SELECT @@OPTIONS & 2") == [[2]] and not conn.autocommit()


# ---- live messages (carried from Task 4's review) ----


def test_a_print_arrives_once_live_and_never_again_from_take_messages(conn):
    heard = []
    conn.set_message_listener(lambda m: heard.append(m.text))
    result = conn.run("PRINT 'only once'; SELECT 1 AS a")
    assert result.fetch(5) == [[1]] and not result.next_result()
    assert result.take_messages() == []
    result.close()
    conn.set_message_listener(None)
    assert heard == ["only once"]


def test_messages_from_dbaguys_own_queries_never_reach_a_request(conn, monkeypatch):
    for name in ("_STATE_QUERY", "_COMMIT", "_ROLLBACK", "_SERVER_INFO"):  # each internal query now prints first
        monkeypatch.setattr(mssql_adapter, name, f"PRINT 'internal {name}'; " + getattr(mssql_adapter, name))
    heard = []
    conn.set_autocommit(False)
    one(conn, "SELECT TOP 1 id FROM dbo.big_table")  # stale state, an open transaction
    conn.server_info()
    conn.commit()
    one(conn, "SELECT TOP 1 id FROM dbo.big_table")
    conn.rollback()
    assert not conn.in_transaction()  # the state query ran here, without a listener
    one(conn, "PRINT 'left over'")  # a statement's own message, never taken: it must not reach a later request either
    conn.set_message_listener(lambda m: heard.append(m.text))
    result = conn.run("SELECT 2 AS b")  # run() reads the stale state again, now with a listener
    assert result.fetch(5) == [[2]] and not result.next_result()
    assert result.take_messages() == []
    result.close()
    assert conn.in_transaction() is False and conn.autocommit() is False
    conn.set_message_listener(None)
    assert heard == []


# ---- what the MySQL adapter review taught (03a): desync, closing between sets, the transaction badge ----


def read_all(connection, sql):
    """Every result set of a batch, read to its end."""
    result = connection.run(sql)
    try:
        sets = [drain(result)]
        while result.next_result():
            sets.append(drain(result))
        return sets
    finally:
        result.close()


def test_a_cancelled_procedure_never_answers_the_next_statement(conn):
    result = conn.run("EXEC dbo.two_results 3")
    assert result.fetch(10) == [[1, "row 1"], [2, "row 2"], [3, "row 3"]]
    conn.cancel()  # between the procedure's result sets: the flag, honoured before the next one is asked for
    with pytest.raises(StatementCancelled):
        result.next_result()
    result.close()
    assert one(conn, "SELECT 'mine' AS m") == [["mine"]]
    assert read_all(conn, "EXEC dbo.two_results 1") == [[[1, "row 1"]], [[100000]]]


def test_a_cancel_inside_a_batch_with_a_wait_between_selects_leaves_no_rows_behind(conn):
    threading.Timer(0.5, conn.cancel).start()
    started = time.monotonic()
    with pytest.raises(StatementCancelled):  # the server holds set 1 while it waits: cancelled across threads
        read_all(conn, "SELECT 1 AS a; WAITFOR DELAY '00:00:30'; SELECT 2 AS b")
    assert time.monotonic() - started < 3
    assert one(conn, "SELECT 'mine' AS m") == [["mine"]]
    result = conn.run("SELECT TOP 5000 id FROM dbo.big_table ORDER BY id; WAITFOR DELAY '00:00:02'; SELECT 'late' AS l")
    assert len(result.fetch(100)) == 100
    conn.cancel()  # streaming: the next read stops the batch on this thread
    with pytest.raises(StatementCancelled):
        drain(result)
    result.close()
    assert one(conn, "SELECT 'mine again' AS m") == [["mine again"]]


def test_closing_at_a_result_set_boundary_stops_the_rest_of_the_batch(conn):
    one(conn, "CREATE TABLE #rest (a int)")
    result = conn.run(
        "SELECT TOP 3 id FROM dbo.big_table ORDER BY id; "
        "SELECT TOP 2000000 a.id FROM dbo.big_table a CROSS JOIN dbo.big_table b; INSERT #rest VALUES (1)"
    )
    assert drain(result) == [[1], [2], [3]]  # the first set read to its end, the second not asked for
    started = time.monotonic()
    result.close()
    assert time.monotonic() - started < 2  # an attention, not two million rows read and dropped
    assert one(conn, "SELECT COUNT(*) FROM #rest") == [[0]]  # the INSERT after them never ran
    procedure = conn.run("EXEC dbo.two_results 2")
    assert drain(procedure) == [[1, "row 1"], [2, "row 2"]]
    procedure.close()
    assert one(conn, "SELECT 'mine' AS m") == [["mine"]]


def test_rowcount_and_error_inside_a_batch_are_the_users(conn):
    assert one(conn, "DECLARE @t TABLE (a int); INSERT @t VALUES (1), (2), (3); SELECT @@ROWCOUNT AS n") == [[3]]
    # the failed SELECT sends its (empty) result set before the CATCH block's
    assert read_all(conn, "BEGIN TRY SELECT 1/0 END TRY BEGIN CATCH SELECT @@ERROR AS e END CATCH")[-1] == [[8134]]


def test_an_xact_abort_error_ends_the_transaction_and_the_state_follows(conn):
    one(conn, "SET XACT_ABORT ON")
    try:
        one(conn, "BEGIN TRAN")
        assert conn.in_transaction()
        with pytest.raises(StatementError) as info:
            one(conn, "SELECT 1/0")
        assert info.value.code == "8134"
        assert not conn.in_transaction() and not conn.transaction_failed()
        conn.set_autocommit(False)
        one(conn, "SELECT TOP 1 id FROM dbo.big_table")
        assert conn.in_transaction()
        with pytest.raises(StatementError) as conversion:
            one(conn, "SELECT CAST('x' AS int)")  # a run-time error under XACT_ABORT ON ends the implicit one too
        assert conversion.value.code == "245" and not conn.in_transaction()
    finally:
        one(conn, "SET XACT_ABORT OFF")


def test_a_deadlock_victims_transaction_state_follows_the_server(conn):
    other = open_mssql()
    one(conn, "CREATE TABLE ##zz_deadlock_a (a int); CREATE TABLE ##zz_deadlock_b (b int); "
              "INSERT ##zz_deadlock_a VALUES (1); INSERT ##zz_deadlock_b VALUES (1)")
    try:
        one(conn, "SET DEADLOCK_PRIORITY LOW; BEGIN TRAN; UPDATE ##zz_deadlock_a SET a = 2")
        one(other, "BEGIN TRAN; UPDATE ##zz_deadlock_b SET b = 2")
        failures = []

        def other_side():
            try:
                one(other, "UPDATE ##zz_deadlock_a SET a = 3")  # waits for conn's lock
            except StatementError as exc:
                failures.append(exc)

        waiter = threading.Thread(target=other_side, daemon=True)
        waiter.start()
        time.sleep(0.5)
        with pytest.raises(StatementError) as victim:  # closes the cycle; conn has the lower priority
            one(conn, "UPDATE ##zz_deadlock_b SET b = 3")
        waiter.join(15)
        assert victim.value.code == "1205" and not failures
        assert not conn.in_transaction()  # SQL Server rolled the victim's transaction back
        assert other.in_transaction()
        other.rollback()
    finally:
        other.close()
        one(conn, "SET DEADLOCK_PRIORITY NORMAL; DROP TABLE ##zz_deadlock_a; DROP TABLE ##zz_deadlock_b")


# ---- fix round 1: cancels that never hang (R-03b-T8a/d/e), SET options, FOR JSON/XML, real, the paused badge ----

CHATTY = {  # temporary procedures: nothing is left in the dev database
    "dml": "CREATE PROCEDURE #zz_work AS BEGIN DECLARE @t TABLE (a int); DECLARE @i int = 0; "
           "WHILE @i < 3000000 BEGIN INSERT @t VALUES (@i); SET @i += 1; END; SELECT 'done' AS d; END",
    "nowait": "CREATE PROCEDURE #zz_work AS BEGIN DECLARE @i int = 0; WHILE @i < 400 BEGIN "
              "RAISERROR('tick %d', 0, 1, @i) WITH NOWAIT; WAITFOR DELAY '00:00:00.002'; SET @i += 1; END; "
              "SELECT 'done' AS d; END",
}


@pytest.mark.parametrize("kind, iterations", [("dml", 8), ("nowait", 20)])
def test_cancelling_chatty_batches_never_hangs(kind, iterations):  # Critical: every cancel ends, never a hang
    rng, outcomes, connection = random.Random(7), [], None
    for i in range(iterations):
        if connection is None:
            connection = open_mssql()
            one(connection, CHATTY[kind])
            connection.set_message_listener(lambda m: None)
        outcome = []

        def attempt(c=connection):
            try:
                result = c.run("EXEC #zz_work")
                while result.fetch(500):
                    pass
                result.close()
                outcome.append("ran")
            except StatementError as exc:
                outcome.extend([exc.code, exc.message])

        runner = threading.Thread(target=attempt, daemon=True)
        runner.start()
        time.sleep(rng.uniform(0.1, 0.4))
        connection.cancel()
        runner.join(10)
        assert not runner.is_alive(), f"{kind}: hung in iteration {i}"
        assert outcome[0] in ("cancelled", "connection_lost"), outcome
        outcomes.append(outcome[0])
        if outcome[0] == "connection_lost":  # auto-commit, nothing open before: dropped by itself, and told so (T8h)
            assert outcome[1] == mssql_adapter_cancel.BUSY_DROPPED_MESSAGE
            connection.close()
            connection = None
        else:
            assert one(connection, "SELECT 'mine' AS m") == [["mine"]] and not connection.in_transaction()
    if connection is not None:
        connection.close()
    if kind == "nowait":  # the cancel goes out from the message handler: the session stays
        assert outcomes.count("cancelled") == iterations


@pytest.mark.parametrize("setup, batch", [
    ("manual", "EXEC #zz_work"),  # manual commit: the batch's work is in an open transaction
    ("open", "EXEC #zz_work"),  # auto-commit, but a transaction was open before the batch
    ("word", "BEGIN TRAN; EXEC #zz_work"),  # auto-commit, the batch opens one itself
])
def test_a_batch_that_may_hold_a_transaction_is_never_dropped_by_the_first_cancel(conn, setup, batch):  # T8h
    one(conn, CHATTY["dml"])
    if setup == "manual":
        conn.set_autocommit(False)
    elif setup == "open":
        one(conn, "BEGIN TRAN")
    heard: list = []
    conn.set_message_listener(lambda m: heard.append((m.severity, m.text)))
    for _ in range(3):  # a rare quiet moment cancels the loop cleanly instead: try again
        heard.clear()
        thread, state = stream_in_thread(conn, batch)
        time.sleep(0.3)
        conn.cancel()
        deadline = time.monotonic() + 4
        while ("warning", mssql_adapter_cancel.BUSY_WARNING) not in heard and "out" not in state:
            assert time.monotonic() < deadline, "neither a warning nor an end"
            time.sleep(0.05)
        if "out" not in state:
            break
        assert state["out"] == "cancelled"
        conn.rollback()
        if setup == "open":
            one(conn, "BEGIN TRAN")
    else:
        pytest.skip("the loop found a quiet moment three times")
    time.sleep(0.5)
    assert thread.is_alive() and "out" not in state  # warned, still running: nothing was rolled back
    assert [text for severity, text in heard if severity == "warning"] == [mssql_adapter_cancel.BUSY_WARNING]
    conn.force_cancel()  # the second Cancel
    thread.join(5)
    assert state.get("out") == "connection_lost" and state["message"] == mssql_adapter_cancel.ABORTED_MESSAGE


@pytest.fixture
def locked_row():
    """##zz_lockrows (5,000 rows) with row 3000 locked by another session's open transaction."""
    locker = open_mssql()
    one(locker, "IF OBJECT_ID('tempdb..##zz_lockrows') IS NOT NULL DROP TABLE ##zz_lockrows; "
                "CREATE TABLE ##zz_lockrows (id int PRIMARY KEY, v int); "
                "INSERT ##zz_lockrows SELECT TOP 5000 id, id AS v FROM dbo.big_table ORDER BY id")
    locker.set_autocommit(False)
    one(locker, "UPDATE ##zz_lockrows SET v = v WHERE id = 3000")
    yield "SELECT id, v FROM ##zz_lockrows ORDER BY id"
    locker.rollback()
    locker.set_autocommit(True)
    one(locker, "DROP TABLE ##zz_lockrows")
    locker.close()


def stream_in_thread(connection, sql):
    state = {"rows": 0}

    def reader():
        try:
            result = connection.run(sql)
            while rows := result.fetch(500):
                state["rows"] += len(rows)
            result.close()
            state["out"] = "ran"
        except StatementError as exc:
            state["out"], state["message"] = exc.code, exc.message

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    return thread, state


def test_a_select_blocked_on_a_row_lock_mid_stream_can_be_cancelled(conn, locked_row):  # Important 1
    thread, state = stream_in_thread(conn, locked_row)
    time.sleep(0.8)
    assert 0 < state["rows"] < 3000 and "out" not in state  # blocked inside a row read
    started = time.monotonic()
    conn.cancel()
    thread.join(5)
    assert state.get("out") == "cancelled" and time.monotonic() - started < 2
    assert one(conn, "SELECT 'mine' AS m") == [["mine"]]


def test_closing_the_session_mid_stream_reads_connection_lost_not_the_end(conn, locked_row):  # R-03b-T8a (6)
    thread, state = stream_in_thread(conn, locked_row)
    time.sleep(0.8)
    conn.abort()  # the tab closes: the blocked read ends by dropping the connection
    thread.join(5)
    assert state.get("out") == "connection_lost"  # never "ran" with 2,500 of 5,000 rows
    assert "dropped to stop the statement" in state["message"]  # not "cancelled", not a bare "lost"
    with pytest.raises(StatementError) as lost:
        conn.run("SELECT 1")
    assert lost.value.code == "connection_lost" and "dropped to stop the statement" in lost.value.message


def test_a_kill_during_a_flagged_cancel_reads_connection_lost(conn):  # R-03b-T8a (5)
    [[spid]] = one(conn, "SELECT @@SPID")
    result = conn.run(ENDLESS)
    assert len(result.fetch(500)) == 500
    conn.cancel()  # between rows: only the flag
    kill_mssql_session(spid)
    with pytest.raises(StatementError) as lost:
        result.fetch(500)
    assert lost.value.code == "connection_lost"


@pytest.mark.parametrize("option", ["NOEXEC", "PARSEONLY", "FMTONLY", "SHOWPLAN_XML", "SHOWPLAN_TEXT"])
@pytest.mark.parametrize("manual", [False, True])
def test_set_options_that_change_every_answer_leave_the_session_working(conn, option, manual):  # Important 2
    conn.set_autocommit(not manual)
    one(conn, f"SET {option} ON")
    assert conn.autocommit() is not manual and not conn.in_transaction()  # no row or a plan: the cache stays
    sets = read_all(conn, "SELECT id FROM dbo.big_table WHERE id = 1")
    if option.startswith("SHOWPLAN"):
        assert sets[0] and isinstance(sets[0][0][0], str)  # the plan reaches the user
    one(conn, f"SET {option} OFF")
    assert one(conn, "SELECT id FROM dbo.big_table WHERE id = 1") == [[1]]
    assert one(conn, "SELECT @@OPTIONS & 2") == [[2 if manual else 0]]
    conn.rollback()


def test_for_json_and_for_xml_arrive_as_one_value(conn):  # minor: as SSMS joins the ~2,000-character rows
    result = conn.run("SELECT TOP 300 id, label FROM dbo.big_table ORDER BY id FOR JSON PATH")
    [column] = result.columns()
    [[text]] = drain(result)
    result.close()
    assert column.category == "json" and len(json.loads(text)) == 300
    result = conn.run("SELECT TOP 300 id FROM dbo.big_table ORDER BY id FOR XML PATH('r')")
    [[xml]] = drain(result)
    result.close()
    assert result.columns()[0].type_name == "xml" and xml.count("<r>") == 300 and xml.startswith("<r><id>1</id>")


def test_real_values_show_their_shortest_digits(conn):  # minor
    result = conn.run("SELECT CAST(0.1 AS real) AS r, CAST(0.1 AS float) AS f, CAST(NULL AS real) AS n")
    assert [c.type_name for c in result.columns()] == ["real", "float", "real"]
    assert drain(result) == [[0.1, 0.1, None]]
    result.close()


def test_a_pause_after_transaction_words_reports_a_transaction(conn):  # minor: Ruling 17 in auto-commit mode
    result = conn.run("BEGIN TRAN; " + ENDLESS)
    assert len(result.fetch(500)) == 500
    assert conn.in_transaction()  # rows unread: the state is unknown, a transaction is assumed
    result.close()
    assert conn.in_transaction()  # read from the server: BEGIN TRAN opened one
    result = conn.run("COMMIT; " + ENDLESS)
    assert len(result.fetch(500)) == 500 and conn.in_transaction()
    result.close()
    assert not conn.in_transaction()
    result = conn.run(ENDLESS)  # no transaction word: the cached state stands while paused
    assert len(result.fetch(500)) == 500 and not conn.in_transaction()
    result.close()


def test_tls_modes():
    def encrypted(connection):
        return one(connection, "SELECT encrypt_option FROM sys.dm_exec_connections WHERE session_id = @@SPID")[0][0]

    plain = open_mssql(password=MSSQL_SA_PASSWORD, username="sa", ssl=SslConfig(mode="prefer"))
    secure = open_mssql(password=MSSQL_SA_PASSWORD, username="sa", ssl=SslConfig(mode="require"))
    try:
        assert (encrypted(plain), encrypted(secure)) == ("FALSE", "TRUE")  # prefer = request: the login only
    finally:
        plain.close()
        secure.close()
    with pytest.raises(ConnectionFailed, match="CA file"):  # the public CAs did not sign the dev server's certificate
        open_mssql(ssl=SslConfig(mode="verify-ca", ca_file="/etc/ssl/cert.pem"))


def test_browse_select_on_a_bracket_name_runs(conn):
    path = [seg("database", "dbaguy"), seg("schema", "dbo"), seg("folder", "tables"), seg("table", "brack]et")]
    sql = build_browse_select(TS, path, "[a]]b] = 1", [SortKey(column="a]b", descending=True)])
    assert one(conn, sql) == [[1, "one"]]


def test_value_filters_match_their_rows(conn):
    path = [seg("database", "dbaguy"), seg("schema", "dbo"), seg("folder", "tables"), seg("table", "types_demo")]
    one(conn, "SET LANGUAGE British")  # dmy (R-03b-P10): the space-separated form is now misread
    with pytest.raises(StatementError) as dmy:
        one(conn, "SELECT CAST(N'2026-10-18 12:34:56.123' AS datetime)")
    assert dmy.value.code == "242"
    result = conn.run("SELECT * FROM dbo.types_demo WHERE id = 1")
    columns, [row] = result.columns(), result.fetch(1)
    result.close()
    cells = encode_row(row)
    for name in ("amount", "price", "flag", "guid", "born", "seen", "data", "body", "legacy"):
        i = [c.name for c in columns].index(name)
        condition = value_condition(TS, name, cells[i], columns[i].category)
        assert one(conn, build_browse_select(TS, path, condition, [], count=True)) == [[1]], name


def test_metadata_is_bound_to_the_connections_database(conn):
    assert "orders" in [n.name for n in conn.list_children(
        [seg("database", "dbaguy"), seg("schema", "sales"), seg("folder", "tables")])]
    with pytest.raises(Exception, match="bound to database 'dbaguy'"):
        conn.list_children([seg("database", "analytics"), seg("schema", "dbo")])
    assert conn.schema_snapshot().database == "dbaguy"


def test_registry_and_url_import():
    registry = default_registry()
    assert registry.get("mssql").info().dialect == TS
    parsed = registry.parse_url(
        "jdbc:sqlserver://127.0.0.1:14337;databaseName=dbaguy;user=dbaguy;encrypt=true;trustServerCertificate=true")
    assert (parsed.profile.engine, parsed.profile.port, parsed.profile.ssl.mode) == ("mssql", 14337, "require")


@requires_mssql_bastion
def test_mssql_through_the_ssh_bastion_including_cancel(tmp_path):
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    tunnels = SshTunnelManager(write_askpass_script(tmp_path), extra_options=TEST_SSH_OPTIONS)
    service = ConnectionService(AdapterRegistry([MssqlAdapter()]), profiles, tunnels)
    profile = make_mssql_profile(
        host="mssql", port=1433, ssh=SshConfig(enabled=True, host="127.0.0.1", port=BASTION_PORT, user="dev", auth="password"),
    )
    lease = service.open(profile, None, ResolvedSecrets(password=MSSQL_PASSWORD, ssh_password="dev"))
    try:
        connection = lease.connection
        assert connection.server_info().version.startswith("Microsoft SQL Server 2022")
        threading.Timer(0.5, connection.cancel).start()
        started = time.monotonic()
        with pytest.raises(StatementCancelled):
            connection.run("WAITFOR DELAY '00:00:30'")
        assert time.monotonic() - started < 3
        assert one(connection, "SELECT 1") == [[1]]
    finally:
        lease.close()
    assert tunnels.active_count() == 0
