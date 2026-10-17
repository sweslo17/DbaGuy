import threading
import time
from contextlib import suppress

import mysql.connector
import pytest

from dbaguy_backend.adapters.base import Endpoint, StatementCancelled, StatementError
from dbaguy_backend.adapters.mysql import adapter as mysql_adapter
from dbaguy_backend.adapters.mysql.adapter import MysqlAdapter
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.values import encode_row
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.container import default_registry
from dbaguy_backend.domain.column_category import ColumnCategory
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
    BASTION_PORT, MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, make_mysql_profile, mysql_driver_kwargs, mysql_procedure,
    requires_mysql, requires_mysql_bastion,
)
from tests.test_ssh_tunnel import TEST_SSH_OPTIONS

pytestmark = requires_mysql
ENDPOINT = Endpoint(MYSQL_HOST, MYSQL_PORT)
ENDLESS = "SELECT a.id FROM big_table a CROSS JOIN big_table b"  # 10^10 rows: the server streams until stopped
# A SLEEP inside a procedure swallows KILL QUERY: it returns early and the procedure goes on to its next statement.
SLEEP_BETWEEN = "SELECT 1 AS first; DO SLEEP(3); SELECT 2 AS second;"
# About 4 s of server work that KILL QUERY does interrupt, then a write that must not happen once the CALL is closed.
LONG_THEN_WRITE = (
    "SELECT 1 AS first; "
    "SELECT COUNT(*) INTO @zz_n FROM (SELECT a.id FROM big_table a CROSS JOIN big_table b LIMIT 20000000) t; "
    "INSERT INTO zz_written VALUES (1);"
)


def refuse(**_):
    raise mysql.connector.errors.InterfaceError("side connection refused")


def open_mysql(**overrides):
    return MysqlAdapter().open(make_mysql_profile(**overrides), ENDPOINT, MYSQL_PASSWORD, None)


@pytest.fixture
def conn():
    connection = open_mysql()
    yield connection
    connection.close()


def drain(result):
    rows = []
    while batch := result.fetch(5000):
        rows += batch
    return rows


def test_wrong_password():
    with pytest.raises(ConnectionFailed, match="Access denied") as plain:
        MysqlAdapter().open(make_mysql_profile(), ENDPOINT, "wrong", None)  # ssl disable
    assert "needs TLS (SSL mode Prefer or Require)" in plain.value.message  # R-03a-P5: the hint for disable mode
    with pytest.raises(ConnectionFailed, match="Access denied") as secure:
        MysqlAdapter().open(make_mysql_profile(ssl=SslConfig(mode="require")), ENDPOINT, "wrong", None)
    assert "needs TLS" not in secure.value.message


def test_time_zone_and_sql_mode_options_are_set_after_connect_before_read_only():  # R-03a-F2
    conn = open_mysql(options={"time_zone": "+02:00", "sql_mode": "ANSI_QUOTES"}, read_only=True)
    try:
        result = conn.run(
            "SELECT @@session.time_zone, @@session.sql_mode, @@session.autocommit, @@session.transaction_read_only"
        )
        assert result.fetch(1) == [["+02:00", "ANSI_QUOTES", 1, 1]]
        result.close()
    finally:
        conn.close()


@pytest.mark.parametrize("option", ["time_zone", "sql_mode"])
def test_an_option_value_shaped_like_an_injection_sets_nothing_else(option):  # R-03a-F2 (M3)
    # Quoted, the whole value is one literal the server refuses: the connection fails, where before it opened in
    # autocommit=0 while DbaGuy showed auto-commit on.
    with pytest.raises(ConnectionFailed, match="time_zone / sql_mode"):
        open_mysql(options={option: "+00:00',@@session.autocommit=0,@x='"})


def test_server_info(conn):
    info = conn.server_info()
    assert info.version.startswith("MySQL 8.4") and info.current_database == "dbaguy"
    assert info.current_user.startswith("dbaguy@")


def test_stream_big_table_in_pages(conn):
    result = conn.run("SELECT id, label, value FROM big_table ORDER BY id")
    assert [(c.name, c.type_name, c.category) for c in result.columns()] == [
        ("id", "int", "number"), ("label", "varchar", "text"), ("value", "double", "number"),
    ]
    first = result.fetch(500)
    assert len(first) == 500 and first[0] == [1, "row 1", 1.5]
    assert len(first) + len(drain(result)) == 100_000
    result.close()


