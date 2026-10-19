"""Shared helpers for tests that need the dev databases from dev/docker-compose.yml."""

import base64
import http.client
import json
import os
import socket
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple
from uuid import uuid4

import mysql.connector
import psycopg
import pytest
from pymssql import _mssql

from dbaguy_backend.domain.profiles import ServerProfile, SslConfig
from dbaguy_backend.sql.dialects import keyspace_path

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
COUCHBASE_HOST = "127.0.0.1"
COUCHBASE_PORT = int(os.environ.get("DBAGUY_TEST_COUCHBASE_PORT", "8093"))
COUCHBASE_TLS_PORT = 18093
COUCHBASE_MANAGER_PORT = 8091
COUCHBASE_USER = "dbaguy"
COUCHBASE_PASSWORD = "dbaguy"
COUCHBASE_READER = "dbaguy_reader"  # query_select + data_reader on dbaguy.app.users only
# Minutes of server work and no row until the end: what a cancel must stop (spec §1 success criteria).
LONG_AGGREGATE = "SELECT COUNT(*) AS n FROM ARRAY_RANGE(0, 100000) AS a UNNEST ARRAY_RANGE(0, 100000) AS b"
BASTION_PORT = 2222
SKIP_REASON = "dev databases not running: docker compose -f dev/docker-compose.yml up -d --build --wait"


LOAD_FACTOR_ENV = "DBAGUY_TEST_LOAD_FACTOR"
MAX_LOAD_FACTOR = 6.0


def load_factor() -> float:
    """How much slower than an idle machine the timing tests allow the machine to be right now: 1.0 (idle) up to
    MAX_LOAD_FACTOR. It is the 1-minute load average per core (so it lags a sudden burst by a little), or the value of
    DBAGUY_TEST_LOAD_FACTOR when that is set (to force a factor, or 1 to get the idle bounds back)."""
    override = os.environ.get(LOAD_FACTOR_ENV)
    try:
        value = float(override) if override else os.getloadavg()[0] / (os.cpu_count() or 1)
    except (ValueError, OSError):
        value = 1.0
    return min(MAX_LOAD_FACTOR, max(1.0, value))


def scaled(seconds: float) -> float:
    """A hard deadline (an upper bound: "this must end within") stretched by the machine's load. Only for upper bounds
    that guard against a hang or a missing cancel (a 30 s WAITFOR, 10^10 rows, a stuck thread), which stay far above the
    stretched value; never for a lower bound (a wait the product promises) or a product constant such as WEDGE_S."""
    return seconds * load_factor()


def unique_name(prefix: str) -> str:
    """`prefix` with a random suffix: an object a test creates on the shared dev server (a global temp table, a table,
    a procedure). Several test runs share one server (an agent's run beside another's), and a fixed name lets one run
    drop or lock what another is in the middle of using."""
    return f"{prefix}_{uuid4().hex[:8]}"


def wait_until(predicate, timeout: float, interval: float = 0.01) -> bool:
    """Polls `predicate()` until it is true (True) or `timeout` seconds have passed (False). `timeout` is used as given:
    pass scaled(...) for a deadline."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
    return True


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
    """A stored procedure in the dev `dbaguy` database for one test, dropped afterwards. `name` starts with `zz_`; the
    procedure gets a random suffix (the yielded name), so concurrent runs never share one."""
    assert name.startswith("zz_")
    name = unique_name(name)
    owner = mysql.connector.connect(**mysql_driver_kwargs(autocommit=True))
    try:
        owner.cmd_query(f"DROP PROCEDURE IF EXISTS {name}")
        owner.cmd_query(f"CREATE PROCEDURE {name}() BEGIN {body} END")
        yield name
    finally:
        owner.cmd_query(f"DROP PROCEDURE IF EXISTS {name}")
        owner.close()


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def couchbase_query_text(
    statement: str,
    params: dict[str, Any] | None = None,
    *,
    user: str = COUCHBASE_USER,
    password: str = COUCHBASE_PASSWORD,
    timeout: float = 60.0,
) -> str:
    """One SQL++ request to the dev cluster's query service, answered as the raw response text. `timeout` (seconds) is
    the HTTP timeout: the import-time readiness check uses a short one (P48)."""
    conn = http.client.HTTPConnection(COUCHBASE_HOST, COUCHBASE_PORT, timeout=timeout)
    try:
        body = json.dumps({"statement": statement, **(params or {})})
        conn.request(
            "POST",
            "/query/service",
            body=body,
            headers={"Authorization": _basic(user, password), "Content-Type": "application/json"},
        )
        return conn.getresponse().read().decode("utf-8")
    finally:
        conn.close()


def couchbase_query(statement: str, params: dict[str, Any] | None = None, **options: Any) -> dict[str, Any]:
    """`options`: `user`, `password`, `timeout` of couchbase_query_text."""
    return json.loads(couchbase_query_text(statement, params, **options), parse_float=Decimal)


def couchbase_ca_file(directory: Path) -> Path:
    """The cluster's CA certificates, fetched now (none is committed), as a PEM file in `directory`."""
    conn = http.client.HTTPConnection(COUCHBASE_HOST, COUCHBASE_MANAGER_PORT, timeout=10)
    try:
        conn.request(
            "GET", "/pools/default/trustedCAs", headers={"Authorization": _basic(COUCHBASE_USER, COUCHBASE_PASSWORD)}
        )
        cas = json.loads(conn.getresponse().read())
    finally:
        conn.close()
    path = directory / "couchbase-ca.pem"
    path.write_text("".join(ca["pem"] for ca in cas))
    return path


