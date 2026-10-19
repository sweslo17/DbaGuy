"""The Couchbase adapter against the dev cluster (7.6.12 EE): connect and identify, exact cells, errors with positions,
an error after rows, DML / DDL, parameters and query context, no transactions, read-only enforced by the server,
cancel and close at every moment (Review Focus 2, 3, 5), TLS and the SSH bastion."""

import threading
import time
import uuid

import pytest

from dbaguy_backend.adapters.base import Endpoint, RunOptions, StatementCancelled, StatementError
from dbaguy_backend.adapters.couchbase import adapter as couchbase_adapter
from dbaguy_backend.adapters.couchbase.adapter import CouchbaseAdapter
from dbaguy_backend.adapters.couchbase.result import STOPPED_ON_THE_SERVER
from dbaguy_backend.adapters.couchbase.rows import OVERFLOW_NAME
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.values import MISSING, JsonText as J, encode_row
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.container import default_registry
from dbaguy_backend.domain.errors import ConnectionFailed, ValidationFailed
from dbaguy_backend.domain.execution import QueryContext, StatementParams
from dbaguy_backend.domain.metadata import NodeKind, PathSegment
from dbaguy_backend.domain.profiles import SshConfig, SslConfig
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.profile_service import ProfileService, ResolvedSecrets
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.support import (
    BASTION_PORT, COUCHBASE_MANAGER_PORT, COUCHBASE_PASSWORD, COUCHBASE_PORT, COUCHBASE_READER, LONG_AGGREGATE,
    completed_request_state, couchbase_ca_file, couchbase_query, make_couchbase_profile, requires_couchbase,
    requires_couchbase_bastion, scaled,
)
from tests.test_dev_env import U_NUMBERS
from tests.test_ssh_tunnel import TEST_SSH_OPTIONS

pytestmark = requires_couchbase
APP = QueryContext(bucket="dbaguy", scope="app")
U_KEYORDER = '{"zeta": 1,\n "alpha": {"z": 1, "a": 2}, "ws" :  [ 1 , 2 ], "mid": "a b"}'
A11_SAN_HAS_LOOPBACK = True  # plan, assumptions table A11: the dev 18093 certificate's SAN is IP 127.0.0.1
CANCEL_WITHIN_S = 1.0  # spec §1: a cancel ends a long aggregate within a second
# Many rows, streamed as fast as the server can: what a cancel or a close stops part-way
STREAM = "SELECT RAW a * 1000 + b FROM ARRAY_RANGE(0, 100000) AS a UNNEST ARRAY_RANGE(0, 1000) AS b"


def open_couchbase(password=COUCHBASE_PASSWORD, **overrides):
    profile = make_couchbase_profile(**overrides)
    adapter = CouchbaseAdapter()
    host, port = adapter.endpoint_target(profile)
    return adapter.open(profile, Endpoint(host, port), password, None)


@pytest.fixture
def connection():
    conn = open_couchbase()
    yield conn
    conn.close()


def read(connection, sql, options=None):
    """(column names, rows, message texts) of one statement, read to the end."""
    result = connection.run(sql, options)
    try:
        columns = result.columns()
        rows = []
        while columns is not None and (batch := result.fetch(500)):
            rows += batch
        result.next_result()
        return [c.name for c in columns or []], rows, [m.text for m in result.take_messages()]
    finally:
        result.close()


def cancel_later(connection, after_s: float) -> tuple[threading.Timer, list[float]]:
    """A cancel from another thread after `after_s`, started BEFORE run(): run() waits for the status line, which a
    long aggregate sends only once it has its first output (R-04a-C1). The list gets the moment it was delivered."""
    delivered: list[float] = []

    def cancel() -> None:
        delivered.append(time.monotonic())
        connection.cancel()

    timer = threading.Timer(after_s, cancel)
    timer.start()
    return timer, delivered