def test_types(conn):
    result = conn.run("SELECT * FROM types_demo ORDER BY id")
    assert [c.category for c in result.columns()] == [
        "number", "number", "number", "number", "number", "number", "number", "datetime", "datetime", "datetime",
        "datetime", "datetime", "json", "binary", "binary", "text", "text", "text", "text",
    ]
    full, empty = result.fetch(2)
    assert full == [
        1, 18446744073709551615, -9007199254740993, "12345678901234567890.1234567890", 1.5, 1, 165, "0000-00-00",
        "2026-10-17 12:34:56", "2026-10-17 12:34:56", "-12:30:00", "2026", '{"a": [1, 2], "b": null}',
        b"\xde\xad\xbe\xef", b"\x01\x02\x03\x04", "happy", "a,b", "it's \\ back", "中文 😀",
    ]
    assert empty == [2] + [None] * 18
    result.close()
    probe = conn.run("SELECT CAST(0 AS DATETIME) z, 1.50 d, 18446744073709551615 big, JSON_OBJECT('a', 1) j, x'0102' b")
    assert probe.fetch(1) == [[None, "1.50", 18446744073709551615, '{"a": 1}', b"\x01\x02"]]
    probe.close()


def test_edge_values_and_cjk_names_survive(conn):  # pain point M7: TIME beyond 24 h, zero dates, FLOAT as sent
    result = conn.run("SELECT * FROM edge_values")
    assert [c.type_name for c in result.columns()] == [
        "int", "time", "time", "time", "datetime", "datetime", "date", "float", "text", "varbinary",
    ]
    assert result.fetch(1) == [[
        1, "838:59:59", "-838:59:59", "12:34:56.789012", "2026-10-17 01:02:03.456789", "0000-00-00 00:00:00",
        "2026-00-15", 0.1, "a long text value", b"\x00\xff",
    ]]
    result.close()
    cjk = conn.run("SELECT `編號`, `內容` FROM `客戶備註`")
    assert [c.name for c in cjk.columns()] == ["編號", "內容"] and cjk.fetch(5) == [[1, "測試"]]
    cjk.close()


def test_zero_row_select_has_columns_and_dml_reports_affected_rows(conn):
    result = conn.run("SELECT id, label FROM big_table WHERE id < 0")
    assert [c.name for c in result.columns()] == ["id", "label"] and result.fetch(10) == []
    result.close()
    conn.run("CREATE TEMPORARY TABLE affected (id INT)").close()
    insert = conn.run("INSERT INTO affected VALUES (1), (2)")
    assert insert.columns() is None and insert.affected_rows() == 2
    insert.close()


def test_warnings_become_one_message(conn):
    result = conn.run("SELECT CAST('abc' AS SIGNED) AS n")
    assert result.fetch(5) == [[0]]
    assert [m.text for m in result.take_messages()] == ["1 warning: run SHOW WARNINGS to see it"]
    assert result.take_messages() == []
    result.close()


def test_call_returns_every_result_set(conn):
    result = conn.run("CALL two_results(3)")
    assert result.fetch(10) == [[1, "row 1"], [2, "row 2"], [3, "row 3"]]
    assert result.next_result() and result.fetch(10) == [[100000]]
    assert result.next_result() and result.columns() is None and result.affected_rows() == 0
    assert not result.next_result()
    result.close()
    assert conn.run("SELECT 1").fetch(1) == [[1]]  # no "Commands out of sync"


def test_procedure_failing_after_a_result_set_leaves_the_connection_usable(conn):  # Review Focus 4
    result = conn.run("CALL fail_after_select()")
    assert result.fetch(5) == [[1]]
    with pytest.raises(StatementError, match="boom after the first result") as info:
        result.next_result()
    assert info.value.code == "1644"
    result.close()
    assert conn.run("SELECT 2").fetch(1) == [[2]]


def test_closing_a_fully_sent_result_does_not_poison_the_next_statement(conn):  # Review Focus 4
    result = conn.run("SELECT id FROM big_table WHERE id <= 3")
    assert result.fetch(1) == [[1]]
    result.close()
    result.close()  # idempotent
    for _ in range(3):
        assert conn.run("SELECT 7").fetch(1) == [[7]]


def assert_next_statement_gets_its_own_rows(conn):
    result = conn.run("SELECT 'next' AS n")
    assert [c.name for c in result.columns()] == ["n"] and result.fetch(5) == [["next"]]
    result.close()


