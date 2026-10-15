import threading
import time

import psycopg
import pytest
from loguru import logger

from dbaguy_backend.adapters.base import Endpoint, StatementCancelled, StatementError
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.domain.errors import ConnectionFailed, ValidationFailed
from tests.support import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, make_pg_profile, requires_pg

pytestmark = requires_pg


@pytest.fixture
def conn():
    connection = PostgresAdapter().open(make_pg_profile(), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)
    yield connection
    connection.close()


def test_wrong_password():
    with pytest.raises(ConnectionFailed, match="password authentication failed"):
        PostgresAdapter().open(make_pg_profile(), Endpoint(PG_HOST, PG_PORT), "wrong", None)


def test_server_info(conn):
    info = conn.server_info()
    assert info.version.startswith("PostgreSQL 17")
    assert info.current_database == "dbaguy"


def test_stream_big_table_in_pages(conn):
    result = conn.run("SELECT id, label, value FROM big_table ORDER BY id")
    columns = result.columns()
    assert [c.name for c in columns] == ["id", "label", "value"]
    assert [c.category for c in columns] == ["number", "text", "number"]
    first = result.fetch(500)
    assert len(first) == 500 and first[0] == [1, "row 1", 1.5]
    total = len(first)
    while batch := result.fetch(5000):
        total += len(batch)
    assert total == 100_000
    result.close()


def test_text_first_values(conn):
    row = conn.run("SELECT huge_int, big_number, data FROM blobs").fetch(1)[0]
    # Raw values after the text-first loaders: int8/numeric stay text, bytea is bytes (encoded later by the worker).
    assert row == ["9007199254740993", "12345678901234567890.123456789", b"\xde\xad\xbe\xef"]
    profile, tags, address = conn.run("SELECT profile, tags, address FROM customers WHERE id = 1").fetch(1)[0]
    assert '"score": 12345678901234567890' in profile
    assert tags == "{math,pioneer}"
    assert address == '("12 Analytical St",London,N1)'
    assert conn.run("SELECT name, address FROM customers WHERE email = 'empty@example.com'").fetch(1) == [["", None]]
    assert conn.run("SELECT name FROM customers WHERE id = 2").fetch(1) == [["林小明"]]
    statuses = conn.run("SELECT status FROM sales.orders ORDER BY id")
    assert statuses.columns()[0].type_name == "order_status"
    assert statuses.fetch(5) == [["paid"], ["pending"], ["shipped"]]


def test_dml_ddl_notice_and_vacuum(conn):
    assert conn.run("CREATE TEMP TABLE t_tmp (id int)").columns() is None
    assert conn.run("INSERT INTO t_tmp SELECT generate_series(1, 3)").affected_rows() == 3
    result = conn.run("DO $$ BEGIN RAISE NOTICE 'hello %', 42; END $$")
    assert any("hello 42" in m.text for m in result.take_messages())
    assert conn.run("VACUUM t_tmp").columns() is None


def test_multiple_statements_buffered(conn):
    result = conn.run("CREATE TEMP TABLE m (x int); INSERT INTO m VALUES (1), (2)")
    assert result.columns() is None
    assert result.next_result() is True
    assert result.affected_rows() == 2
    assert result.next_result() is False


def test_multiple_statements_starting_with_select(conn):
    result = conn.run("SELECT 1 AS a; SELECT 2 AS b")
    assert [c.name for c in result.columns()] == ["a"]
    assert result.fetch(10) == [[1]]
    assert result.next_result() is True
    assert [c.name for c in result.columns()] == ["b"]
    assert result.fetch(10) == [[2]]
    assert result.next_result() is False
    result.close()
    assert conn.run("SELECT 3").fetch(1) == [[3]]


def test_zero_row_select_still_has_columns(conn):
    result = conn.run("SELECT id, label FROM big_table WHERE false")
    assert [c.name for c in result.columns()] == ["id", "label"]
    assert result.fetch(10) == []


def test_select_into_is_not_an_error(conn):
    assert conn.run("SELECT 1 AS x INTO TEMP TABLE sel_into").columns() is None
    assert conn.run("SELECT x FROM sel_into").fetch(1) == [[1]]


def test_error_position_with_cjk(conn):
    sql = "SELECT '中文' AS a,\n  nope_column FROM big_table"
    with pytest.raises(StatementError) as info:
        conn.run(sql).fetch(1)
    assert info.value.code == "42703"
    assert (info.value.position.line, info.value.position.column) == (2, 3)


def test_cancel_running_query(conn):
    threading.Timer(0.5, conn.cancel).start()
    started = time.monotonic()
    with pytest.raises(StatementCancelled):
        conn.run("SELECT pg_sleep(30)").fetch(1)
    assert time.monotonic() - started < 5
    assert conn.run("SELECT 1 AS one").fetch(1) == [[1]]