def test_connects_and_identifies(connection):
    info = connection.server_info()
    assert info.version.startswith("Couchbase Server 7.6.12-") and info.edition == "enterprise"
    assert "dbaguy" in (info.current_user or "")
    assert (info.current_database, info.default_query_context) == ("dbaguy.app", APP)
    assert connection.current_database() == "dbaguy.app"
    assert (connection.in_transaction(), connection.autocommit(), connection.transaction_failed()) == (False, True, False)


def test_a_limited_user_connects():  # A17
    conn = open_couchbase(username=COUCHBASE_READER)
    try:
        assert COUCHBASE_READER in (conn.server_info().current_user or "")
    finally:
        conn.close()


def test_wrong_password_and_unreachable_nodes():
    with pytest.raises(ConnectionFailed, match="Check the user name and password") as error:
        open_couchbase(password="wrong")
    assert "authenticate" in error.value.message  # the server's own text first (2120 on 7.6.12, A14)
    with pytest.raises(ConnectionFailed, match="Check the user name and password"):
        open_couchbase(username=COUCHBASE_READER, password="wrong")
    with pytest.raises(ConnectionFailed, match=r"Could not reach a query node: 127\.0\.0\.1:1 \(refused\)"):
        open_couchbase(port=1)
    conn = open_couchbase(host="cb-nowhere.invalid,127.0.0.1")  # discovery skips the dead seed
    conn.close()
    conn = open_couchbase(host="cb-nowhere.invalid,127.0.0.1", options={"discover_nodes": "false"})  # the next seed
    conn.close()


def test_stored_numbers_reach_the_cells_exactly(connection):  # Review Focus 1
    names, rows, _ = read(connection, 'SELECT * FROM dbaguy.app.users AS d USE KEYS "u_numbers"')
    assert names == ["big", "i64max", "i64min", "u64max", "beyond", "dec", "pi", "sci", "tiny", "huge", "half", OVERFLOW_NAME]
    assert rows == [[J("9007199254740993"), J("9223372036854775807"), J("-9223372036854775808"),
                     J("18446744073709551615"), J("12345678901234567890"), J("1.10"),
                     J("3.141592653589793238462643383279"), J("1E+3"), J("1e-400"), J("1e400"), 0.5, MISSING]]
    assert encode_row(rows[0])[5] == {"$type": "json", "text": "1.10"}
    _, [[raw]], _ = read(connection, 'SELECT RAW d FROM dbaguy.app.users AS d USE KEYS "u_numbers"')
    assert raw == J(U_NUMBERS)
    names, rows, _ = read(connection, 'SELECT * FROM dbaguy.app.users AS d USE KEYS "u_keyorder"')
    assert names == ["zeta", "alpha", "ws", "mid", OVERFLOW_NAME] and rows[0][1] == J('{"z": 1, "a": 2}')


def test_star_over_mixed_documents(connection):  # Review Focus 4
    names, rows, _ = read(connection, 'SELECT * FROM dbaguy.app.users AS d WHERE META(d).id IN ["u1","u2","u3","u4","u5"] '
                                      "ORDER BY META(d).id")
    assert names == ["name", "age", "tags", "address", "title", "nested", "value", "select", "odd key", OVERFLOW_NAME]
    assert rows[1][:4] == ["Bob", None, MISSING, J('{"city":"Tainan"}')]  # null and MISSING apart
    # R-04a-T8a: a string document beside an object one: the object's keys are the columns, the string goes whole
    # into the overflow cell as it came (`{"d": …}`), so nothing is lost
    names, rows, _ = read(connection, 'SELECT * FROM dbaguy.app.users AS d WHERE META(d).id IN ["s1","u1"] ORDER BY META(d).id')
    assert names == ["name", "age", "tags", "address", OVERFLOW_NAME]
    assert rows[0] == [MISSING, MISSING, MISSING, MISSING, J('{"d":"just a string document"}')]
    assert rows[1][0] == "Ann" and rows[1][-1] is MISSING