def test_a_cancelled_call_is_read_to_its_end(conn):  # fix round 1: the CALL's later sets and final OK
    result = conn.run("CALL two_results(50)")
    assert len(result.fetch(10)) == 10
    conn.cancel()
    with pytest.raises(StatementCancelled):
        result.fetch(10)
    result.close()
    assert_next_statement_gets_its_own_rows(conn)


def test_cancelling_a_call_between_its_result_sets_keeps_the_protocol_in_step(conn):  # fix round 1
    with mysql_procedure("zz_sleep_between", SLEEP_BETWEEN) as name:
        result = conn.run(f"CALL {name}()")
        assert result.fetch(5) == [[1]]
        threading.Timer(0.5, conn.cancel).start()
        started = time.monotonic()
        with pytest.raises(StatementCancelled):
            result.next_result()  # the SLEEP ends early, SELECT 2 still arrives: it is a cancel all the same
        result.close()
        assert time.monotonic() - started < 2.5
        assert_next_statement_gets_its_own_rows(conn)


def test_closing_a_call_at_a_result_set_boundary_stops_the_procedure(conn):  # R-03a-T9a
    conn.run("CREATE TEMPORARY TABLE zz_written (id INT)").close()
    with mysql_procedure("zz_long_then_write", LONG_THEN_WRITE) as name:
        result = conn.run(f"CALL {name}()")
        assert result.fetch(5) == [[1]]  # the first set is read to its end; the procedure is still working
        started = time.monotonic()
        result.close()
        assert time.monotonic() - started < 2
    written = conn.run("SELECT COUNT(*) FROM zz_written")
    assert written.fetch(1) == [[0]]  # KILL QUERY stopped it before its INSERT
    written.close()
    assert_next_statement_gets_its_own_rows(conn)


def give_up_on(conn, sql, monkeypatch):
    """Open `sql` in a manual transaction, then close it while no KILL can be sent and the drain limit is 0.5 s."""
    monkeypatch.setattr(mysql_adapter, "_DRAIN_LIMIT_S", 0.5)
    conn.set_autocommit(False)
    conn.run("CREATE TEMPORARY TABLE zz_tx (id INT)").close()
    conn.run("INSERT INTO zz_tx VALUES (1)").close()
    assert conn.in_transaction()
    thread = conn.run("SELECT CONNECTION_ID()")
    [[thread_id]] = thread.fetch(1)
    thread.close()
    result = conn.run(sql)
    assert result.fetch(1)
    monkeypatch.setattr(mysql.connector, "connect", refuse)  # the KILL cannot be sent
    try:
        started = time.monotonic()
        result.close()
        assert time.monotonic() - started < 1.5
        assert not conn.in_transaction()  # the connection is gone, its transaction with it: no badge, no idle rollback
        with pytest.raises(StatementError) as info:
            conn.run("SELECT 1")
        assert info.value.code == "connection_lost"  # the Reconnect UI takes over
    finally:
        monkeypatch.undo()
        cleaner = mysql.connector.connect(**mysql_driver_kwargs())  # a dropped client's statement may run on a while
        with suppress(mysql.connector.Error):
            cleaner.cmd_query(f"KILL {thread_id}")
        cleaner.close()


def test_a_drain_that_cannot_end_gives_up_the_connection(conn, monkeypatch):  # fix round 1: never wedge
    give_up_on(conn, ENDLESS, monkeypatch)


def test_a_trickling_stream_cannot_outlast_the_drain_limit(conn, monkeypatch):  # fix round 2
    # About 100 rows a second, sent in bursts of 8 as the server's 16 KB network buffer fills: a batch of 1,000 rows
    # would take 10 s.
    give_up_on(conn, "SELECT SLEEP(0.01), REPEAT('x', 2000) FROM big_table", monkeypatch)


def test_a_call_working_between_its_result_sets_cannot_outlast_the_drain_limit(conn, monkeypatch):  # fix round 2
    with mysql_procedure("zz_sleep_between", SLEEP_BETWEEN) as name:
        give_up_on(conn, f"CALL {name}()", monkeypatch)  # nextset() would wait for the SLEEP(3)


