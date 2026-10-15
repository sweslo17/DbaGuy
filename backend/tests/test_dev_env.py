import psycopg

from tests.support import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, requires_pg


@requires_pg
def test_dev_postgres_is_seeded():
    with psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy") as conn:
        assert conn.execute("SELECT count(*) FROM big_table").fetchone()[0] == 100_000
        assert conn.execute("SELECT count(*) FROM pg_database WHERE datname = 'analytics'").fetchone()[0] == 1