def test_browse_shape_expands_the_alias(connection):  # A1, D-04-11's statement
    sql = "SELECT META(d).id, * FROM events AS d WHERE META(d).id BETWEEN 'e100400' AND 'e101100' ORDER BY META(d).id"
    names, rows, _ = read(connection, sql, RunOptions(query_context=APP))
    assert names == ["id", "amount", "at", "d.id", "kind", OVERFLOW_NAME]  # first page from e100400 (i=400): amount first
    assert len(rows) == 701 and rows[600][0] == "e101000"
    assert rows[600][-1] == J('{"late":{"note":"a key the first page does not show"}}')  # after the first page
    assert rows[6][1] is MISSING  # e100406 (i % 7 == 0) has no amount


def test_errors_carry_code_and_utf16_position(connection):
    with pytest.raises(StatementError) as error:
        connection.run("SELECT '中文😀' AS x WHER")
    assert (error.value.code, error.value.position.line, error.value.position.column) == ("3000", 1, 20)
    assert read(connection, "SELECT RAW 1")[1] == [[1]]  # an error answer leaves the connection usable


def test_an_error_after_rows_keeps_the_rows(connection):  # Review Focus 2
    result = connection.run('SELECT RAW CASE WHEN i < 3000 THEN i ELSE ABORT("stop at 3000") END '
                            "FROM ARRAY_RANGE(0, 10000) AS i")
    rows = []
    with pytest.raises(StatementError, match="stop at 3000") as error:
        result.columns()
        while batch := result.fetch(500):
            rows += batch
        result.next_result()
    result.close()
    assert error.value.code == "5011"
    assert rows and rows[0] == [0] and len(rows) <= 3000 and rows == [[i] for i in range(len(rows))]
    assert read(connection, "SELECT RAW 1")[1] == [[1]]


def test_dml_ddl_and_returning(connection):
    try:
        result = connection.run('UPSERT INTO dbaguy.app.orders (KEY, VALUE) VALUES ("zz_t1", {"a": 1}), ("zz_t2", {"a": 2})')
        assert result.columns() is None and result.affected_rows() == 2
        result.close()
        names, rows, messages = read(connection, 'DELETE FROM dbaguy.app.orders AS o USE KEYS ["zz_t1", "zz_t2"] '
                                                 "RETURNING RAW META(o).id")
        assert sorted(rows) == [["zz_t1"], ["zz_t2"]] and any("2 mutations" in m for m in messages)
        result = connection.run('DELETE FROM dbaguy.app.orders USE KEYS "zz_none"')
        assert result.columns() is None and result.affected_rows() == 0  # no mutationCount when nothing changed
        result.close()
        result = connection.run("CREATE INDEX zz_idx IF NOT EXISTS ON dbaguy.app.orders(status)")
        assert result.columns() is None and result.affected_rows() is None
        result.close()
    finally:
        couchbase_query('DELETE FROM dbaguy.app.orders USE KEYS ["zz_t1", "zz_t2"]')
        couchbase_query("DROP INDEX zz_idx IF EXISTS ON dbaguy.app.orders")


def test_parameters_are_typed_and_exact(connection):
    params = StatementParams(named={"n": "9007199254740993", "s": '"中文"'})
    _, rows, _ = read(connection, "SELECT $n + 1 AS n1, @s AS s", RunOptions(params=params))
    assert rows == [[J("9007199254740994"), "中文"]]  # int64 arithmetic on the server: exact (spec §1)
    _, rows, _ = read(connection, "SELECT $1 AS a, $2 AS b", RunOptions(params=StatementParams(positional=["[1,2]", "true"])))
    assert rows == [[J("[1,2]"), True]]


def test_the_query_context_goes_with_each_request(connection):
    assert read(connection, "SELECT RAW COUNT(*) FROM events", RunOptions(query_context=APP))[1] == [[20_000]]
    with pytest.raises(StatementError) as error:
        connection.run("SELECT RAW COUNT(*) FROM events")
    assert error.value.code == "12003"  # keyspace not found: no context, no resolution