def test_a_drain_after_a_kill_that_was_sent_is_never_cut_short(conn, monkeypatch):  # R-03a-T9f
    monkeypatch.setattr(mysql_adapter, "_DRAIN_LIMIT_S", 0.0)  # the limit is only for a drain without a KILL
    conn.set_autocommit(False)
    conn.run("CREATE TEMPORARY TABLE zz_tx (id INT)").close()
    conn.run("INSERT INTO zz_tx VALUES (1)").close()
    result = conn.run(ENDLESS)
    assert len(result.fetch(500)) == 500
    result.close()
    assert conn.in_transaction()
    rows = conn.run("SELECT COUNT(*) FROM zz_tx")
    assert rows.fetch(1) == [[1]]
    rows.close()
    conn.rollback()


# A loop around a SLEEP: KILL QUERY only cuts the SLEEP it lands in, and the loop goes on sending result sets.
KILL_SWALLOWING_LOOP = (
    "DECLARE i INT DEFAULT 0; WHILE i < 40 DO SELECT i AS n; DO SLEEP(0.5); SET i = i + 1; END WHILE;"
)


def test_a_call_that_swallows_the_kill_cannot_hold_its_close(conn, monkeypatch):  # final-wave carry (Task 9)
    monkeypatch.setattr(mysql_adapter, "_DRAIN_LIMIT_S", 1.0)  # only the waits for later result sets count
    thread = conn.run("SELECT CONNECTION_ID()")
    [[thread_id]] = thread.fetch(1)
    thread.close()
    with mysql_procedure("zz_kill_swallowing_loop", KILL_SWALLOWING_LOOP) as name:
        try:
            result = conn.run(f"CALL {name}()")
            assert result.fetch(5) == [[0]]
            started = time.monotonic()
            result.close()  # KILL QUERY is sent, yet the loop keeps sending a result set every 0.5 s
            assert time.monotonic() - started < 3
            assert not conn.in_transaction()
            with pytest.raises(StatementError) as info:
                conn.run("SELECT 1")
            assert info.value.code == "connection_lost"  # the Reconnect UI takes over
        finally:
            cleaner = mysql.connector.connect(**mysql_driver_kwargs())  # the dropped client's CALL may run on a while
            with suppress(mysql.connector.Error):
                cleaner.cmd_query(f"KILL {thread_id}")
            cleaner.close()


def test_next_set_within_restores_the_cursors_read_timeout(monkeypatch):  # final-wave carry (Task 9)
    raw = mysql.connector.connect(**mysql_driver_kwargs())
    try:
        cursor = mysql_adapter._VerbatimCursor(raw)
        cursor._read_timeout = 7
        assert cursor.next_set_within(0.5) is False  # nothing pending
        assert cursor._read_timeout == 7

        def lost(self):
            raise mysql.connector.errors.ReadTimeoutError("timed out")

        monkeypatch.setattr(mysql_adapter._VerbatimCursor, "nextset", lost)
        with pytest.raises(mysql.connector.errors.ReadTimeoutError):
            cursor.next_set_within(0.5)
        assert cursor._read_timeout == 7
    finally:
        raw.close()


def test_a_client_read_timeout_drops_the_connection(monkeypatch):  # fix round 2: a reply left half read
    conn = open_mysql(options={"read_timeout": "1"})
    try:
        with pytest.raises(StatementError) as info:
            conn.run("SELECT SLEEP(3)")
        assert info.value.code == "connection_lost" and not conn.in_transaction()
        with pytest.raises(StatementError) as again:
            conn.run("SELECT 1")  # never the SLEEP's late reply
        assert again.value.code == "connection_lost"
    finally:
        conn.close()


def test_text_that_is_not_valid_unicode_is_a_statement_error(conn):  # fix round 1: a lone surrogate
    with pytest.raises(StatementError) as info:
        conn.run("SELECT 1,\n  '\ud800'")
    assert info.value.code == "invalid_text"
    assert (info.value.position.line, info.value.position.column) == (2, 4)
    assert conn.run("SELECT 1").fetch(1) == [[1]]


def test_cancel_with_nothing_running_is_harmless(conn):  # Review Focus 4
    conn.cancel()
    assert conn.run("SELECT 1").fetch(1) == [[1]]


