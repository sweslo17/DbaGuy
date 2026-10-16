import sqlite3
from uuid import uuid4

import pytest

from dbaguy_backend.adapters.base import FAILED_COMMIT_MESSAGE, Endpoint, StatementError
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.domain.execution import MessageSeverity, ServerMessage
from dbaguy_backend.domain.profiles import SqliteProfile
from dbaguy_backend.services.safety import GuardAction, SafetyPolicy
from dbaguy_backend.sql.dialects import Dialect
from tests.support import PG_HOST, PG_PASSWORD, PG_PORT, make_pg_profile, requires_pg
from tests.test_safety import HARMLESS_SETTINGS


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


@pytest.fixture
def sqlite_manual(tmp_path):
    """A SQLite connection in manual-commit mode on a database that holds one empty table `t`."""
    db = tmp_path / "t.db"
    sqlite3.connect(db).executescript("CREATE TABLE t (id INTEGER);")
    conn = SqliteAdapter().open(SqliteProfile(id=uuid4(), name="t", engine="sqlite", path=str(db)), None, None, None)
    conn.set_autocommit(False)
    yield conn
    conn.close()


def test_sqlite_manual_mode_rolls_back_dml_whatever_its_first_word(sqlite_manual):
    conn = sqlite_manual
    drain(conn.run("WITH x AS (SELECT 9 AS i) INSERT INTO t SELECT i FROM x"))
    assert conn.in_transaction()
    conn.rollback()
    assert scalar(conn, "SELECT count(*) FROM t") == 0


def test_sqlite_manual_mode_rolls_back_ddl(sqlite_manual):
    conn = sqlite_manual
    drain(conn.run("CREATE TABLE z (id INTEGER)"))
    assert conn.in_transaction()
    conn.rollback()
    assert scalar(conn, "SELECT count(*) FROM sqlite_master WHERE name = 'z'") == 0


def test_sqlite_manual_mode_select_opens_a_transaction(sqlite_manual):
    conn = sqlite_manual
    assert scalar(conn, "SELECT 1") == 1
    assert conn.in_transaction()
    with pytest.raises(StatementError) as exc:
        conn.set_autocommit(True)
    assert exc.value.code == "transaction_open"
    conn.commit()
    assert not conn.in_transaction()
    conn.commit()  # nothing open: a no-op
    conn.set_autocommit(True)
    assert conn.autocommit()


def test_sqlite_manual_mode_runs_statements_that_cannot_be_inside_a_transaction(sqlite_manual):
    conn = sqlite_manual
    drain(conn.run("VACUUM"))
    drain(conn.run("-- compact\nVACUUM"))
    assert not conn.in_transaction()
    assert scalar(conn, "PRAGMA user_version") == 0
    assert not conn.in_transaction()


def test_sqlite_manual_mode_leaves_the_users_own_transaction_control_alone(sqlite_manual):
    conn = sqlite_manual
    drain(conn.run("BEGIN"))  # no second BEGIN in front of it
    drain(conn.run("INSERT INTO t VALUES (1)"))
    assert conn.in_transaction()
    drain(conn.run("COMMIT"))
    assert not conn.in_transaction()
    conn.rollback()
    assert scalar(conn, "SELECT count(*) FROM t") == 1


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


@requires_pg
def test_postgres_set_autocommit_on_a_dead_connection_is_not_reported_as_an_open_transaction():
    conn = PostgresAdapter().open(make_pg_profile(), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)
    conn._conn.close()
    with pytest.raises(StatementError) as exc:
        conn.set_autocommit(False)
    assert exc.value.code != "transaction_open"


@requires_pg
@pytest.mark.parametrize("ending", ["COMMIT", "end", "COMMIT; SELECT 1"])
def test_postgres_typed_commit_of_an_aborted_transaction_warns_that_it_rolled_back(ending):
    conn = PostgresAdapter().open(make_pg_profile(), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)
    try:
        drain(conn.run("CREATE TEMP TABLE commit_probe (id int)"))  # gone with the connection
        drain(conn.run("BEGIN"))
        drain(conn.run("INSERT INTO commit_probe VALUES (1)"))
        with pytest.raises(StatementError):
            drain(conn.run("SELECT 1/0"))
        assert conn.transaction_failed()
        result = conn.run(ending)
        assert result.take_messages() == [
            ServerMessage(severity=MessageSeverity.WARNING, text=FAILED_COMMIT_MESSAGE),
        ]
        result.close()
        assert not conn.in_transaction() and int(scalar(conn, "SELECT count(*) FROM commit_probe")) == 0
        drain(conn.run("BEGIN"))  # a healthy transaction commits without the warning
        drain(conn.run("INSERT INTO commit_probe VALUES (2)"))
        result = conn.run(ending)
        assert result.take_messages() == []
        result.close()
        assert int(scalar(conn, "SELECT count(*) FROM commit_probe")) == 1
    finally:
        conn.close()


@requires_pg
@pytest.mark.parametrize("autocommit", [True, False])
def test_postgres_read_only_connection_runs_the_harmless_settings_and_stays_read_only(autocommit):
    conn = PostgresAdapter().open(make_pg_profile(read_only=True), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)
    try:
        conn.set_autocommit(autocommit)
        for sql in HARMLESS_SETTINGS:
            assert SafetyPolicy().check(sql, Dialect.POSTGRES, "standard", read_only=True).action == GuardAction.RUN
            drain(conn.run(sql))
        assert scalar(conn, "SHOW transaction_read_only") == "on"
        conn.rollback()
    finally:
        conn.close()
