import json
import socket
import ssl
import threading
import time
from uuid import uuid4

import pytest
from loguru import logger

from dbaguy_backend.adapters.couchbase import http as http_module
from dbaguy_backend.adapters.couchbase.http import (
    ChannelError, ChunkedBody, NodeClient, QueryRequest, TlsFailure, basic_auth, build_body, client_context_id,
    failure_reason, tls_failure,
)
from dbaguy_backend.adapters.couchbase.stream import EndEvent, FieldEvent, ResponseParser, RowEvent
from tests.couchbase_fakes import (
    FailingContext, FakeQueryServer, FullListenQueue, PlainHttpPort, Reply, cert_error, ssl_error,
)
from tests.support import (
    COUCHBASE_HOST, COUCHBASE_PASSWORD, COUCHBASE_PORT, COUCHBASE_TLS_PORT, COUCHBASE_USER, LONG_AGGREGATE,
    completed_request_state, couchbase_ca_file, requires_couchbase, scaled, wait_until,
)

OK = b'{"results":[1],"status":"success"}'


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def node(server, clock=time.monotonic, auth=basic_auth("u", "p")):
    return NodeClient("127.0.0.1", server.port, auth=auth, user_agent="DbaGuy/test", tls=None, clock=clock)


def read_all(response) -> bytes:
    data = b""
    while chunk := response.read_chunk():
        data += chunk
    return data


def send(client, retry_safe=True, attach=lambda channel: None):
    return client.send(b'{"statement":"SELECT 1"}', retry_safe=retry_safe, attach=attach)


def test_body_embeds_literals_unchanged():
    body = build_body(QueryRequest(
        statement='SELECT $n, ? -- "中文"\n', client_context_id="dbaguy-r1", query_context="default:dbaguy.app",
        readonly=True, named={"n": "9007199254740993", "doc": '{"a": 1.10}'}, positional=["1e400", '"x"'],
    ))
    assert body.decode() == (
        '{"statement":"SELECT $n, ? -- \\"中文\\"\\n","client_context_id":"dbaguy-r1",'
        '"query_context":"default:dbaguy.app","readonly":true,"$n":9007199254740993,"$doc":{"a": 1.10},'
        '"args":[1e400,"x"]}'
    )
    assert json.loads(body)["$n"] == 9007199254740993  # still valid JSON
    assert b"timeout" not in body and b"scan_consistency" not in body  # never sent (R-04-5)


def test_a_timeout_is_in_the_body_only_when_the_request_has_one():  # R-04a-F3
    body = build_body(QueryRequest("SELECT 1", "dbaguy-r2", readonly=True, timeout="30s"))
    assert body.decode() == '{"statement":"SELECT 1","client_context_id":"dbaguy-r2","readonly":true,"timeout":"30s"}'
    assert json.loads(body)["timeout"] == "30s"
    assert b"timeout" not in build_body(QueryRequest("SELECT 1", "dbaguy-r3"))  # a user's statement: never


def test_a_lone_surrogate_cannot_be_sent():
    with pytest.raises(UnicodeEncodeError):
        build_body(QueryRequest(statement="SELECT '\ud800'", client_context_id="x"))


def test_client_context_ids():
    assert client_context_id('a/b"c 1-2_3') == "dbaguy-abc1-2_3"
    assert len(client_context_id("x" * 100)) == 64


def test_failure_reasons_and_labels():
    assert [failure_reason(e) for e in (ConnectionRefusedError(61, "x"), TimeoutError("x"), socket.gaierror(8, "x"),
                                        OSError("boom"), ConnectionResetError())] == [
        "refused", "timed out", "unknown host", "boom", "ConnectionResetError",
    ]
    assert NodeClient("::1", 8093, auth="", user_agent="", tls=None).label == "[::1]:8093"
    assert NodeClient("db.example", 18093, auth="", user_agent="", tls=None).label == "db.example:18093"