def test_a_cancel_never_relabels_later_errors(conn):  # Review Focus 4, R-03a-P4
    threading.Timer(0.5, conn.cancel).start()
    with pytest.raises(StatementCancelled):
        conn.run("SELECT SLEEP(30)")
    with pytest.raises(StatementError) as failing:  # the next statement's own error
        conn.run("SELECT * FROM nope")
    assert failing.value.code == "1146"
    result = conn.run("SELECT CONNECTION_ID()")
    [[thread]] = result.fetch(1)
    result.close()
    conn.cancel()  # nothing running: the KILL hits nothing, and its flag must not reach commit()
    killer = mysql.connector.connect(**mysql_driver_kwargs())
    killer.cmd_query(f"KILL {thread}")
    killer.close()
    with pytest.raises(StatementError) as lost:
        conn.commit()
    assert lost.value.code == "connection_lost"  # not "cancelled"


def test_one_statement_per_run(conn):
    with pytest.raises(StatementError) as info:
        conn.run("SELECT 1; SELECT 2")
    assert info.value.code == "1064"
    assert conn.run("SELECT 2").fetch(1) == [[2]]


def test_text_mentioning_delimiter_is_sent_verbatim(conn):  # Review Focus 1
    result = conn.run("SELECT 1 AS `delimiter` /* delimiter $$ */ -- delimiter ;\n")
    assert [c.name for c in result.columns()] == ["delimiter"] and result.fetch(1) == [[1]]
    result.close()
    text = conn.run("SELECT 'DELIMITER //\nx' AS s")
    assert text.fetch(1) == [["DELIMITER //\nx"]]
    text.close()


def test_errors_carry_errno_sqlstate_and_line(conn):
    with pytest.raises(StatementError) as info:
        conn.run("SELECT 1,\nFROM t")  # "… near 'FROM t' at line 2"
    assert info.value.code == "1064" and (info.value.position.line, info.value.position.column) == (2, 1)
    with pytest.raises(StatementError) as missing:
        conn.run("SELECT * FROM nope")
    assert missing.value.code == "1146" and "SQLSTATE: 42S02" in missing.value.message


def test_cancel_a_running_statement(conn):
    threading.Timer(0.5, conn.cancel).start()
    started = time.monotonic()
    with pytest.raises(StatementCancelled):
        conn.run("SELECT SLEEP(30)")
    assert time.monotonic() - started < 2.5
    assert conn.run("SELECT 1").fetch(1) == [[1]]


def test_a_cancel_whose_kill_cannot_be_sent_lets_the_statement_finish(conn, monkeypatch):  # Ruling 10
    monkeypatch.setattr(mysql.connector, "connect", refuse)  # only the KILL connection: `conn` is already open
    threading.Timer(0.2, conn.cancel).start()
    result = conn.run("SELECT SLEEP(0.6)")
    assert result.fetch(1) == [[0]]  # it slept to the end: not reported as cancelled
    result.close()


def test_cancel_mid_stream_then_reuse(conn):
    result = conn.run(ENDLESS)
    assert len(result.fetch(1000)) == 1000
    started = time.monotonic()
    conn.cancel()
    with pytest.raises(StatementCancelled):
        drain(result)
    assert time.monotonic() - started < 3
    result.close()
    assert conn.run("SELECT 1").fetch(1) == [[1]]


def test_closing_a_partly_read_stream_inside_a_transaction_keeps_the_transaction(conn):
    conn.set_autocommit(False)
    conn.run("CREATE TEMPORARY TABLE tx_rows (id INT)").close()
    conn.run("INSERT INTO tx_rows VALUES (1)").close()
    assert conn.in_transaction()
    result = conn.run(ENDLESS)
    assert len(result.fetch(500)) == 500
    result.close()
    assert conn.in_transaction()
    conn.run("INSERT INTO tx_rows VALUES (2)").close()
    conn.commit()
    assert not conn.in_transaction()
    assert conn.run("SELECT COUNT(*) FROM tx_rows").fetch(1) == [[2]]


def test_manual_commit_mode_and_transaction_state(conn):
    assert conn.autocommit()
    conn.set_autocommit(False)
    assert not conn.autocommit()
    conn.run("SELECT 1").close()
    assert not conn.in_transaction()  # a plain read leaves no transaction behind (protocol flag)
    conn.run("CREATE TEMPORARY TABLE st_rows (id INT)").close()
    conn.run("INSERT INTO st_rows VALUES (1)").close()
    assert conn.in_transaction()
    with pytest.raises(StatementError) as info:
        conn.set_autocommit(True)
    assert info.value.code == "transaction_open"
    conn.rollback()
    assert not conn.in_transaction()
    conn.run("INSERT INTO st_rows VALUES (2)").close()
    conn.run("CREATE TABLE IF NOT EXISTS st_ddl_probe (id INT)").close()  # DDL commits implicitly
    assert not conn.in_transaction()
    conn.run("DROP TABLE IF EXISTS st_ddl_probe").close()
    conn.run("SET autocommit = 1").close()  # typed by the user: recalibrated once no result is open
    assert conn.autocommit()