def test_internal_queries_decode_plain_json_and_the_catalog_works(connection):  # R-04a-C20
    assert connection.query("SELECT RAW $n + 0", {"n": "42"}) == [42]
    assert connection.query('SELECT RAW s.name FROM system:scopes AS s WHERE s.`bucket` = $b AND s.name = $s',
                            {"b": '"dbaguy"', "s": '"app"'}) == ["app"]
    names = [node.name for node in connection.list_children([])]
    assert "dbaguy" in names
    path = [PathSegment(kind=NodeKind.BUCKET, name="dbaguy"), PathSegment(kind=NodeKind.SCOPE, name="app")]
    assert "orders" in [node.name for node in connection.list_children(path)]
    with pytest.raises(StatementError) as error:
        connection.query('UPSERT INTO dbaguy.app.orders (KEY, VALUE) VALUES ("zz_internal", {})')
    assert error.value.code == "1000"  # internal queries are always read-only


def test_transactions_are_refused(connection):
    for sql in ("BEGIN WORK", "  start transaction", "/* x */ COMMIT", "ROLLBACK WORK", "SAVEPOINT s1",
                "SET TRANSACTION ISOLATION LEVEL READ COMMITTED", "SELECT 1; COMMIT"):
        with pytest.raises(StatementError) as error:
            connection.run(sql)
        assert error.value.code == "transactions_unsupported", sql
    with pytest.raises(StatementError):
        connection.set_autocommit(False)
    connection.set_autocommit(True)
    connection.commit()
    connection.rollback()
    assert (connection.in_transaction(), connection.autocommit()) == (False, True)
    assert read(connection, "SELECT RAW 1")[1] == [[1]]


def test_read_only_profile_is_enforced_by_the_server():  # Review Focus 5
    couchbase_query('DELETE FROM dbaguy.app.orders USE KEYS "zz_ro"')  # a leftover of an earlier failed run
    conn = open_couchbase(read_only=True)
    name = f"zz_p_{uuid.uuid4().hex[:8]}"
    try:
        for sql in ('UPSERT INTO dbaguy.app.orders (KEY, VALUE) VALUES ("zz_ro", {})',
                    'DELETE FROM system:active_requests WHERE clientContextID = "none"'):
            with pytest.raises(StatementError) as error:
                conn.run(sql)
            assert error.value.code == "1000", sql
        read(conn, f'PREPARE {name} FROM UPSERT INTO dbaguy.app.orders (KEY, VALUE) VALUES ("zz_ro", {{}})')
        with pytest.raises(StatementError) as error:
            conn.run(f"EXECUTE {name}")
        assert error.value.code == "1000"
        assert read(conn, "WITH a AS (SELECT 1 AS x) SELECT a")[1]  # A12: a CTE reads
        # A12: ADVISE answers under readonly (on app.orders: ADVISE on app.users panics with 5001 on 7.6.12, A15)
        result = conn.run('ADVISE SELECT * FROM dbaguy.app.orders WHERE status = "x"')
        try:
            assert result.presentation() == "plan" and [c.name for c in result.columns()] == ["plan"]
            [[advice]] = result.fetch(10)
            assert '"IndexAdvice"' in advice.text
        finally:
            result.close()
    finally:
        conn.close()
        couchbase_query("DELETE FROM system:prepareds AS p WHERE p.name = $n", {"$n": name})  # this test's only
    assert couchbase_query('SELECT RAW 1 FROM dbaguy.app.orders USE KEYS "zz_ro"')["results"] == []


def test_cancel_a_long_aggregate_within_a_second(connection):  # Review Focus 3, spec §1; R-04a-C1
    timer, delivered = cancel_later(connection, 2.0)
    try:
        with pytest.raises(StatementCancelled):
            connection.run(LONG_AGGREGATE)  # blocks waiting for the status line until the cancel shuts it down
    finally:
        timer.cancel()
    assert delivered and time.monotonic() - delivered[0] < scaled(CANCEL_WITHIN_S)
    assert completed_request_state(connection.last_client_context_id) == "closed"
    assert read(connection, "SELECT RAW 1")[1] == [[1]]  # the same connection object works on