def completed_request_state(client_context_id: str) -> str | None:
    """How the server ended a request (`closed`, `stopped`, `completed`, …), from system:completed_requests. The log
    holds requests over 1 s (and failed ones), and an entry can take a moment to appear: polled for up to 3 s."""
    statement = "SELECT RAW r.state FROM system:completed_requests AS r WHERE r.clientContextID = $id"
    for _ in range(30):
        found = couchbase_query(statement, {"$id": client_context_id})["results"]
        if found:
            return found[0]
        time.sleep(0.1)
    return None


PLAN_FIXTURES = Path(__file__).parent / "fixtures" / "couchbase"


def plan_fixture(name: str) -> str:
    """A plan, advice or profile text as the dev cluster sent it (captured by 04b Task 1)."""
    return (PLAN_FIXTURES / f"{name}.json").read_text(encoding="utf-8")


def request_active(client_context_id: str) -> bool:
    """Whether the query service is running that request now (system:active_requests, admin)."""
    statement = "SELECT RAW r.state FROM system:active_requests AS r WHERE r.clientContextID = $id"
    return bool(couchbase_query(statement, {"$id": client_context_id})["results"])


SCRATCH_BUCKET = "dbaguy"
SCRATCH_SCOPE = "app"
SCRATCH_COLLECTION = "orders"
SCRATCH_PREFIX = "zz_04c_"  # a test writes only keys with this prefix, and only in app.orders (never app.users)
_KEY_GONE = "Key doesn't exist"  # the docs endpoint's 400 for a key that is already gone (7.6.12)


def scratch_key(tag: str = "") -> str:
    return SCRATCH_PREFIX + tag + uuid4().hex[:10]


def _docs_request(method: str, key: str, scope: str, collection: str, body: str | None = None) -> tuple[int, str]:
    """One request to the cluster manager's documents endpoint (what the web console uses): (status, body text)."""
    path = (
        f"/pools/default/buckets/{SCRATCH_BUCKET}/scopes/{scope}/collections/{collection}"
        f"/docs/{urllib.parse.quote(key, safe='')}"
    )
    headers = {"Authorization": _basic(COUCHBASE_USER, COUCHBASE_PASSWORD)}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn = http.client.HTTPConnection(COUCHBASE_HOST, COUCHBASE_MANAGER_PORT, timeout=30)
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        conn.close()


def put_raw_document(
    key: str,
    text: str,
    *,
    expiry: int | None = None,
    scope: str = SCRATCH_SCOPE,
    collection: str = SCRATCH_COLLECTION,
) -> None:
    """Stores `text` byte for byte as the document `key` (SQL++ would normalise it). A text that is not JSON becomes a
    binary document (`META().type` `base64`). `expiry`: an absolute Unix time, at most 2147483647 (A31)."""
    form = {"value": text} | ({} if expiry is None else {"expiry": str(expiry)})
    status, answer = _docs_request("POST", key, scope, collection, urllib.parse.urlencode(form))
    assert status == 200, f"storing {key!r} answered {status}: {answer}"


