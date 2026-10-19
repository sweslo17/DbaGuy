import http.client
import json
import threading
import time

import mysql.connector
import psycopg
import pytest
from pymssql import _mssql

from tests.edit_support import ENGINES, SCRATCH_PREFIX, SERVER_ENGINES, items_table, scratch, scratch_db
from tests.fake_ssh import wait_until
from tests.support import (
    COUCHBASE_HOST,
    COUCHBASE_PORT,
    COUCHBASE_READER,
    PG_HOST,
    PG_PASSWORD,
    PG_PORT,
    PG_USER,
    _basic,
    couchbase_query,
    couchbase_query_text,
    mssql_connect,
    mysql_driver_kwargs,
    plan_fixture,
    put_raw_document,
    request_active,
    requires_couchbase,
    requires_mssql,
    requires_mysql,
    requires_pg,
    scaled,
    scratch_documents,
    scratch_key,
    stored_document,
)


@requires_pg
def test_dev_postgres_is_seeded():
    with psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy") as conn:
        assert conn.execute("SELECT count(*) FROM big_table").fetchone()[0] == 100_000
        assert conn.execute("SELECT count(*) FROM pg_database WHERE datname = 'analytics'").fetchone()[0] == 1


@pytest.fixture
def mysql_one():
    """Runs a query on the dev MySQL and returns the first column of the first row."""
    conn = mysql.connector.connect(**mysql_driver_kwargs())
    cursor = conn.cursor()

    def one(sql):
        cursor.execute(sql)
        return cursor.fetchone()[0]

    yield one
    conn.close()


@requires_mysql
def test_dev_mysql_is_seeded(mysql_one):
    assert mysql_one("SELECT COUNT(*) FROM big_table") == 100_000
    assert mysql_one("SELECT COUNT(*) FROM analytics.page_views") == 50
    assert mysql_one("SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = 'dbaguy'") == 3
    assert mysql_one("SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA = 'dbaguy'") == 1
    assert mysql_one("SELECT CAST(born AS CHAR) FROM types_demo WHERE id = 1") == "0000-00-00"
    assert mysql_one("SELECT label FROM `tick``table`") == "one"


@requires_mysql
def test_dev_mysql_has_edge_values_and_unusual_names(mysql_one):
    # Everything goes through CAST/HEX: the driver would turn zero dates into None and TIME into a timedelta.
    assert mysql_one("SELECT CAST(long_time AS CHAR) FROM edge_values") == "838:59:59"
    assert mysql_one("SELECT CAST(neg_long_time AS CHAR) FROM edge_values") == "-838:59:59"
    assert mysql_one("SELECT CAST(frac_time AS CHAR) FROM edge_values") == "12:34:56.789012"
    assert mysql_one("SELECT CAST(frac_dt AS CHAR) FROM edge_values") == "2026-10-17 01:02:03.456789"
    assert mysql_one("SELECT CAST(zero_dt AS CHAR) FROM edge_values") == "0000-00-00 00:00:00"
    assert mysql_one("SELECT CAST(part_zero AS CHAR) FROM edge_values") == "2026-00-15"
    assert mysql_one("SELECT CAST(f AS CHAR) FROM edge_values") == "0.1"
    assert mysql_one("SELECT CAST(lt AS CHAR) FROM edge_values") == "a long text value"
    assert mysql_one("SELECT HEX(vb) FROM edge_values") == "00FF"
    # Unusual table and column names exist, and the CJK ones are stored single-encoded (their UTF-8 bytes in HEX).
    for table in ("edge_values", "客戶備註", "group"):
        assert mysql_one(
            f"SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = 'dbaguy' AND TABLE_NAME = '{table}'"
        ) == 1
    assert mysql_one(
        "SELECT HEX(COLUMN_NAME) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = 'dbaguy' AND TABLE_NAME = '客戶備註' AND ORDINAL_POSITION = 1"
    ) == "E7B7A8E8999F"
    assert mysql_one("SELECT HEX(`內容`) FROM `客戶備註`") == "E6B8ACE8A9A6"
    assert mysql_one("SELECT `order` FROM `group`") == 42