def test_a_cancel_mid_stream_keeps_the_rows_read(connection):  # Review Focus 3
    result = connection.run(STREAM)
    assert len(result.fetch(500)) == 500
    timer, delivered = cancel_later(connection, 1.2)  # past 1 s: system:completed_requests keeps it
    read_rows = 500
    try:
        with pytest.raises(StatementCancelled):
            while batch := result.fetch(500):
                read_rows += len(batch)
    finally:
        timer.cancel()
        result.close()
    assert delivered and time.monotonic() - delivered[0] < scaled(CANCEL_WITHIN_S)
    assert 500 < read_rows < 100_000_000
    assert completed_request_state(connection.last_client_context_id) == "closed"
    assert read(connection, "SELECT RAW 3")[1] == [[3]]


def test_a_stop_on_the_server_is_a_cancel(connection):  # Review Focus 2: status "stopped" without errors
    def stop() -> None:
        ident = connection.last_client_context_id
        couchbase_query("DELETE FROM system:active_requests AS r WHERE r.clientContextID = $id", {"$id": ident})

    timer = threading.Timer(1.5, stop)
    timer.start()
    result = None
    try:
        with pytest.raises(StatementCancelled, match=STOPPED_ON_THE_SERVER):
            result = connection.run(LONG_AGGREGATE)  # the stopped request answers with its head, then "stopped"
            result.columns()
            assert result.fetch(10) == []
    finally:
        timer.cancel()
        if result is not None:
            result.close()
    assert result is not None and completed_request_state(connection.last_client_context_id) == "stopped"
    assert read(connection, "SELECT RAW 4")[1] == [[4]]


def test_a_cancel_during_the_start_is_kept(connection, monkeypatch):  # Review Focus 3
    couchbase_query('DELETE FROM dbaguy.app.orders USE KEYS "zz_never"')  # a leftover of an earlier failed run
    real = couchbase_adapter.build_body

    def cancel_while_building(request):
        connection.cancel()  # arrives after run() began, before the request goes out
        return real(request)

    monkeypatch.setattr(couchbase_adapter, "build_body", cancel_while_building)
    with pytest.raises(StatementCancelled):
        connection.run('UPSERT INTO dbaguy.app.orders (KEY, VALUE) VALUES ("zz_never", {})')
    monkeypatch.undo()
    assert couchbase_query('SELECT RAW 1 FROM dbaguy.app.orders USE KEYS "zz_never"')["results"] == []
    assert read(connection, "SELECT RAW 5")[1] == [[5]]


@pytest.mark.parametrize("how", ["cancel", "force_cancel", "abort"])
def test_an_armed_cancel_is_kept_and_nothing_is_sent(connection, how):  # R-04a-C16 (Task 6b contract)
    couchbase_query('DELETE FROM dbaguy.app.orders USE KEYS "zz_armed"')  # a leftover of an earlier failed run
    connection.arm_cancel()
    getattr(connection, how)()
    with pytest.raises(StatementCancelled):
        connection.run('UPSERT INTO dbaguy.app.orders (KEY, VALUE) VALUES ("zz_armed", {})')
    connection.disarm_cancel()
    assert couchbase_query('SELECT RAW 1 FROM dbaguy.app.orders USE KEYS "zz_armed"')["results"] == []
    connection.arm_cancel()  # armed and disarmed without a statement: nothing is left behind
    connection.cancel()
    connection.disarm_cancel()
    assert read(connection, "SELECT RAW 6")[1] == [[6]]


def test_an_idle_cancel_never_reaches_the_next_statement(connection):  # Review Focus 3
    connection.cancel()
    assert read(connection, "SELECT RAW 1")[1] == [[1]]
    result = connection.run("SELECT RAW 7")
    assert result.fetch(10) == [[7]]  # read to its end: the request is over
    connection.cancel()  # after the end, before the close: nothing to stop
    result.next_result()
    result.close()
    assert read(connection, "SELECT RAW 8")[1] == [[8]]