def test_discard_stream_then_reuse(conn):
    result = conn.run("SELECT * FROM big_table")
    assert len(result.fetch(10)) == 10
    result.close()
    assert conn.run("SELECT 2").fetch(1) == [[2]]


def test_transaction_state(conn):
    conn.run("BEGIN")
    assert conn.in_transaction()
    conn.run("ROLLBACK")
    assert not conn.in_transaction()


@pytest.fixture
def tx(conn):
    """`conn` inside an open transaction with a temp table holding one uncommitted row."""
    conn.run("CREATE TEMP TABLE tx_rows (id int)")
    conn.run("BEGIN")
    conn.run("INSERT INTO tx_rows VALUES (1)")
    yield conn
    if conn.in_transaction():
        conn.run("ROLLBACK")


def assert_transaction_healthy(conn):
    assert conn.in_transaction() and not conn.transaction_failed()


def committed_rows(conn) -> str:
    conn.run("COMMIT")
    assert not conn.in_transaction()
    return conn.run("SELECT count(*) FROM tx_rows").fetch(1)[0][0]


def test_closing_a_partly_read_select_inside_a_transaction_keeps_the_transaction(tx):
    result = tx.run("SELECT g FROM generate_series(1, 5000000) g")
    assert [c.name for c in result.columns()] == ["g"]
    assert result.fetch(500) == [[g] for g in range(1, 501)]
    open_cursors = tx.run("SELECT name FROM pg_cursors").fetch(10)
    assert len([n for (n,) in open_cursors if n.startswith("dbaguy_cursor_")]) == 2  # the paused SELECT and this one
    result.close()
    result.close()  # idempotent
    assert_transaction_healthy(tx)
    assert committed_rows(tx) == "1"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 AS a; SELECT 2 AS b",  # several statements: DECLARE would reject the script
        "WITH x AS (INSERT INTO tx_rows VALUES (2) RETURNING id) SELECT id FROM x",  # data-modifying WITH
        "INSERT INTO tx_rows VALUES (3) RETURNING id",
        "SELECT 1 AS x INTO TEMP TABLE tx_into",  # SELECT INTO is not allowed in DECLARE
        "SHOW search_path",
        "EXPLAIN SELECT * FROM tx_rows",
        "VALUES (1), (2)",
        "TABLE tx_rows",
        "(SELECT 1) UNION ALL (SELECT 2)",
        "SELECT * FROM tx_rows FOR UPDATE",
        "SELECT '100%' AS pct  -- comment; with a semicolon",
        "SELECT 1 AS one;",
    ],
)
def test_statements_inside_a_transaction_never_abort_it_by_our_routing(tx, sql):
    result = tx.run(sql)
    while True:
        if result.columns() is not None:
            result.fetch(1)
        if not result.next_result():
            break
    result.close()
    assert_transaction_healthy(tx)


def test_select_inside_a_transaction_returns_raw_values_and_zero_row_columns(tx):
    assert tx.run("SELECT '100%' AS pct, 9007199254740993::int8 AS big").fetch(5) == [["100%", "9007199254740993"]]
    empty = tx.run("SELECT id, label FROM big_table WHERE false")
    assert [c.name for c in empty.columns()] == ["id", "label"] and empty.fetch(10) == []
    empty.close()
    assert_transaction_healthy(tx)


def test_error_position_inside_a_transaction_is_relative_to_the_statement(tx):
    sql = "SELECT '中文' AS a,\n  nope_column FROM big_table"
    with pytest.raises(StatementError) as info:
        tx.run(sql).fetch(1)
    assert info.value.code == "42703"
    assert (info.value.position.line, info.value.position.column) == (2, 3)
    assert tx.transaction_failed()  # the statement's own error: PostgreSQL aborts the transaction


def test_transaction_failed_tracks_the_aborted_state(conn):
    assert not conn.transaction_failed()
    conn.run("BEGIN")
    with pytest.raises(StatementError):
        conn.run("SELECT 1 / 0").fetch(1)
    assert conn.in_transaction() and conn.transaction_failed()
    conn.run("ROLLBACK")
    assert not conn.in_transaction() and not conn.transaction_failed()


def test_parse_url():
    parsed = PostgresAdapter().parse_url("postgresql://me:p%40ss@db.example.com:6543/app?sslmode=require&connect_timeout=5")
    draft = parsed.profile
    assert (draft.host, draft.port, draft.database, draft.username) == ("db.example.com", 6543, "app", "me")
    assert draft.ssl.mode == "require"
    assert draft.options == {"connect_timeout": "5"}
    assert parsed.password == "p@ss"