def test_a_connection_is_reused_while_fresh():
    server = FakeQueryServer([Reply(OK, chunked=False), Reply(OK)])
    client = node(server)
    try:
        first = send(client)
        assert read_all(first) == OK
        first.abandon()  # read to the end already: the connection stays
        assert read_all(send(client)) == OK
        assert server.connections == 1 and len(server.bodies) == 2
    finally:
        client.close()
        server.close()


def test_an_old_or_unfinished_connection_is_replaced():
    clock = Clock()
    server = FakeQueryServer([Reply(OK), Reply(OK), Reply(b'{"results":[' + b"1," * 50_000 + b'1]}'), Reply(OK)])
    client = node(server, clock)
    try:
        read_all(send(client))
        clock.now += 31  # idle for longer than KEEPALIVE_REUSE_S
        read_all(send(client))
        assert server.connections == 2
        unfinished = send(client)
        unfinished.read_chunk()
        unfinished.abandon()  # not read to the end: the connection is dropped
        with pytest.raises(ChannelError):  # and what was left never looks like a clean end
            unfinished.read_chunk()
        read_all(send(client))
        assert server.connections == 3
    finally:
        client.close()
        server.close()


def test_a_connection_the_server_says_it_closes_is_not_reused():
    server = FakeQueryServer([Reply(OK, close=True), Reply(OK)])
    client = node(server)
    try:
        assert read_all(send(client)) == OK and read_all(send(client)) == OK
        assert server.connections == 2
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize("chunked", [True, False], ids=["chunked", "content-length"])
def test_a_body_cut_short_is_a_failure_not_an_end(chunked):
    body = b'{"results":[' + b"1," * 1000 + b'1],"status":"success"}'
    server = FakeQueryServer([Reply(body, chunked=chunked, cut=True), Reply(OK, chunked=chunked)])
    client = node(server)
    try:
        response = send(client)
        with pytest.raises(ChannelError) as error:
            read_all(response)
        assert "IncompleteRead" in str(error.value) and not response.done
        assert read_all(send(client)) == OK and server.connections == 2  # never reused
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize("retry_safe", [True, False])
def test_a_read_is_resent_once_when_the_kept_connection_was_closed(retry_safe):
    server = FakeQueryServer([Reply(OK, drop_after=True), Reply(OK)])
    client = node(server)
    try:
        read_all(send(client))
        time.sleep(0.1)  # the server has closed its end
        if retry_safe:
            assert read_all(send(client)) == OK and server.connections == 2
        else:
            with pytest.raises(ChannelError) as error:
                send(client, retry_safe=False)
            assert error.value.before_response
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize("chunked", [True, False], ids=["chunked", "content-length"])
def test_shutdown_from_another_thread_ends_a_blocked_read(chunked):  # Review Focus 3
    hold = threading.Event()
    body = b'{"results":[' + b"1," * 1000 + b'1],"status":"success"}'
    server = FakeQueryServer([Reply(body, hold=hold, chunked=chunked)])
    client = node(server)
    channels = []
    try:
        response = send(client, attach=channels.append)
        response.read_chunk()  # the first half; the server now waits
        threading.Timer(0.2, channels[0].shutdown).start()
        started = time.monotonic()
        with pytest.raises(ChannelError):
            read_all(response)
        assert time.monotonic() - started < scaled(1.0)
        with pytest.raises(ChannelError):  # a failed body never ends cleanly
            response.read_chunk()
        assert not response.done
    finally:
        hold.set()
        client.close()
        server.close()


def test_a_cancel_while_waiting_for_the_status_line_is_not_resent():  # R-04a-C2
    answer = threading.Event()
    server = FakeQueryServer([Reply(OK), Reply(OK, before_head=answer), Reply(OK)])
    client = node(server)
    channels, logged = [], []
    sink = logger.add(lambda message: logged.append(message.record["message"]), level="DEBUG")
    try:
        read_all(send(client))  # the connection is now a kept-alive one: a stale-looking failure would be resent

        def cancel():
            if wait_until(lambda: len(server.bodies) == 2, scaled(5)):
                channels[-1].shutdown()

        threading.Thread(target=cancel, daemon=True).start()
        started = time.monotonic()
        with pytest.raises(ChannelError) as error:
            send(client, attach=channels.append)  # retry_safe: a read
        assert time.monotonic() - started < scaled(2.0)
        assert error.value.before_response  # it looks like a server that closed the connection …
        assert len(channels) == 1 and len(server.bodies) == 2 and server.connections == 1  # … and nothing was resent
        assert not any("kept-alive" in line for line in logged)
        assert read_all(send(client)) == OK and server.connections == 2  # the next request: a new connection
    finally:
        logger.remove(sink)
        answer.set()
        client.close()
        server.close()


