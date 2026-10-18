"""The Couchbase result object over canned responses (errors after rows, the `*` peek, cancel and close), and the
connection over a fake query service (what a request carries, what is never sent, login failures)."""

import json
import threading

import pytest

from dbaguy_backend import container
from dbaguy_backend.adapters.base import Endpoint, RunOptions, StatementCancelled, StatementError
from dbaguy_backend.adapters.couchbase.adapter import (
    PAUSED_RESULT_MAX_IDLE_S, TRANSACTIONS_UNSUPPORTED, CouchbaseAdapter, CouchbaseConnection,
)
from dbaguy_backend.adapters.couchbase.http import NodeClient, basic_auth
from dbaguy_backend.adapters.couchbase.result import EventStream, start_result
from dbaguy_backend.adapters.couchbase.rows import OVERFLOW_NAME
from dbaguy_backend.adapters.couchbase.settings import connection_settings
from dbaguy_backend.adapters.values import MISSING, JsonText
from dbaguy_backend.domain.errors import ConnectionFailed
from dbaguy_backend.domain.execution import ErrorPosition, MessageSeverity, QueryContext, StatementParams
from dbaguy_backend.domain.metadata import ServerInfo
from dbaguy_backend.domain.profiles import Engine
from dbaguy_backend.sessions.worker import paused_result_closed_text
from tests.couchbase_fakes import FakeQueryServer, FakeResponse, Reply
from tests.support import make_couchbase_profile


def result_of(*chunks, sql="SELECT * FROM ks", plan=False, bare_star=True, dml=False, cancelled=lambda: False):
    released = []
    response = FakeResponse(list(chunks))
    stream = EventStream(response, cancelled=cancelled, node="n:8093")
    result = start_result(stream, sql, plan=plan, bare_star=bare_star, dml=dml, on_done=lambda: released.append(1))
    return result, response, released


def test_errors_after_the_last_row_raise_from_next_result():  # Review Focus 2
    body = (b'{"signature":{"a":"json"},"results":[{"a":1},{"a":2}],'
            b'"errors":[{"code":1080,"msg":"Timeout 3s exceeded"}],"status":"fatal","metrics":{"resultCount":2}}')
    result, _, released = result_of(body[:40], body[40:])
    assert [c.name for c in result.columns()] == ["a"]
    assert result.fetch(200) == [[1], [2]]  # the rows come first, the request has ended
    assert released and [m.text for m in result.take_messages()] == ["2 results"]
    with pytest.raises(StatementError) as error:
        result.next_result()
    assert (error.value.code, error.value.message) == ("1080", "Timeout 3s exceeded")


def test_an_error_after_a_full_page_comes_on_the_next_fetch():
    body = b'{"signature":{"a":"json"},"results":[{"a":1},{"a":2}],"errors":[{"code":5010,"msg":"boom"}],"status":"fatal"}'
    result, _, _ = result_of(body)
    result.columns()
    assert result.fetch(2) == [[1], [2]]
    with pytest.raises(StatementError, match="boom"):
        result.fetch(1)


def test_errors_before_results_raise_from_the_start():
    body = b'{"requestID":"r","errors":[{"code":3000,"msg":"syntax error","line":1,"column":10}],"status":"fatal"}'
    with pytest.raises(StatementError) as error:
        result_of(body, sql="SELECT 😀 WHER")
    assert error.value.position == ErrorPosition(line=1, column=11)


def test_stopped_without_errors_is_a_cancel():  # Review Focus 2
    result, _, _ = result_of(b'{"signature":{"a":"json"},"results":[{"a":1}],"status":"stopped"}')
    result.columns()
    assert result.fetch(10) == [[1]]
    with pytest.raises(StatementCancelled, match="stopped on the server"):
        result.next_result()
    with pytest.raises(StatementCancelled):
        result_of(b'{"requestID":"r","status":"stopped"}')


def test_dml_counts_its_mutations():
    result, _, _ = result_of(b'{"results":[],"status":"success","metrics":{"mutationCount":3}}', sql="UPDATE ks SET a = 1",
                             dml=True)
    assert result.columns() is None and result.affected_rows() == 3
    assert result_of(b'{"results":[],"status":"success","metrics":{}}', dml=True)[0].affected_rows() == 0
    assert result_of(b'{"status":"success"}', sql="CREATE INDEX i ON ks(a)")[0].affected_rows() is None


@pytest.mark.parametrize("was_cancelled, code", [(False, "connection_lost"), (True, "cancelled")])
def test_a_cut_response_keeps_the_rows_read(was_cancelled, code):
    result, _, _ = result_of(b'{"signature":{"a":"json"},"results":[{"a":1},', cancelled=lambda: was_cancelled)
    result.columns()
    assert result.fetch(10) == [[1]]
    with pytest.raises(StatementError) as error:
        result.fetch(10)
    assert error.value.code == code