def delete_documents(*keys: str, scope: str = SCRATCH_SCOPE, collection: str = SCRATCH_COLLECTION) -> None:
    """Removes the documents by exact key. Every key is tried before the one assertion, so a failure never leaves the
    later keys behind. A key that is already gone answers 400 `Key doesn't exist`: fine; a 404 means the collection is
    missing."""
    failed = []
    for key in keys:
        status, answer = _docs_request("DELETE", key, scope, collection)
        if status != 200 and not (status == 400 and _KEY_GONE in answer):
            failed.append(f"{key!r}: {status} {answer}")
    assert not failed, "removing failed for " + "; ".join(failed)


class StoredDocument(NamedTuple):
    text: str  # the stored text exactly as the query service sends it
    cas: str  # the decimal text of the CAS
    expiration: int
    type: str  # `json` or `base64`


def stored_document(
    key: str, *, scope: str = SCRATCH_SCOPE, collection: str = SCRATCH_COLLECTION
) -> StoredDocument | None:
    """The document `key` as the store reads it (04c: one SELECT … USE KEYS), or None when there is none."""
    from dbaguy_backend.adapters.couchbase.stream import FieldEvent, ResponseParser, RowEvent

    statement = (
        "SELECT META(d).cas AS cas, META(d).expiration AS e, META(d).type AS t, d "
        f"FROM {keyspace_path(SCRATCH_BUCKET, scope, collection)} AS d USE KEYS $k"
    )
    parser = ResponseParser()
    events = parser.feed(couchbase_query_text(statement, {"$k": key}).encode()) + parser.finish()
    errors = [event.value for event in events if isinstance(event, FieldEvent) and event.name == "errors"]
    assert not errors, f"reading {key!r} failed: {errors}"
    row = next((event.row for event in events if isinstance(event, RowEvent)), None)
    if row is None:
        return None
    fields = {member.key: member for member in row.fields}
    return StoredDocument(fields["d"].text, fields["cas"].text, int(fields["e"].text), json.loads(fields["t"].text))


@contextmanager
def scratch_documents() -> Iterator[list[str]]:
    """The list a test appends its scratch keys to; whatever happens, they are removed by exact key afterwards."""
    keys: list[str] = []
    try:
        yield keys
    finally:
        delete_documents(*keys)


def _couchbase_ready() -> bool:
    if not _port_open(COUCHBASE_PORT):
        return False
    try:
        # A short timeout (P48): this runs at import, and a slow query service must not stall every pytest start for 60 s
        done = couchbase_query('SELECT RAW COUNT(*) FROM dbaguy._default._default USE KEYS "seed_done"', timeout=5)
        return done.get("results") == [1]
    except (OSError, ValueError, http.client.HTTPException):
        return False


PG_AVAILABLE = _pg_ready()
MYSQL_AVAILABLE = _mysql_ready()
MSSQL_AVAILABLE = _mssql_ready()
COUCHBASE_AVAILABLE = _couchbase_ready()
BASTION_AVAILABLE = _port_open(BASTION_PORT)
requires_pg = pytest.mark.skipif(not PG_AVAILABLE, reason=SKIP_REASON)
requires_mysql = pytest.mark.skipif(not MYSQL_AVAILABLE, reason=SKIP_REASON)
requires_bastion = pytest.mark.skipif(not (PG_AVAILABLE and BASTION_AVAILABLE), reason=SKIP_REASON)
requires_mysql_bastion = pytest.mark.skipif(not (MYSQL_AVAILABLE and BASTION_AVAILABLE), reason=SKIP_REASON)
requires_mssql = pytest.mark.skipif(not MSSQL_AVAILABLE, reason=SKIP_REASON)
requires_mssql_bastion = pytest.mark.skipif(not (MSSQL_AVAILABLE and BASTION_AVAILABLE), reason=SKIP_REASON)
requires_couchbase = pytest.mark.skipif(not COUCHBASE_AVAILABLE, reason=SKIP_REASON)
requires_couchbase_bastion = pytest.mark.skipif(not (COUCHBASE_AVAILABLE and BASTION_AVAILABLE), reason=SKIP_REASON)


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


def make_couchbase_profile(**overrides) -> ServerProfile:
    data = {
        "id": uuid4(),
        "name": "dev couchbase",
        "engine": "couchbase",
        "host": COUCHBASE_HOST,
        "port": None,  # the default query port: node discovery runs (D-04-6)
        "database": "dbaguy.app",
        "username": COUCHBASE_USER,
        "ssl": SslConfig(mode="disable"),
    }
    data.update(overrides)
    return ServerProfile(**data)