def test_a_deadlock_rollback_clears_the_transaction_flag(conn):  # R-03a-T9b: ERR packets carry no status flags
    owner = mysql.connector.connect(**mysql_driver_kwargs(autocommit=True))
    owner.cmd_query("DROP TABLE IF EXISTS zz_deadlock")
    owner.cmd_query("CREATE TABLE zz_deadlock (id INT PRIMARY KEY, v INT)")
    owner.cmd_query("INSERT INTO zz_deadlock VALUES (1, 0), (2, 0)")
    other = mysql.connector.connect(**mysql_driver_kwargs())
    blocked = threading.Thread(target=other.cmd_query, args=("UPDATE zz_deadlock SET v = 2 WHERE id = 1",))
    try:
        conn.set_autocommit(False)
        conn.run("UPDATE zz_deadlock SET v = 1 WHERE id = 1").close()
        assert conn.in_transaction()
        other.cmd_query("START TRANSACTION")
        # More rows changed: InnoDB rolls back the smaller transaction, ours.
        other.cmd_query("INSERT INTO zz_deadlock SELECT id + 10, 0 FROM big_table WHERE id <= 100")
        other.cmd_query("UPDATE zz_deadlock SET v = 2 WHERE id = 2")
        blocked.start()
        time.sleep(0.3)  # `other` now waits for our lock on row 1
        with pytest.raises(StatementError) as info:
            conn.run("UPDATE zz_deadlock SET v = 1 WHERE id = 2")
        assert info.value.code == "1213"
        assert not conn.in_transaction()  # the server rolled our transaction back
    finally:
        if blocked.ident is not None:
            blocked.join(5)
        other.close()
        conn.rollback()
        owner.cmd_query("DROP TABLE IF EXISTS zz_deadlock")
        owner.close()


def test_a_failing_ddl_has_committed_and_the_flag_says_so(conn):  # R-03a-T9b
    owner = mysql.connector.connect(**mysql_driver_kwargs(autocommit=True))
    owner.cmd_query("DROP TABLE IF EXISTS zz_implicit")
    owner.cmd_query("CREATE TABLE zz_implicit (id INT)")
    try:
        conn.set_autocommit(False)
        conn.run("INSERT INTO zz_implicit VALUES (1)").close()
        assert conn.in_transaction()
        with pytest.raises(StatementError) as info:
            conn.run("CREATE TABLE zz_implicit (id INT)")  # MySQL commits before it runs DDL, even DDL that fails
        assert info.value.code == "1050"
        assert not conn.in_transaction()
        warnings = conn.run("SHOW WARNINGS")  # R-03a-T9e: reading the flags again keeps the diagnostics
        assert [row[1] for row in warnings.fetch(5)] == [1050]
        warnings.close()
        conn.rollback()
        rows = conn.run("SELECT COUNT(*) FROM zz_implicit")
        assert rows.fetch(1) == [[1]]  # committed: the rollback could not undo it
        rows.close()
    finally:
        conn.rollback()
        owner.cmd_query("DROP TABLE IF EXISTS zz_implicit")
        owner.close()


def test_show_warnings_still_lists_the_error_of_a_failed_statement(conn, monkeypatch):  # R-03a-T9e
    for sql, errno in (("SELECT * FROM nope", 1146), ("SELECT 1,\nFROM t", 1064)):
        with pytest.raises(StatementError):
            conn.run(sql)
        warnings = conn.run("SHOW WARNINGS")
        assert [(row[0], row[1]) for row in warnings.fetch(5)] == [("Error", errno)]
        warnings.close()
    probes = []
    monkeypatch.setattr(conn, "_read_status", lambda: probes.append(1))
    for sql in ("SELECT 1,\nFROM t", ""):  # parse errors: nothing ran, nothing to read again
        with pytest.raises(StatementError):
            conn.run(sql)
    assert probes == []
    with pytest.raises(StatementError):
        conn.run("SELECT * FROM nope")
    assert probes == [1]