def test_a_malformed_response_is_a_protocol_error():
    result, _, _ = result_of(b'{"signature":{"a":"json"},"results":[{"a":1} x {"a":2}]}')
    result.columns()
    with pytest.raises(StatementError) as error:
        result.fetch(10)
        result.fetch(10)
    assert (error.value.code, error.value.message) == ("protocol_error", "The query service sent a malformed response")


def test_the_star_peek_reads_at_most_about_8_mib():
    row = '{"k":"' + "x" * (1024 * 1024) + '"}'
    chunks = [b'{"signature":{"*":"*"},"results":['] + [(row + ",").encode() for _ in range(19)] + [(row + ']}').encode()]
    result, response, _ = result_of(*chunks, bare_star=False)
    assert [c.name for c in result.columns()] == ["k", OVERFLOW_NAME]
    assert len(response._chunks) >= 10  # not everything was read to build the columns
    assert len(result.fetch(100)) == 20  # the peeked rows come first, then the rest


def test_a_cancel_during_the_star_peek_keeps_the_rows_read():  # Review Focus 3
    result, _, _ = result_of(b'{"signature":{"*":"*"},"results":[{"a":1},{"a":2},', bare_star=False,
                             cancelled=lambda: True)
    assert [c.name for c in result.columns()] == ["a", OVERFLOW_NAME]  # built from the rows that arrived
    assert result.fetch(10) == [[1, MISSING], [2, MISSING]]
    with pytest.raises(StatementCancelled):
        result.fetch(10)


def test_rows_for_a_raw_export_are_the_server_text():
    result, _, _ = result_of(b'{"signature":{"*":"*"},"results":[{"d":{"b":1.10, "a":[1e400]}}],"status":"success"}')
    result.columns()
    assert result.fetch_raw(10) == ['{"d":{"b":1.10, "a":[1e400]}}']


def test_closing_early_abandons_and_releases_once():
    result, response, released = result_of(b'{"signature":{"a":"json"},"results":[{"a":1},')
    result.columns()
    result.close()
    result.close()
    assert response.abandoned and released == [1]
    finished, response, _ = result_of(b'{"signature":"json","results":[1],"status":"success"}')
    finished.fetch(10)
    finished.close()
    assert not response.abandoned  # read to the end: the connection is kept


def test_a_plan_is_one_cell():
    text = '{"plan":{"#operator":"Sequence"},"text":"SELECT 1"}'
    result, _, _ = result_of(('{"signature":"json","results":[' + text + '],"status":"success"}').encode(),
                             sql="EXPLAIN SELECT 1", plan=True)
    assert result.presentation() == "plan" and [c.name for c in result.columns()] == ["plan"]
    assert result.fetch(10) == [[JsonText(text)]]


def test_keys_outside_the_columns_warn_once():
    result, _, _ = result_of(b'{"signature":{"a":"json"},"results":[{"a":1,"z":2},{"a":3,"y":4}],"status":"success"}',
                             bare_star=False)
    result.columns()
    result.fetch(10)
    warnings = [m for m in result.take_messages() if m.severity == MessageSeverity.WARNING]
    assert [m.text for m in warnings] == ["Some rows have keys outside the columns (from row 1); export as JSON to see every key"]
    assert result.fetch(10) == [] and not [m for m in result.take_messages() if m.severity == MessageSeverity.WARNING]


def test_a_non_json_answer_is_reported_by_its_status():
    server = FakeQueryServer([Reply(b"<html>bad gateway</html>", status=502, content_type="text/html")])
    profile = make_couchbase_profile(port=server.port)
    settings = connection_settings(profile, Endpoint("127.0.0.1", server.port), None)
    client = NodeClient("127.0.0.1", server.port, auth=basic_auth("u", "p"), user_agent="t", tls=None)
    connection = CouchbaseConnection(client, settings=settings,
                                     info=ServerInfo(engine=Engine.COUCHBASE, version="Couchbase Server test"))
    try:
        with pytest.raises(StatementError) as error:
            connection.run("SELECT 1")
        assert (error.value.code, error.value.message) == ("http_502", "HTTP 502 Reason")
    finally:
        connection.close()
        server.close()


# ---- the connection over a fake query service: what goes out, and what never does ----

OK_ONE = Reply(b'{"signature":{"a":"number"},"results":[{"a":1}],"status":"success"}')


def fake_connection(replies, **profile_fields):
    server = FakeQueryServer(replies)
    profile = make_couchbase_profile(port=server.port, **profile_fields)
    settings = connection_settings(profile, Endpoint("127.0.0.1", server.port), None)
    client = NodeClient("127.0.0.1", server.port, auth=basic_auth("u", "p"), user_agent="t", tls=None)
    connection = CouchbaseConnection(client, settings=settings,
                                     info=ServerInfo(engine=Engine.COUCHBASE, version="Couchbase Server test"))
    return server, connection


