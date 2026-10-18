"""Shared helpers for tests that need the dev databases from dev/docker-compose.yml."""

import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import mysql.connector
import psycopg
import pytest
from pymssql import _mssql

from dbaguy_backend.domain.profiles import ServerProfile, SslConfig

PG_HOST = "127.0.0.1"
PG_PORT = int(os.environ.get("DBAGUY_TEST_PG_PORT", "54317"))
PG_USER = "dbaguy"
PG_PASSWORD = "dbaguy"
MYSQL_HOST = "127.0.0.1"
MYSQL_PORT = int(os.environ.get("DBAGUY_TEST_MYSQL_PORT", "33067"))
MYSQL_USER = "dbaguy"
MYSQL_PASSWORD = "dbaguy"
MSSQL_HOST = "127.0.0.1"
MSSQL_PORT = int(os.environ.get("DBAGUY_TEST_MSSQL_PORT", "14337"))
MSSQL_USER = "dbaguy"
MSSQL_PASSWORD = "Dbaguy_dev1"
MSSQL_SA_PASSWORD = "Dbaguy_dev1"
BASTION_PORT = 2222
SKIP_REASON = "dev databases not running: docker compose -f dev/docker-compose.yml up -d --build --wait"


def _port_open(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def _pg_ready() -> bool:
    if not _port_open(PG_PORT):
        return False
    try:
        psycopg.connect(
            host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy", connect_timeout=3
        ).close()
        return True
    except psycopg.Error:
        return False


def mysql_driver_kwargs(**overrides) -> dict:
    """Keyword arguments for mysql.connector.connect against the dev MySQL.

    TLS stays enabled (never ssl_disabled): the TLS login fills the server's caching_sha2_password cache, which is what
    lets the `ssl disable` test profiles log in over plain TCP after a server restart.
    """
    kwargs = {
        "host": MYSQL_HOST,
        "port": MYSQL_PORT,
        "user": MYSQL_USER,
        "password": MYSQL_PASSWORD,
        "database": "dbaguy",
        "use_pure": True,
        "connection_timeout": 3,
    }
    kwargs.update(overrides)
    return kwargs


def _mysql_ready() -> bool:
    if not _port_open(MYSQL_PORT):
        return False
    try:
        mysql.connector.connect(**mysql_driver_kwargs()).close()
        return True
    except (mysql.connector.Error, OSError):
        return False


def mssql_driver_kwargs(**overrides) -> dict:
    """Keyword arguments for pymssql._mssql.connect against the dev SQL Server (port is a str, as the driver wants)."""
    kwargs = {
        "server": MSSQL_HOST,
        "port": str(MSSQL_PORT),
        "user": MSSQL_USER,
        "password": MSSQL_PASSWORD,
        "database": "dbaguy",
        "appname": "DbaGuy tests",
        "tds_version": "7.4",
    }
    kwargs.update(overrides)
    return kwargs


def mssql_connect(**overrides) -> _mssql.MSSQLConnection:
    """A connection to the dev SQL Server. The login timeout is a pymssql module global, so it is set on every call."""
    _mssql.login_timeout = 5
    return _mssql.connect(**mssql_driver_kwargs(**overrides))


def _mssql_ready() -> bool:
    if not _port_open(MSSQL_PORT):
        return False
    try:
        mssql_connect().close()
        return True
    except (_mssql.MSSQLException, OSError):
        return False


def kill_mssql_session(spid: int) -> None:
    """Kills a session of the dev SQL Server as `sa`; the dbaguy login has no ALTER ANY CONNECTION."""
    conn = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database="master")
    try:
        conn.execute_non_query(f"KILL {int(spid)}")
    finally:
        conn.close()


@contextmanager
def mysql_procedure(name: str, body: str) -> Iterator[str]:
    """A stored procedure in the dev `dbaguy` database for one test, dropped afterwards. `name` starts with `zz_`."""
    assert name.startswith("zz_")
    owner = mysql.connector.connect(**mysql_driver_kwargs(autocommit=True))
    try:
        owner.cmd_query(f"DROP PROCEDURE IF EXISTS {name}")
        owner.cmd_query(f"CREATE PROCEDURE {name}() BEGIN {body} END")
        yield name
    finally:
        owner.cmd_query(f"DROP PROCEDURE IF EXISTS {name}")
        owner.close()


PG_AVAILABLE = _pg_ready()
MYSQL_AVAILABLE = _mysql_ready()
MSSQL_AVAILABLE = _mssql_ready()
BASTION_AVAILABLE = _port_open(BASTION_PORT)
requires_pg = pytest.mark.skipif(not PG_AVAILABLE, reason=SKIP_REASON)
requires_mysql = pytest.mark.skipif(not MYSQL_AVAILABLE, reason=SKIP_REASON)
requires_bastion = pytest.mark.skipif(not (PG_AVAILABLE and BASTION_AVAILABLE), reason=SKIP_REASON)
requires_mysql_bastion = pytest.mark.skipif(not (MYSQL_AVAILABLE and BASTION_AVAILABLE), reason=SKIP_REASON)
requires_mssql = pytest.mark.skipif(not MSSQL_AVAILABLE, reason=SKIP_REASON)
requires_mssql_bastion = pytest.mark.skipif(not (MSSQL_AVAILABLE and BASTION_AVAILABLE), reason=SKIP_REASON)


def make_pg_profile(**overrides) -> ServerProfile:
    data = {
        "id": uuid4(),
        "name": "dev pg",
        "engine": "postgres",
        "host": PG_HOST,
        "port": PG_PORT,
        "database": "dbaguy",
        "username": PG_USER,
        "ssl": SslConfig(mode="disable"),
    }
    data.update(overrides)
    return ServerProfile(**data)


def make_mysql_profile(**overrides) -> ServerProfile:
    data = {
        "id": uuid4(),
        "name": "dev mysql",
        "engine": "mysql",
        "host": MYSQL_HOST,
        "port": MYSQL_PORT,
        "database": "dbaguy",
        "username": MYSQL_USER,
        "ssl": SslConfig(mode="disable"),
    }
    data.update(overrides)
    return ServerProfile(**data)


def make_mssql_profile(**overrides) -> ServerProfile:
    data = {
        "id": uuid4(),
        "name": "dev mssql",
        "engine": "mssql",
        "host": MSSQL_HOST,
        "port": MSSQL_PORT,
        "database": "dbaguy",
        "username": MSSQL_USER,
        "ssl": SslConfig(mode="prefer"),
    }
    data.update(overrides)
    return ServerProfile(**data)