def _terminate_backend(pid: int) -> None:
    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="postgres", autocommit=True
    ) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        for _ in range(50):  # wait until the server has really dropped it
            if admin.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()[0] == 0:
                return
            time.sleep(0.1)
    raise AssertionError("backend did not terminate")


def test_dead_connection_raises_statement_errors(conn):
    result = conn.run("SELECT pg_backend_pid()")
    pid = result.fetch(1)[0][0]
    result.close()
    _terminate_backend(pid)
    for _ in range(2):  # the first call sees the server shutdown, later ones a closed connection
        with pytest.raises(StatementError) as info:
            conn.run("SELECT 1").fetch(1)
        assert info.value.code == "connection_lost"
        assert "lost" in info.value.message
    with pytest.raises(StatementError) as info:
        conn.run("CREATE TEMP TABLE after_death (id int)")  # buffered path
    assert info.value.code == "connection_lost"
    assert not conn.in_transaction()


@pytest.mark.parametrize(
    "sql",
    [
        "COPY big_table FROM STDIN",
        "copy (SELECT 1) to stdout with (format csv)",
        "-- export\nCOPY big_table TO STDOUT",
        "CREATE TEMP TABLE cp (id int); COPY cp FROM STDIN",  # a later statement of a script wedges it just the same
    ],
)
def test_copy_from_stdin_or_to_stdout_is_rejected_and_the_session_keeps_working(conn, sql):
    with pytest.raises(StatementError) as info:
        conn.run(sql)
    assert info.value.code == "unsupported_copy"
    assert info.value.message == "COPY FROM STDIN / TO STDOUT is not supported in the console yet"
    assert conn.run("SELECT 1").fetch(1) == [[1]]


def test_server_side_copy_still_runs(conn):
    conn.run("CREATE TEMP TABLE cp_src AS SELECT 1 AS id")
    assert conn.run("COPY cp_src TO '/dev/null'").affected_rows() == 1  # a server-side file, not STDOUT


def test_command_in_progress_is_not_reported_as_connection_lost(conn):
    with pytest.raises(psycopg.ProgrammingError):
        conn._conn.execute("COPY (SELECT 1) TO STDOUT")  # wedge the connection the way an unguarded COPY did
    with pytest.raises(StatementError) as info:
        conn.run("SELECT 1").fetch(1)
    assert "another command is already in progress" in info.value.message
    assert info.value.code != "connection_lost"  # the connection is alive, so no "Reconnect" is offered for it


def test_parse_url_lifts_connection_keys_out_of_options():
    parsed = PostgresAdapter().parse_url("postgres://u@good/db?host=attacker&password=x&connect_timeout=5")
    assert parsed.profile.host == "good"
    assert parsed.profile.username == "u"
    assert parsed.password == "x"
    assert parsed.profile.options == {"connect_timeout": "5"}


def test_parse_url_query_fills_missing_parts_and_drops_reserved_keys():
    url = "postgres://good?user=bob&port=6000&dbname=d&hostaddr=203.0.113.1&service=s&passfile=/p&sslpassword=y&application_name=a"
    parsed = PostgresAdapter().parse_url(url)
    draft = parsed.profile
    assert (draft.host, draft.port, draft.database, draft.username) == ("good", 6000, "d", "bob")
    assert draft.options == {"application_name": "a"}
    assert parsed.password is None
    assert PostgresAdapter().parse_url("postgres:///db?host=socket-host").profile.host == "socket-host"
    with pytest.raises(ValidationFailed, match="host"):
        PostgresAdapter().parse_url("postgres:///db")
    with pytest.raises(ValidationFailed, match="port"):
        PostgresAdapter().parse_url("postgres://good?port=abc")


def test_profile_options_cannot_redirect_or_override_the_connection():
    reserved = {
        "host": "attacker.invalid", "hostaddr": "203.0.113.1", "port": "1", "dbname": "analytics", "user": "nobody",
        "password": "wrong-secret", "service": "nope", "passfile": "/nonexistent/passfile", "sslpassword": "ssl-secret",
    }
    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        connection = PostgresAdapter().open(
            make_pg_profile(options={**reserved, "connect_timeout": "5"}), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None
        )
    finally:
        logger.remove(sink)
    try:
        info = connection.server_info()
        assert (info.current_database, info.current_user) == ("dbaguy", PG_USER)
    finally:
        connection.close()
    warning = "\n".join(messages)
    assert all(key in warning for key in reserved)  # names are logged ...
    assert not any(value in warning for value in ("attacker.invalid", "wrong-secret", "ssl-secret", "/nonexistent"))  # ... values never