def sent(server):
    return [json.loads(body) for body in server.bodies]


def test_a_request_carries_the_options_and_nothing_else():
    server, connection = fake_connection([OK_ONE, OK_ONE], read_only=True)
    try:
        options = RunOptions(query_context=QueryContext(bucket="travel-sample", scope="inventory"),
                             params=StatementParams(named={"n": "9007199254740993"}, positional=['"x"']),
                             retry_safe=True)
        result = connection.run("SELECT $n, $1", options)
        assert result.fetch(10) == [[1]]
        result.close()
        connection.run("SELECT 1").close()
        first, second = sent(server)
        assert set(first) == {"statement", "client_context_id", "query_context", "readonly", "$n", "args"}
        assert first["query_context"] == "default:`travel-sample`.inventory" and first["readonly"] is True
        assert '"$n":9007199254740993' in server.bodies[0].decode()  # the literal as given, never through a float
        assert first["client_context_id"].startswith("dbaguy-") and first["client_context_id"] != second["client_context_id"]
        assert connection.last_client_context_id == second["client_context_id"]
        assert set(second) == {"statement", "client_context_id", "readonly"}  # no context, no timeout, no auto_execute
    finally:
        connection.close()
        server.close()


@pytest.mark.parametrize("sql", ["BEGIN WORK", "commit", "SELECT 1; ROLLBACK", "SAVEPOINT a", "START TRANSACTION"])
def test_transaction_statements_are_refused_before_anything_is_sent(sql):
    server, connection = fake_connection([OK_ONE])
    try:
        with pytest.raises(StatementError) as error:
            connection.run(sql)
        assert error.value.code == TRANSACTIONS_UNSUPPORTED and server.bodies == []
        with pytest.raises(StatementError) as error:
            connection.set_autocommit(False)
        assert error.value.code == TRANSACTIONS_UNSUPPORTED
    finally:
        connection.close()
        server.close()


def test_text_that_is_not_utf8_is_refused_before_anything_is_sent():
    server, connection = fake_connection([OK_ONE])
    try:
        with pytest.raises(StatementError) as error:
            connection.run("SELECT '\ud83d'")
        assert error.value.code == "invalid_text" and server.bodies == []
        with pytest.raises(StatementError) as error:
            # pydantic refuses such a name in a command; a context built otherwise is still refused, not sent
            connection.run("SELECT 1", RunOptions(query_context=QueryContext.model_construct(bucket="b\udc00", scope=None)))
        assert error.value.code == "invalid_text" and server.bodies == []
    finally:
        connection.close()
        server.close()


@pytest.mark.parametrize("how", ["cancel", "force_cancel", "abort"])
def test_an_armed_cancel_sends_nothing_and_a_disarmed_one_is_gone(how):  # R-04a-C16
    server, connection = fake_connection([OK_ONE])
    try:
        connection.arm_cancel()
        getattr(connection, how)()
        with pytest.raises(StatementCancelled):
            connection.run("SELECT 1")
        connection.disarm_cancel()
        assert server.bodies == []
        connection.arm_cancel()
        connection.cancel()
        connection.disarm_cancel()  # the statement never ran: its kept cancel goes with it
        connection.cancel()  # idle: no flag (P46)
        assert not connection.cancelled()
        result = connection.run("SELECT 1")
        assert result.fetch(10) == [[1]] and len(server.bodies) == 1
        result.close()
    finally:
        connection.close()
        server.close()


def test_a_cancel_while_waiting_for_the_status_line_is_a_cancel():  # Review Focus 3
    gate = threading.Event()
    server, connection = fake_connection([Reply(OK_ONE.body, before_head=gate)])
    timer = threading.Timer(0.2, connection.cancel)
    timer.start()
    try:
        with pytest.raises(StatementCancelled):
            connection.run("SELECT 1")
    finally:
        timer.cancel()
        gate.set()
        connection.close()
        server.close()


def test_a_cancel_mid_body_is_a_cancel_and_the_rows_read_stay():  # Review Focus 3
    hold = threading.Event()
    body = b'{"signature":{"a":"number"},"results":[' + b",".join(b'{"a":%d}' % i for i in range(200)) + b'],"status":"success"}'
    server, connection = fake_connection([Reply(body, hold=hold)])
    try:
        result = connection.run("SELECT a FROM ks")
        rows = result.fetch(50)
        assert rows[:2] == [[0], [1]]
        connection.cancel()
        with pytest.raises(StatementCancelled):
            while result.fetch(50):
                pass
        result.close()
    finally:
        hold.set()
        connection.close()
        server.close()


