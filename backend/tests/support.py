"""Shared helpers for tests that need the dev databases from dev/docker-compose.yml."""

import os
import socket
from uuid import uuid4

import psycopg
import pytest

from dbaguy_backend.domain.profiles import ServerProfile, SslConfig

PG_HOST = "127.0.0.1"
PG_PORT = int(os.environ.get("DBAGUY_TEST_PG_PORT", "54317"))
PG_USER = "dbaguy"
PG_PASSWORD = "dbaguy"
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


PG_AVAILABLE = _pg_ready()
BASTION_AVAILABLE = _port_open(BASTION_PORT)
requires_pg = pytest.mark.skipif(not PG_AVAILABLE, reason=SKIP_REASON)
requires_bastion = pytest.mark.skipif(not (PG_AVAILABLE and BASTION_AVAILABLE), reason=SKIP_REASON)


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