def test_attach_refusal_sends_nothing():  # Review Focus 3: a cancel before the request goes out
    server = FakeQueryServer([Reply(OK)])
    client = node(server)

    def refuse(channel):
        raise RuntimeError("cancelled")

    try:
        with pytest.raises(RuntimeError):
            send(client, attach=refuse)
        time.sleep(0.1)
        assert server.connections == 0 and server.bodies == []
    finally:
        client.close()
        server.close()


def test_a_cancel_after_attach_and_before_connecting_sends_nothing():
    server = FakeQueryServer([Reply(OK)])
    client = node(server)
    try:
        with pytest.raises(ChannelError):
            send(client, attach=lambda channel: channel.shutdown())
        time.sleep(0.1)
        assert server.bodies == []
        assert read_all(send(client)) == OK  # a new connection for the next request
    finally:
        client.close()
        server.close()


def test_a_refused_connection():
    with socket.create_server(("127.0.0.1", 0)) as probe:
        port = probe.getsockname()[1]  # closed again: nothing listens there
    client = NodeClient("127.0.0.1", port, auth="", user_agent="DbaGuy/test", tls=None)
    with pytest.raises(ChannelError) as error:
        send(client)
    assert (str(error.value), error.value.before_response, error.value.tls, error.value.tls_failure) == (
        "refused", False, False, None)


def test_a_host_name_with_more_addresses_tries_the_next_one():  # `localhost`: ::1 refused, then 127.0.0.1
    server = FakeQueryServer([Reply(OK)])
    client = NodeClient("localhost", server.port, auth="", user_agent="DbaGuy/test", tls=None)
    try:
        assert read_all(send(client)) == OK
    finally:
        client.close()
        server.close()


def test_a_cancel_while_connecting_ends_the_connect_at_once():  # R-04a-T7a
    queue = FullListenQueue()
    client = NodeClient("127.0.0.1", queue.port, auth="", user_agent="DbaGuy/test", tls=None)
    shut_at = []

    def attach(channel):
        def cancel():
            shut_at.append(time.monotonic())
            channel.shutdown()

        threading.Timer(0.2, cancel).start()

    try:
        with pytest.raises(ChannelError) as error:
            send(client, attach=attach)
        assert time.monotonic() - shut_at[0] < scaled(0.2)
        assert str(error.value) == "cancelled while connecting"
    finally:
        client.close()
        queue.close()


def test_a_connect_that_gets_no_answer_times_out(monkeypatch):
    monkeypatch.setattr(http_module, "CONNECT_TIMEOUT_S", 0.3)  # the whole attempt, every address
    queue = FullListenQueue()
    client = NodeClient("127.0.0.1", queue.port, auth="", user_agent="DbaGuy/test", tls=None)
    try:
        started = time.monotonic()
        with pytest.raises(ChannelError) as error:
            send(client)
        assert 0.25 < time.monotonic() - started < 0.3 + scaled(0.5)
        assert (str(error.value), error.value.before_response) == ("timed out", False)
    finally:
        client.close()
        queue.close()


