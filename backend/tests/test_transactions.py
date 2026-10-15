import sqlite3
from uuid import uuid4

import pytest

from dbaguy_backend.adapters.base import Endpoint, StatementError
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.domain.profiles import SqliteProfile
from tests.support import PG_HOST, PG_PASSWORD, PG_PORT, make_pg_profile, requires_pg


def drain(result):
    rows = []
    while chunk := result.fetch(500):
        rows += chunk
    result.close()
    return rows


def scalar(conn, sql):
    return drain(conn.run(sql))[0][0]


def test_sqlite_manual_commit_mode(tmp_path):
    db = tmp_path / "t.db"
    sqlite3.connect(db).executescript("CREATE TABLE t (id INTEGER);")
    conn = SqliteAdapter().open(SqliteProfile(id=uuid4(), name="t", engine="sqlite", path=str(db)), None, None, None)
    try:
        assert conn.autocommit()
        conn.set_autocommit(False)
        assert not conn.autocommit()
        drain(conn.run("INSERT INTO t VALUES (1)"))
        assert conn.in_transaction()
        with pytest.raises(StatementError) as exc:
            conn.set_autocommit(True)
        assert exc.value.code == "transaction_open"
        conn.rollback()
        assert not conn.in_transaction() and scalar(conn, "SELECT count(*) FROM t") == 0
        drain(conn.run("INSERT INTO t VALUES (2)"))
        conn.commit()
        assert scalar(conn, "SELECT max(id) FROM t") == 2
        conn.commit()  # nothing open: a no-op
        conn.set_autocommit(True)
        assert conn.autocommit()
    finally:
        conn.close()


@requires_pg
def test_postgres_manual_commit_mode():
    conn = PostgresAdapter().open(make_pg_profile(), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)
    try:
        drain(conn.run("CREATE TEMP TABLE tx_probe (id int)"))
        conn.set_autocommit(False)
        drain(conn.run("INSERT INTO tx_probe VALUES (1)"))
        assert conn.in_transaction()
        with pytest.raises(StatementError) as exc:
            conn.set_autocommit(True)
        assert exc.value.code == "transaction_open"
        conn.rollback()
        assert int(scalar(conn, "SELECT count(*) FROM tx_probe")) == 0
        drain(conn.run("INSERT INTO tx_probe VALUES (2)"))
        conn.commit()
        assert int(scalar(conn, "SELECT max(id) FROM tx_probe")) == 2
        conn.rollback()  # the SELECT above opened a transaction in manual mode

        # A partly read large result in manual mode must not abort the transaction (no streaming here).
        result = conn.run("SELECT g FROM generate_series(1, 200000) g")
        assert len(result.fetch(10)) == 10
        result.close()
        assert conn.in_transaction() and not conn.transaction_failed()
        conn.rollback()
        conn.set_autocommit(True)
        assert conn.autocommit() and not conn.in_transaction()
    finally:
        conn.close()


@requires_pg
def test_postgres_read_only_connection_stays_read_only_in_manual_mode():
    conn = PostgresAdapter().open(make_pg_profile(read_only=True), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)
    try:
        conn.set_autocommit(False)
        # Hidden in a function, the session default is switched off and committed; the next BEGIN must still be READ ONLY.
        drain(conn.run("SELECT set_config('default_transaction_read_only', 'off', false)"))
        conn.commit()
        with pytest.raises(StatementError) as exc:
            drain(conn.run("CREATE TEMP TABLE ro_probe (id int)"))
        assert exc.value.code == "25006"  # read_only_sql_transaction
        conn.rollback()
    finally:
        conn.close()
