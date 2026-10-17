import mysql.connector
import psycopg
import pytest

from tests.support import (
    PG_HOST,
    PG_PASSWORD,
    PG_PORT,
    PG_USER,
    mysql_driver_kwargs,
    requires_mysql,
    requires_pg,
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