@pytest.mark.parametrize("error, kind", [
    (cert_error(62), TlsFailure.HOSTNAME), (cert_error(64), TlsFailure.HOSTNAME),  # a name / an IP address not held
    (cert_error(18), TlsFailure.ISSUER), (cert_error(19), TlsFailure.ISSUER),  # self-signed (alone, in the chain)
    (cert_error(20), TlsFailure.ISSUER), (cert_error(21), TlsFailure.ISSUER),  # no certificate for the issuer
    (cert_error(10), TlsFailure.OTHER), (cert_error(9), TlsFailure.OTHER),  # expired, not yet valid
    (ssl.SSLCertVerificationError(1, "no verify code"), TlsFailure.OTHER),
    (ssl_error("WRONG_VERSION_NUMBER"), TlsFailure.NOT_TLS),
    (ssl_error("TLSV1_ALERT_PROTOCOL_VERSION"), TlsFailure.OTHER), (ssl_error(None), TlsFailure.OTHER),
    (ssl.SSLEOFError(8, "EOF occurred in violation of protocol"), TlsFailure.OTHER),
])
def test_a_tls_failure_is_told_by_its_fields(error, kind):  # R-04a-F1: never by parsing the message
    assert tls_failure(error) == kind


@pytest.mark.parametrize("error, kind", [
    (cert_error(62), TlsFailure.HOSTNAME), (cert_error(20), TlsFailure.ISSUER), (cert_error(10), TlsFailure.OTHER),
    (ssl_error("WRONG_VERSION_NUMBER"), TlsFailure.NOT_TLS),
])
def test_the_channel_error_carries_the_kind_of_a_failed_handshake(error, kind):
    server = FakeQueryServer([])
    client = NodeClient("127.0.0.1", server.port, auth="", user_agent="DbaGuy/test", tls=FailingContext(error))
    try:
        with pytest.raises(ChannelError) as raised:
            send(client)
        assert raised.value.tls and raised.value.tls_failure == kind and not raised.value.before_response
    finally:
        client.close()
        server.close()


def test_a_tls_client_on_a_plain_http_port_fails_with_a_wrong_version():  # a real OpenSSL, not a made-up error
    port = PlainHttpPort()
    client = NodeClient("127.0.0.1", port.port, auth="", user_agent="DbaGuy/test", tls=ssl.create_default_context())
    try:
        with pytest.raises(ChannelError) as raised:
            send(client)
        assert raised.value.tls_failure == TlsFailure.NOT_TLS and "WRONG_VERSION_NUMBER" in str(raised.value)
    finally:
        client.close()
        port.close()


def test_read_timeout_ends_a_stalled_status_line_and_a_stalled_body():  # R-04a-F3
    stall = threading.Event()
    server = FakeQueryServer([Reply(OK, before_head=stall), Reply(b'{"results":[' + b"1," * 1000 + b'1]}', hold=stall)])
    client = node(server)
    try:
        started = time.monotonic()
        with pytest.raises(ChannelError, match="timed out"):  # nothing arrives
            client.send(b'{"statement":"SELECT 1"}', retry_safe=False, attach=lambda channel: None, read_timeout=0.3)
        assert time.monotonic() - started < scaled(3.0)
        response = client.send(b'{"statement":"SELECT 1"}', retry_safe=False, attach=lambda channel: None,
                               read_timeout=0.3)
        response.read_chunk()  # the first half; the server now waits
        with pytest.raises(ChannelError, match="timed out"):  # the rest never comes
            read_all(response)
        assert not response.done
    finally:
        stall.set()
        client.close()
        server.close()


def test_a_read_timeout_is_set_for_each_request_not_left_on_a_kept_alive_channel():  # R-04a-F3
    server = FakeQueryServer([Reply(OK, chunked=False), Reply(OK, chunked=False), Reply(OK, chunked=False)])
    client = node(server)
    channels, timeouts = [], []
    try:
        for read_timeout in (40.0, None, 5.0):
            response = client.send(b'{"statement":"SELECT 1"}', retry_safe=True, attach=channels.append,
                                   read_timeout=read_timeout)
            timeouts.append(channels[-1].conn.sock.gettimeout())
            read_all(response)
        assert timeouts == [40.0, None, 5.0] and server.connections == 1 and len(set(map(id, channels))) == 1
    finally:
        client.close()
        server.close()