def test_closing_a_partly_read_result_frees_the_server_request(connection):  # Review Focus 3
    result = connection.run("SELECT RAW a * 1000 + b FROM ARRAY_RANGE(0, 3000) AS a UNNEST ARRAY_RANGE(0, 1000) AS b")
    result.columns()
    assert len(result.fetch(500)) == 500
    time.sleep(1.5)  # long enough for system:completed_requests to keep it
    started = time.monotonic()
    result.close()
    assert time.monotonic() - started < 0.5
    assert completed_request_state(connection.last_client_context_id) == "closed"
    assert read(connection, "SELECT RAW 2")[1] == [[2]]


def test_a_result_left_open_is_closed_by_the_next_statement(connection):
    result = connection.run(STREAM)
    assert len(result.fetch(500)) == 500
    left_open = connection.last_client_context_id
    time.sleep(1.2)  # completed_requests keeps requests over 1 s
    assert read(connection, "SELECT RAW 9")[1] == [[9]]
    assert completed_request_state(left_open) == "closed"


def test_explain_is_one_plan_cell(connection):  # A15
    result = connection.run("EXPLAIN SELECT * FROM dbaguy.app.users")
    try:
        assert result.presentation() == "plan" and [c.name for c in result.columns()] == ["plan"]
        [[cell]] = result.fetch(10)
        assert isinstance(cell, J) and '"#operator"' in cell.text
    finally:
        result.close()


def test_a_big_document_is_one_truncated_cell(connection):
    _, [[cell]], _ = read(connection, 'SELECT RAW d FROM dbaguy.app.users AS d USE KEYS "u_big"')
    assert len(cell.text) > 5 * 1024 * 1024 and encode_row([cell])[0]["truncated"]


def test_streams_100k_rows_within_budget(connection):  # Q1, an absolute budget as R-03a-P1 (ruling P21)
    started = time.monotonic()
    result = connection.run('SELECT i AS n, "x" AS s FROM ARRAY_RANGE(0, 100000) AS i')
    result.columns()
    total = 0
    while batch := result.fetch(500):
        total += len([encode_row(row) for row in batch])
    result.close()
    took = time.monotonic() - started
    assert total == 100_000 and took < scaled(4.0), f"{took:.2f} s"


def test_tls_with_the_cluster_ca(tmp_path):  # A10, A11
    ca = str(couchbase_ca_file(tmp_path))
    for mode in ("verify-ca", "verify-full", "require"):
        if mode == "verify-full" and not A11_SAN_HAS_LOOPBACK:
            with pytest.raises(ConnectionFailed, match="CA file"):
                open_couchbase(ssl=SslConfig(mode=mode, ca_file=ca))
            continue
        conn = open_couchbase(ssl=SslConfig(mode=mode, ca_file=ca if mode != "require" else None))
        try:
            assert read(conn, "SELECT RAW 1")[1] == [[1]]
            if mode == "verify-ca":  # cancel over TLS (A10), the timer started before run() (R-04a-C1)
                timer, delivered = cancel_later(conn, 1.5)  # past 1 s on the server: completed_requests keeps it
                try:
                    with pytest.raises(StatementCancelled):
                        conn.run(LONG_AGGREGATE)
                finally:
                    timer.cancel()
                assert delivered and time.monotonic() - delivered[0] < scaled(CANCEL_WITHIN_S)
                assert completed_request_state(conn.last_client_context_id) == "closed"
                assert read(conn, "SELECT RAW 2")[1] == [[2]]
        finally:
            conn.close()
    with pytest.raises(ConnectionFailed, match="CA file"):
        open_couchbase(ssl=SslConfig(mode="verify-ca"))  # the default bundle does not know the cluster CA