def test_internal_queries_are_read_only_with_named_literals_and_plain_ints():  # R-04a-C20
    server, connection = fake_connection([
        Reply(b'{"signature":"json","results":[{"num_replica":1,"big":9007199254740993},"x"],"status":"success"}'),
        Reply(b'{"signature":"json","results":[1],"errors":[{"code":5000,"msg":"late"}],"status":"fatal"}'),
    ])
    try:
        assert connection.query("SELECT RAW i FROM system:indexes AS i WHERE i.bucket_id = $b", {"b": '"dbaguy"'}) == [
            {"num_replica": 1, "big": 9007199254740993}, "x",
        ]
        [body] = sent(server)
        assert body["readonly"] is True and body["$b"] == "dbaguy" and '"$b":"dbaguy"' in server.bodies[0].decode()
        with pytest.raises(StatementError, match="late"):  # an error after the rows is not lost
            connection.query("SELECT RAW 1")
    finally:
        connection.close()
        server.close()


IDENTIFIED = b'{"signature":{"v":"string","u":"array"},"results":[{"v":"7.6.2-3721-community","u":["local:me"]}],"status":"success"}'


def open_fake(replies, **profile_fields):
    server = FakeQueryServer(replies)
    profile = make_couchbase_profile(port=server.port, **profile_fields)
    try:
        return server, CouchbaseAdapter().open(profile, Endpoint("127.0.0.1", server.port), "p", None)
    except BaseException:
        server.close()
        raise


def test_open_identifies_the_server():
    server, connection = open_fake([Reply(IDENTIFIED)])
    try:
        info = connection.server_info()
        assert (info.version, info.edition, info.current_user) == ("Couchbase Server 7.6.2-3721-community", "community",
                                                                   "local:me")
        assert sent(server)[0]["readonly"] is True  # identification is an internal query
    finally:
        connection.close()
        server.close()


def test_a_user_that_may_not_list_users_falls_back_to_the_version():  # A17's fallback
    denied = b'{"errors":[{"code":13014,"msg":"User does not have credentials"}],"status":"fatal"}'
    no_function = b'{"errors":[{"code":5010,"msg":"no such function"}],"status":"fatal"}'
    version = b'{"signature":{"v":"string"},"results":[{"v":"7.6.12-8946-enterprise"}],"status":"success"}'
    server, connection = open_fake([Reply(no_function, status=500), Reply(version)])
    try:
        assert connection.server_info().current_user == "dbaguy"  # the profile's user name
        assert connection.server_info().edition == "enterprise"
    finally:
        connection.close()
        server.close()
    server, connection = open_fake([Reply(denied, status=403), Reply(version)])  # refused CURRENT_USERS() only
    try:
        assert connection.server_info().current_user == "dbaguy"
    finally:
        connection.close()
        server.close()
    with pytest.raises(ConnectionFailed, match="Check the user name and password"):  # the version refused too
        open_fake([Reply(denied, status=403), Reply(denied, status=403)])
    with pytest.raises(ConnectionFailed, match="Check the user name and password"):  # a 401 never tries again
        open_fake([Reply(denied, status=401)])


@pytest.mark.parametrize("reply", [
    Reply(b'{"errors":[{"code":2120,"msg":"Failure to authenticate user"}],"status":"fatal"}', status=401),
    Reply(b'{"errors":[{"code":2999,"msg":"Some other way of saying no"}],"status":"fatal"}', status=401),
    Reply(b"<html>401</html>", status=401, content_type="text/html"),
])
def test_a_login_failure_says_so_at_once(reply):
    with pytest.raises(ConnectionFailed, match="Check the user name and password"):
        open_fake([reply])


def test_a_node_nobody_listens_on_is_reported_with_its_reason():
    with pytest.raises(ConnectionFailed, match=r"^Could not reach a query node: 127\.0\.0\.1:1 \(refused\)$"):
        CouchbaseAdapter().open(make_couchbase_profile(port=1), Endpoint("127.0.0.1", 1), "p", None)


def test_a_paused_result_is_closed_before_the_server_ends_it():  # A13: 7.6.12 ends a stalled request after 77–86 s
    assert PAUSED_RESULT_MAX_IDLE_S + container._HOUSEKEEPING_INTERVAL_S <= 77 - 15  # a margin for a slower server
    assert CouchbaseAdapter.capabilities.paused_result_max_idle_s == PAUSED_RESULT_MAX_IDLE_S
    assert paused_result_closed_text(PAUSED_RESULT_MAX_IDLE_S).startswith("Closed this result after 45 s without")
    assert paused_result_closed_text(120.0).startswith("Closed this result after 2 min without")