def test_a_host_http_client_refuses_is_a_channel_error():
    client = NodeClient("bad host", 8093, auth="", user_agent="DbaGuy/test", tls=None)
    attached = []
    with pytest.raises(ChannelError) as error:
        send(client, attach=attached.append)
    assert (str(error.value), error.value.before_response, attached) == ("invalid host name", False, [])


def test_a_non_json_error_page_is_reported_as_it_came():
    server = FakeQueryServer([Reply(b"<html>bad gateway</html>", status=502, content_type="text/html")])
    client = node(server)
    try:
        response = send(client)
        assert (response.status, response.content_type) == (502, "text/html")
    finally:
        client.close()
        server.close()


def live_client(*, tls: ssl.SSLContext | None = None) -> NodeClient:
    return NodeClient(COUCHBASE_HOST, COUCHBASE_TLS_PORT if tls else COUCHBASE_PORT,
                      auth=basic_auth(COUCHBASE_USER, COUCHBASE_PASSWORD), user_agent="DbaGuy/test", tls=tls)


def live_events(client: NodeClient, request: QueryRequest) -> list:
    parser = ResponseParser()
    response = client.send(build_body(request), retry_safe=True, attach=lambda channel: None)
    events = []
    while chunk := response.read_chunk():
        events += parser.feed(chunk)
    return events + parser.finish()


@requires_couchbase
@pytest.mark.parametrize("password", ["wrong", "dbaguy"])
def test_the_query_service_answers_errors_as_json(password):  # A14
    client = NodeClient(COUCHBASE_HOST, COUCHBASE_PORT, auth=basic_auth("dbaguy", password), user_agent="DbaGuy/test",
                        tls=None)
    try:
        response = client.send(build_body(QueryRequest("SELEC 1", "dbaguy-a14")), retry_safe=True,
                               attach=lambda channel: None)
        body = read_all(response)
        assert "application/json" in response.content_type and json.loads(body)["errors"]
        assert response.status == 401 if password == "wrong" else response.status != 401
    finally:
        client.close()


@requires_couchbase
def test_a_request_reaches_the_server_as_built():
    ident = client_context_id(uuid4().hex)
    client = live_client()
    try:
        events = live_events(client, QueryRequest(
            "SELECT $n AS n, $1 AS p, $s AS s", ident, readonly=True,
            named={"n": "9007199254740993", "s": '"中文😀"'}, positional=["-7"],
        ))
        assert FieldEvent("clientContextID", ident) in events and FieldEvent("status", "success") in events
        rows = [e.row for e in events if isinstance(e, RowEvent)]
        assert [(f.key, f.text) for f in rows[0].fields] == [("n", "9007199254740993"), ("p", "-7"), ("s", '"中文😀"')]

        in_context = QueryRequest("SELECT RAW COUNT(*) FROM users", client_context_id(uuid4().hex),
                                  query_context="default:`dbaguy`.`app`", readonly=True)
        assert FieldEvent("status", "success") in live_events(client, in_context)

        write = QueryRequest(f'DELETE FROM dbaguy._default._default USE KEYS "dbaguy-no-such-{uuid4().hex}"',
                             client_context_id(uuid4().hex), readonly=True)
        errors = next(e.value for e in live_events(client, write) if isinstance(e, FieldEvent) and e.name == "errors")
        assert errors[0]["code"] == 1000  # read-only, refused by the server
    finally:
        client.close()


@requires_couchbase
def test_a_long_streamed_result_comes_row_by_row():
    client = live_client()
    try:
        events = live_events(client, QueryRequest(
            'SELECT RAW {"i": i, "s": "中文😀" || TO_STRING(i)} FROM ARRAY_RANGE(0, 50000) AS i',
            client_context_id(uuid4().hex), readonly=True,
        ))
        rows = [e.row for e in events if isinstance(e, RowEvent)]
        assert len(rows) == 50_000
        assert all(r.fields[0].text == str(n) and r.fields[1].value == f"中文😀{n}" for n, r in enumerate(rows))
        metrics = next(e.value for e in events if isinstance(e, FieldEvent) and e.name == "metrics")
        assert metrics["resultCount"] == 50_000 and events[-1] == EndEvent()
    finally:
        client.close()