def test_a_failed_connect_names_its_cause_and_the_fix(tmp_path):  # R-04a-F1: the hint follows the failure
    ca = str(couchbase_ca_file(tmp_path))
    with pytest.raises(ConnectionFailed) as error:  # `require` on the plain-HTTP port: not a certificate problem
        open_couchbase(port=COUCHBASE_PORT, ssl=SslConfig(mode="require"))
    assert "WRONG_VERSION_NUMBER" in error.value.message and "CA" not in error.value.message
    assert error.value.message.endswith(
        "This port does not speak TLS: the query service uses 18093 for TLS (8093 is plain HTTP)")
    with pytest.raises(ConnectionFailed) as error:  # the right CA, a name the certificate does not hold
        open_couchbase(host="localhost", ssl=SslConfig(mode="verify-full", ca_file=ca))
    assert "Hostname mismatch" in error.value.message and "CA file" not in error.value.message
    assert error.value.message.endswith(
        "The certificate does not name this host: connect by a name it holds, or use SSL mode verify-ca")
    open_couchbase(host="localhost", ssl=SslConfig(mode="verify-ca", ca_file=ca)).close()  # the name was the only fault
    for mode in ("verify-ca", "verify-full"):  # no CA file: the cluster's self-signed CA is unknown
        with pytest.raises(ConnectionFailed) as error:
            open_couchbase(ssl=SslConfig(mode=mode))
        assert error.value.message.endswith(
            "Set the cluster's CA file in the SSL tab (the app does not read the macOS keychain)")
    for port, mode in ((COUCHBASE_MANAGER_PORT, "disable"), (18091, "require")):  # 18091: the manager with TLS
        with pytest.raises(ConnectionFailed) as error:  # the cluster manager answers a query request with a 404
            open_couchbase(port=port, ssl=SslConfig(mode=mode))
        assert error.value.message == (
            "HTTP 404 Object Not Found\nThis port is not the query service (8093, or 18093 with TLS)")
    with pytest.raises(ConnectionFailed) as error:  # plain HTTP to the TLS query port: a non-JSON 400
        open_couchbase(port=18093, ssl=SslConfig(mode="disable"))
    assert error.value.message.endswith(
        "This port may expect TLS: set SSL mode to require or verify-ca (18093 is the query service over TLS)")


@requires_couchbase_bastion
def test_couchbase_through_the_ssh_bastion_including_cancel(tmp_path):
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    tunnels = SshTunnelManager(write_askpass_script(tmp_path), extra_options=TEST_SSH_OPTIONS)
    service = ConnectionService(AdapterRegistry([CouchbaseAdapter()]), profiles, tunnels)
    profile = make_couchbase_profile(
        host="couchbase", port=8093, ssh=SshConfig(enabled=True, host="127.0.0.1", port=BASTION_PORT, user="dev", auth="password"),
    )
    lease = service.open(profile, None, ResolvedSecrets(password=COUCHBASE_PASSWORD, ssh_password="dev"))
    try:
        connection = lease.connection
        assert connection.server_info().version.startswith("Couchbase Server 7.6.12")
        timer, delivered = cancel_later(connection, 1.0)
        try:
            with pytest.raises(StatementCancelled):
                connection.run(LONG_AGGREGATE)
        finally:
            timer.cancel()
        assert delivered and time.monotonic() - delivered[0] < scaled(CANCEL_WITHIN_S)
        assert read(connection, "SELECT RAW 1")[1] == [[1]]
    finally:
        lease.close()
    assert tunnels.active_count() == 0
    with pytest.raises(ValidationFailed, match="one node"):
        service.open(make_couchbase_profile(host="a,b", ssh=profile.ssh), None, ResolvedSecrets(password="x"))


def test_urls_through_the_registry():
    registry = default_registry()
    assert registry.parse_url("couchbases://cb1,cb2/travel-sample").profile.engine == "couchbase"
    with pytest.raises(ValidationFailed) as error:
        registry.parse_url("couchbase://u:secret@h:x/b")
    assert "secret" not in error.value.message