def test_read_only_profile_is_read_only_in_the_database():
    conn = open_mysql(read_only=True)
    try:
        with pytest.raises(StatementError) as info:
            conn.run("INSERT INTO job_log (message) VALUES ('ro')")
        assert info.value.code == "1792"  # ER_CANT_EXECUTE_IN_READ_ONLY_TRANSACTION
        conn.set_autocommit(False)
        with pytest.raises(StatementError) as manual:
            conn.run("UPDATE job_log SET message = 'x' WHERE id = 1")
        assert manual.value.code == "1792"
    finally:
        conn.close()


def test_a_set_var_hint_cannot_lift_read_only():  # Task 4 carry: transaction_read_only is not hint-updatable
    conn = open_mysql(read_only=True)
    try:
        hinted = conn.run("SELECT /*+ SET_VAR(transaction_read_only=0) */ @@transaction_read_only")
        assert hinted.fetch(1) == [[1]]  # the hint was ignored
        assert [m.text for m in hinted.take_messages()] == ["1 warning: run SHOW WARNINGS to see it"]
        hinted.close()
        warnings = conn.run("SHOW WARNINGS")
        assert [row[1] for row in warnings.fetch(5)] == [3637]  # "cannot be set using SET_VAR hint"
        warnings.close()
        with pytest.raises(StatementError) as info:
            conn.run("INSERT /*+ SET_VAR(transaction_read_only=0) */ INTO job_log (message) VALUES ('hint')")
        assert info.value.code == "1792"
    finally:
        conn.close()


def test_value_condition_finds_a_json_cell(conn):  # Task 3 carry: the grid's "Filter by this value" on JSON
    plain = conn.run("""SELECT id FROM types_demo WHERE doc = '{"a": [1, 2], "b": null}'""")
    assert plain.fetch(5) == []  # why the rule exists: a plain string is compared as a JSON string scalar
    plain.close()
    conn.run("CREATE TEMPORARY TABLE json_cells (id INT PRIMARY KEY, j JSON)").close()
    conn.run(r"""INSERT INTO json_cells VALUES (1, '{"a": [1, 2], "b": null}'), (2, '{"q": "it''s \\\\ x"}'),
                (3, '"x"'), (4, '1'), (5, 'null'), (6, NULL)""").close()
    cells = conn.run("SELECT id, j FROM json_cells ORDER BY id")
    rows = [encode_row(row) for row in cells.fetch(10)]
    cells.close()
    assert rows[1][1] == '{"q": "it\'s \\\\ x"}'  # the JSON text keeps its escaped backslash
    for row_id, cell in rows:
        condition = value_condition(Dialect.MYSQL, "j", cell, ColumnCategory.JSON)
        found = conn.run(f"SELECT id FROM json_cells WHERE {condition}")
        assert found.fetch(10) == [[row_id]], condition
        found.close()


@pytest.mark.parametrize("table, column", [
    ("edge_values", "zero_dt"), ("types_demo", "born"), ("edge_values", "part_zero"), ("types_demo", "seen"),
])
def test_value_condition_finds_a_zero_date_cell(conn, table, column):  # R-03a-T9c
    cell = conn.run(f"SELECT {column} FROM {table} WHERE id = 1")
    [[value]] = [encode_row(row) for row in cell.fetch(1)]
    cell.close()
    condition = value_condition(Dialect.MYSQL, column, value, ColumnCategory.DATETIME)
    found = conn.run(f"SELECT id FROM {table} WHERE {condition}")
    assert found.fetch(5) == [[1]], condition
    found.close()


def test_a_zero_date_literal_is_what_mysql_refuses(conn):  # why R-03a-T9c compares the text
    with pytest.raises(StatementError) as info:
        conn.run("SELECT id FROM edge_values WHERE zero_dt = '0000-00-00 00:00:00'")
    assert info.value.code == "1525"


def test_a_cancel_that_reached_the_server_before_its_statement_still_stops_it(conn, monkeypatch):  # R-03a-T9d
    monkeypatch.setattr(mysql_adapter, "_DRAIN_LIMIT_S", 2.0)
    send = mysql_adapter._VerbatimCursor.execute_verbatim

    def cancel_first(cursor, sql):  # the cancel lands after run() began, before the server has the statement:
        conn.cancel()  # its KILL finds an idle session and is ignored, so the statement runs on
        send(cursor, sql)

    monkeypatch.setattr(mysql_adapter._VerbatimCursor, "execute_verbatim", cancel_first)
    started = time.monotonic()
    with pytest.raises(StatementCancelled):
        conn.run(ENDLESS)  # the rows come back after the cancel: closing them must KILL again, not drain 10^10 rows
    assert time.monotonic() - started < 1.5
    monkeypatch.undo()
    assert_next_statement_gets_its_own_rows(conn)