@requires_mssql
def test_dev_mssql_is_seeded():
    conn = mssql_connect()
    try:

        def one(sql):
            conn.execute_query(sql)
            return [row[0] for row in conn.get_iterator(_mssql.ROW_FORMAT_TUPLE)][0]

        assert one("SELECT COUNT(*) FROM dbo.big_table") == 100_000
        assert one("SELECT COUNT(*) FROM analytics.dbo.page_views") == 50
        assert one("SELECT COUNT(*) FROM sys.procedures") == 3
        assert one("SELECT name FROM dbo.customers WHERE id = 2") == "林小明"
        assert one("SELECT legacy FROM dbo.types_demo WHERE id = 1") == "中文"  # a Chinese_Taiwan varchar column
        assert one("SELECT label FROM dbo.[brack]]et]") == "one"
        assert one("SELECT HAS_DBACCESS('locked_away')") == 0
        # CJK identifiers: the server stores the UTF-16 code units (checked as hex, so a symmetric client-side mix-up
        # cannot hide), and the names and the value come back intact.
        table = "OBJECT_ID(N'dbo.[客戶備註]')"
        hex_of_name = "CONVERT(varchar(100), CAST(name AS varbinary(100)), 1)"
        assert one(f"SELECT {hex_of_name} FROM sys.tables WHERE object_id = {table}") == (
            "0x" + "客戶備註".encode("utf-16-le").hex().upper()
        )
        assert one(f"SELECT name FROM sys.tables WHERE object_id = {table}") == "客戶備註"
        assert one(f"SELECT name FROM sys.columns WHERE object_id = {table} AND column_id = 1") == "編號"
        assert one(f"SELECT name FROM sys.columns WHERE object_id = {table} AND column_id = 2") == "內容"
        assert one("SELECT [內容] FROM dbo.[客戶備註] WHERE [編號] = 1") == "測試"
        # A reserved word as a table name.
        assert one("SELECT [order] FROM dbo.[group] WHERE [id] = 1") == 42
        # sql_variant: pymssql returns a plain variant as bytes (UTF-8 text for an nvarchar one), a CAST the value.
        base_type = "SELECT CAST(SQL_VARIANT_PROPERTY(v, 'BaseType') AS varchar(20)) FROM dbo.variant_cases WHERE id = "
        assert one("SELECT COUNT(*) FROM dbo.variant_cases") == 2
        assert one(base_type + "1") == "nvarchar"
        assert one(base_type + "2") == "int"
        assert one("SELECT CAST(v AS nvarchar(50)) FROM dbo.variant_cases WHERE id = 1") == "中文"
        assert one("SELECT v FROM dbo.variant_cases WHERE id = 1").decode("utf-8") == "中文"
        assert one("SELECT CAST(v AS int) FROM dbo.variant_cases WHERE id = 2") == 42
    finally:
        conn.close()


U_NUMBERS = (
    '{"big":9007199254740993,"i64max":9223372036854775807,"i64min":-9223372036854775808,'
    '"u64max":18446744073709551615,"beyond":12345678901234567890,"dec":1.10,'
    '"pi":3.141592653589793238462643383279,"sci":1E+3,"tiny":1e-400,"huge":1e400,"half":0.5}'
)


@requires_couchbase
def test_dev_couchbase_is_seeded():
    def one(statement, **kw):
        return couchbase_query(statement, **kw)["results"][0]

    assert one("SELECT RAW COUNT(*) FROM dbaguy.app.events") == 20_000
    assert one('SELECT RAW COUNT(*) FROM system:keyspaces AS k WHERE k.`bucket` = "dbaguy" AND k.`scope` = "many"') == 300
    assert one('SELECT RAW i.state FROM system:indexes AS i WHERE i.name = "idx_users_deferred"') == "deferred"
    assert one('SELECT RAW d FROM dbaguy.app.events AS d USE KEYS "e101000"')["late"]
    # Stored byte for byte, and passed through unchanged by SELECT RAW (survey §4)
    assert U_NUMBERS in couchbase_query_text('SELECT RAW d FROM dbaguy.app.users AS d USE KEYS "u_numbers"')
    types = couchbase_query('SELECT RAW META(d).type FROM dbaguy.app.blobs AS d USE KEYS ["bin1", "txt1", "j1"]')
    assert sorted(types["results"]) == ["base64", "base64", "json"]  # A20
    assert one('SELECT RAW META(d).expiration FROM dbaguy.app.users AS d USE KEYS "u_ttl"') == 4102444800  # A21
    assert one('SELECT RAW LENGTH(d.blob) FROM dbaguy.app.users AS d USE KEYS "u_big"') == 5 * 1024 * 1024
    # No `namespace` predicate (P35): every keyspace is in the one namespace `default` (the 7.6.12 rows carry both
    # `namespace` and `namespace_id`, A22).
    reader = couchbase_query("SELECT RAW k.name FROM system:keyspaces AS k", user=COUCHBASE_READER)
    assert reader["results"] == ["users"]  # the limited user sees only its own keyspace (survey §5.1)


@requires_couchbase
def test_request_active_follows_a_request():
    ident = "dbaguy-t1-active"
    conn = http.client.HTTPConnection(COUCHBASE_HOST, COUCHBASE_PORT, timeout=30)
    body = json.dumps({"statement": "SELECT RAW a * 1000 + b FROM ARRAY_RANGE(0, 100000) AS a "
                                    "UNNEST ARRAY_RANGE(0, 1000) AS b", "client_context_id": ident})
    conn.request("POST", "/query/service", body=body,
                 headers={"Authorization": _basic("dbaguy", "dbaguy"), "Content-Type": "application/json"})
    response = conn.getresponse()
    response.read1(1024)  # the request runs: rows are streaming, the client stops reading
    try:
        assert request_active(ident)
    finally:
        conn.close()  # the server stops the request once the connection goes
    wait_until(lambda: not request_active(ident), 10)


