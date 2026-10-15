import threading
import time

import pytest

from dbaguy_backend.adapters.base import Endpoint, StatementCancelled, StatementError
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.domain.errors import ConnectionFailed
from tests.support import PG_HOST, PG_PASSWORD, PG_PORT, make_pg_profile, requires_pg

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
    assert row == ["9007199254740993", "12345678901234567890.123456789", {"$type": "bytes", "size": 4, "preview": "deadbeef"}]
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


def test_parse_url():
    parsed = PostgresAdapter().parse_url("postgresql://me:p%40ss@db.example.com:6543/app?sslmode=require&connect_timeout=5")
    draft = parsed.profile
    assert (draft.host, draft.port, draft.database, draft.username) == ("db.example.com", 6543, "app", "me")
    assert draft.ssl.mode == "require"
    assert draft.options == {"connect_timeout": "5"}
    assert parsed.password == "p@ss"