def test_a_killed_connection_is_reported_as_lost(conn):
    result = conn.run("SELECT CONNECTION_ID()")
    [[thread]] = result.fetch(1)
    result.close()
    killer = mysql.connector.connect(**mysql_driver_kwargs())
    killer.cmd_query(f"KILL {thread}")
    killer.close()
    with pytest.raises(StatementError) as info:
        conn.run("SELECT 1").fetch(1)
    assert info.value.code == "connection_lost"


def test_tls_modes():
    def cipher(connection):
        result = connection.run("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        [[_, value]] = result.fetch(5)
        result.close()
        return value

    secure, plain = open_mysql(ssl=SslConfig(mode="require")), open_mysql(ssl=SslConfig(mode="disable"))
    try:
        assert cipher(secure) and cipher(plain) == ""
    finally:
        secure.close()
        plain.close()
    with pytest.raises(ConnectionFailed, match="(?i)certificate"):  # the dev server's CA is self-signed
        open_mysql(ssl=SslConfig(mode="verify-ca"))


def test_require_fails_closed_when_the_server_does_not_offer_tls(monkeypatch):  # Ruling 16
    class PlainConnection:  # what the driver hands back when the server has no TLS: it silently stays plain
        is_secure = False
        shut = False

        def shutdown(self):
            self.shut = True

    plain = PlainConnection()
    monkeypatch.setattr(mysql.connector, "connect", lambda **_: plain)
    with pytest.raises(ConnectionFailed, match="does not offer TLS, which ssl mode 'require' requires"):
        open_mysql(ssl=SslConfig(mode="require"))
    assert plain.shut


@pytest.mark.parametrize("options", [{"bogus_option": "1"}, {"tls_versions": "[TLSv9]"}])
def test_an_option_the_driver_rejects_is_a_connection_failure(options):  # Task 7 carry
    with pytest.raises(ConnectionFailed, match="Invalid MySQL connection option"):
        open_mysql(options=options)


def test_browse_select_on_a_backtick_name_runs(conn):  # Review Focus 3
    path = [PathSegment(kind="schema", name="dbaguy"), PathSegment(kind="folder", name="tables"),
            PathSegment(kind="table", name="tick`table")]
    sql = build_browse_select(Dialect.MYSQL, path, "`a``b` = 1", [SortKey(column="a`b", descending=True)])
    assert conn.run(sql).fetch(5) == [[1, "one"]]


def test_registry_and_url_import():
    registry = default_registry()
    assert registry.get("mysql").info().dialect == Dialect.MYSQL
    parsed = registry.parse_url("jdbc:mysql://dbaguy@127.0.0.1:33067/dbaguy?useSSL=false")
    assert (parsed.profile.engine, parsed.profile.port, parsed.profile.ssl.mode) == ("mysql", 33067, "disable")


@requires_mysql_bastion
def test_mysql_through_the_ssh_bastion_including_cancel(tmp_path):  # Review Focus 5
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    tunnels = SshTunnelManager(write_askpass_script(tmp_path), extra_options=TEST_SSH_OPTIONS)
    service = ConnectionService(AdapterRegistry([MysqlAdapter()]), profiles, tunnels)
    profile = make_mysql_profile(
        host="mysql", port=3306,
        ssh=SshConfig(enabled=True, host="127.0.0.1", port=BASTION_PORT, user="dev", auth="password"),
    )
    lease = service.open(profile, None, ResolvedSecrets(password=MYSQL_PASSWORD, ssh_password="dev"))
    try:
        connection = lease.connection
        assert connection.server_info().version.startswith("MySQL 8.4")
        threading.Timer(0.5, connection.cancel).start()
        started = time.monotonic()
        with pytest.raises(StatementCancelled):
            connection.run("SELECT SLEEP(30)")
        assert time.monotonic() - started < 3
        assert connection.run("SELECT 1").fetch(1) == [[1]]
    finally:
        lease.close()
    assert tunnels.active_count() == 0