@pytest.mark.parametrize("name, key", [
    ("explain_covered", "plan"), ("explain_fetch", "plan"), ("explain_primary", "plan"),
    ("explain_sequential", "plan"), ("explain_intersect", "plan"), ("explain_join", "plan"),
    ("explain_subquery", "~subqueries"), ("advise_indexes", "advice"), ("advise_covering", "advice"),
    ("advise_none", "advice"), ("profile_timings", "executionTimings"), ("profile_join", "executionTimings"),
])
def test_plan_fixtures_hold_the_server_shapes(name, key):  # files only: no cluster needed
    assert key in json.loads(plan_fixture(name))


@requires_couchbase
def test_scratch_documents_are_stored_as_sent_and_removed():
    with scratch_documents() as keys:
        text, binary, expiring = scratch_key("t"), scratch_key("b"), scratch_key("x")
        keys += [text, binary, expiring]
        put_raw_document(text, '{"b": 1,\n "a": 1.10}')
        put_raw_document(binary, "not json")
        put_raw_document(expiring, '{"a":1}', expiry=2_000_000_000)
        assert stored_document(text).text == '{"b": 1,\n "a": 1.10}'
        assert stored_document(text).cas.isdigit() and stored_document(text).type == "json"
        assert stored_document(binary).type == "base64"
        assert stored_document(expiring).expiration == 2_000_000_000
    assert [stored_document(key) for key in (text, binary, expiring)] == [None, None, None]


@pytest.mark.parametrize("engine", ENGINES)
def test_scratch_objects_are_made_and_dropped_by_exact_name(engine, tmp_path):
    db = scratch_db(engine, tmp_path)
    try:
        with scratch(db) as make:
            table = items_table(db, make)
            assert table.startswith(SCRATCH_PREFIX + "table_")
            assert db.rows(f"SELECT id, label FROM {db.table(table)} ORDER BY id") == [(1, "a"), (2, "b"), (3, "c")]
            db.fill(db.table(table), 1000, 100)
            assert db.rows(f"SELECT COUNT(*) FROM {db.table(table)}") == [(1003,)]
            assert db.exists(table)
        assert not db.exists(table)  # gone from the catalog, asked on the same live connection
    finally:
        db.close()


# a one-row UPDATE that runs about half a second with no lock held, per engine (`{t}` table, `{o}` the order column)
SLOW_UPDATES = {
    "postgres": "UPDATE {t} SET {o} = 5 WHERE id = 1 AND (SELECT pg_sleep(0.5)) IS NOT NULL",
    "mysql": "UPDATE {t} SET {o} = 5 WHERE id = 1 AND SLEEP(0.5) = 0",
    "mssql": "WAITFOR DELAY '00:00:00.500'; UPDATE {t} SET {o} = 5 WHERE id = 1",
}


@pytest.mark.parametrize("engine", SERVER_ENGINES)
def test_waiting_is_a_lock_wait_on_the_tables_own_statements(engine, tmp_path):
    db, other_connection = scratch_db(engine, tmp_path), scratch_db(engine, tmp_path)
    try:
        with scratch(db) as make:
            table, unrelated = items_table(db, make), make("TABLE", f"CREATE TABLE {{t}} (id {db.bigint} PRIMARY KEY)")
            update = f"UPDATE {db.table(table)} SET {db.q('order')} = 99 WHERE id = 1"
            results: list[BaseException | None] = []

            def run(sql):
                try:
                    other_connection.execute(sql)
                    results.append(None)
                except BaseException as exc:  # reported by the assert below
                    results.append(exc)

            assert not db.waiting(table)
            # a short statement that holds no lock is not a wait, however many times it is polled
            slow = SLOW_UPDATES[engine].format(t=db.table(table), o=db.q("order"))
            thread = threading.Thread(target=run, args=(slow,))
            thread.start()
            polls = []
            while thread.is_alive():
                polls.append(db.waiting(table))
                time.sleep(0.02)
            thread.join()
            assert results == [None] and polls and not any(polls)
            # a statement blocked by a row lock is, for its own table only
            results.clear()
            with db.holding(update):
                thread = threading.Thread(target=run, args=(update,))
                thread.start()
                wait_until(lambda: db.waiting(table), scaled(10))  # asserts when the wait is never seen
                assert not db.waiting(unrelated)
            thread.join(scaled(15))
            assert results == [None] and not db.waiting(table)  # released with the lock, and done
    finally:
        other_connection.close()
        db.close()