@pytest.fixture
def cluster_tls(tmp_path) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(couchbase_ca_file(tmp_path)))


@requires_couchbase
@pytest.mark.parametrize("use_tls", [False, True], ids=["plain", "tls"])
def test_a_shutdown_stops_a_long_aggregate_on_the_server(use_tls, request):  # A10 through this module
    client = live_client(tls=request.getfixturevalue("cluster_tls") if use_tls else None)
    ident = client_context_id(uuid4().hex)
    channels, shut_at = [], []

    def cancel():
        shut_at.append(time.monotonic())
        channels[-1].shutdown()

    try:
        first = client.send(build_body(QueryRequest("SELECT RAW 1", client_context_id(uuid4().hex))), retry_safe=True,
                            attach=lambda channel: None)
        assert b'"status": "success"' in read_all(first)  # the channel is now a kept-alive one
        # after 1.5 s: system:completed_requests only logs requests that ran over 1 s (or failed)
        threading.Timer(1.5, cancel).start()
        with pytest.raises(ChannelError) as error:  # waiting for the status line: no output before the count
            client.send(build_body(QueryRequest(LONG_AGGREGATE, ident, readonly=True)), retry_safe=True,
                        attach=channels.append)
        assert time.monotonic() - shut_at[0] < scaled(0.5)
        assert len(channels) == 1 and not error.value.tls
        assert completed_request_state(ident) == "closed"
        assert FieldEvent("status", "success") in live_events(client, QueryRequest("SELECT RAW 1", ident + "-next"))
    finally:
        client.close()


@requires_couchbase
def test_a_certificate_the_client_does_not_trust_is_a_tls_failure():
    client = live_client(tls=ssl.create_default_context())  # the system CAs: not the cluster's
    try:
        with pytest.raises(ChannelError) as error:
            client.send(build_body(QueryRequest("SELECT RAW 1", "dbaguy-tls")), retry_safe=True,
                        attach=lambda channel: None)
        assert error.value.tls and "CERTIFICATE_VERIFY_FAILED" in str(error.value)
        assert error.value.tls_failure == TlsFailure.ISSUER  # the cluster's CA is self-signed, the system does not know it
    finally:
        client.close()


def _chunked(*parts: bytes, extension: bytes = b"", trailer: bytes = b"") -> bytes:
    body = b"".join(f"{len(p):x}".encode() + extension + b"\r\n" + p + b"\r\n" for p in parts)
    return body + b"0\r\n" + trailer + b"\r\n"


def test_chunked_body_decodes_at_every_split():  # B33: what has arrived is decoded, a cut line waits for the rest
    parts = (b'{"results":[', b"1,2", b"]}" * 40)
    for extension, trailer in [(b"", b""), (b";x=1", b"X-Trailer: 1\r\n")]:
        body = _chunked(*parts, extension=extension, trailer=trailer)
        for cut in range(len(body) + 1):
            decoder = ChunkedBody()
            data = decoder.feed(body[:cut]) + decoder.feed(body[cut:])
            assert data == b"".join(parts) and decoder.done
        byte_by_byte = ChunkedBody()
        assert b"".join(byte_by_byte.feed(body[i:i + 1]) for i in range(len(body))) == b"".join(parts)


@pytest.mark.parametrize("body, problem", [
    (b"zz\r\n", "not a hexadecimal number"), (b"-5\r\nabcde\r\n", "not a hexadecimal number"),
    (b"3\r\nabcXY", "not followed by CRLF"), (b"0\r\n\r\nmore", "data after the last chunk"),
    (b"1" * 5000, "too long"),
])
def test_chunked_body_refuses_broken_framing_with_a_fixed_text(body, problem):
    with pytest.raises(ValueError, match=problem) as error:
        ChunkedBody().feed(body)
    assert "zz" not in str(error.value)  # never the peer's bytes
