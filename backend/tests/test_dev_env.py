import mysql.connector
import psycopg

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


@requires_mysql
def test_dev_mysql_is_seeded():
    conn = mysql.connector.connect(**mysql_driver_kwargs())
    try:
        cursor = conn.cursor()

        def one(sql):
            cursor.execute(sql)
            return cursor.fetchone()[0]

        assert one("SELECT COUNT(*) FROM big_table") == 100_000
        assert one("SELECT COUNT(*) FROM analytics.page_views") == 50
        assert one("SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = 'dbaguy'") == 3
        assert one("SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA = 'dbaguy'") == 1
        assert one("SELECT CAST(born AS CHAR) FROM types_demo WHERE id = 1") == "0000-00-00"
        assert one("SELECT label FROM `tick``table`") == "one"
    finally:
        conn.close()
