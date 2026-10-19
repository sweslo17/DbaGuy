"""Couchbase reads a paused result ahead (R-04a-T11a): rows the server has sent go into memory without waiting, up to
READ_AHEAD_BYTES, in order, with any error after them; and against the dev cluster, a result that fits is read to its
end while a larger one keeps the server's request open."""

import shutil
import ssl
import subprocess
import sys
import threading
import time

import pytest

from dbaguy_backend.adapters.base import ReadAheadState, StatementError
from dbaguy_backend.adapters.couchbase.adapter import PAUSED_RESULT_MAX_IDLE_S, READ_AHEAD_BYTES
from dbaguy_backend.adapters.couchbase.http import ChannelError
from dbaguy_backend.adapters.couchbase.result import READ_AHEAD_STEP_CHUNKS, row_cost
from dbaguy_backend.adapters.couchbase.stream import ResponseParser, RowEvent, parse_row
from dbaguy_backend.domain.execution import ExecuteCommand, FetchMoreCommand
from dbaguy_backend.domain.profiles import SslConfig
from dbaguy_backend.sessions.worker import paused_result_closed_text
from tests.couchbase_fakes import Reply
from tests.fake_ssh import wait_until
from tests.support import (
    COUCHBASE_TLS_PORT, completed_request_state, couchbase_ca_file, request_active, requires_couchbase, scaled,
)
from tests.test_couchbase_adapter import STREAM
from tests.test_couchbase_result import fake_connection, result_of
from tests.test_couchbase_sessions import APP, Clock, page_through, start_cb_worker
from tests.test_dev_env import U_NUMBERS
from tests.test_sessions import finished, paused

BIG = 10**12  # a budget nothing reaches


def chunked(body: bytes, size: int) -> list[bytes]:
    return [body[i:i + size] for i in range(0, len(body), size)]


def numbers_body(count: int) -> bytes:
    rows = ",".join(f'{{"n":{i}}}' for i in range(count))
    return (f'{{"signature":{{"n":"number"}},"results":[{rows}],"status":"success",'
            f'"metrics":{{"resultCount":{count}}}}}').encode()


def numbers(result, n):
    return [row[0] for row in result.fetch(n)]


SECOND = Reply(b'{"signature":{"n":"number"},"results":[{"n":7}],"status":"success"}')


@pytest.fixture
def tls_pair(tmp_path):
    """A certificate for 127.0.0.1 made now: the fake server's context, and a client context that trusts it."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not installed")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1",
         "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    return server, ssl.create_default_context(cafile=str(cert))


def test_parse_row_rebuilds_what_the_parser_made():  # Review Focus 2: exact literals survive the re-parse
    body = ('{"signature":"json","results":[' + U_NUMBERS + ',"a\\"b \\u00e9",42,1e400,-0,[1,{"x":null}],'
            '{"a":{"b":[1,2]},"c":"d","e":1.10},null,true,{}],"status":"success"}').encode()
    parser = ResponseParser()
    rows = [e.row for e in parser.feed(body) + parser.finish() if isinstance(e, RowEvent)]
    assert len(rows) == 10 and [parse_row(row.text) for row in rows] == rows


def test_row_cost_counts_what_python_holds():
    assert row_cost("abc") == sys.getsizeof("abc") + 8
    assert row_cost("中文") > row_cost("ab")  # wider characters take more


def test_read_ahead_holds_rows_until_the_budget_then_says_full():  # Review Focus 2
    result, _, released = result_of(*chunked(numbers_body(5000), 4096), sql="SELECT n FROM ks", bare_star=False)
    result.columns()
    assert numbers(result, 10) == list(range(10))
    budget = 50_000
    step = result.read_ahead(budget)
    while step.state is ReadAheadState.READING:
        step = result.read_ahead(budget)
    assert step.state is ReadAheadState.FULL
    assert budget <= result.held < budget + row_cost('{"n":4999}')
    assert not released  # the answer goes on: the connection is still this request's
    assert numbers(result, 5000) == list(range(10, 5000))  # held rows first, then the stream, in order
    assert result.held == 0


def test_received_counts_every_byte_and_the_end_releases_the_connection():
    body = numbers_body(3000)
    result, _, released = result_of(*chunked(body, 1024), sql="SELECT n FROM ks", bare_star=False)
    result.columns()
    step = result.read_ahead(BIG)
    while step.state is not ReadAheadState.DONE:
        step = result.read_ahead(BIG)
    assert step.received == len(body) and released
    assert numbers(result, 5000) == list(range(3000))
    assert result.next_result() is False


def test_one_step_reads_at_most_its_chunks():
    result, _, _ = result_of(*chunked(numbers_body(5000), 1024), sql="SELECT n FROM ks", bare_star=False)
    result.columns()  # the head and the first chunk
    step = result.read_ahead(BIG)
    assert step.state is ReadAheadState.READING and step.received == (1 + READ_AHEAD_STEP_CHUNKS) * 1024


def test_nothing_on_the_socket_is_waiting_and_nothing_is_read():
    result, response, _ = result_of(*chunked(numbers_body(5000), 1024), sql="SELECT n FROM ks", bare_star=False)
    result.columns()
    response.arrived = 0
    assert result.read_ahead(BIG).state is ReadAheadState.READING  # the rows already parsed, taken without a read
    step = result.read_ahead(BIG)
    assert step.state is ReadAheadState.WAITING and step.received == 1024
    response.arrived = 2
    step = result.read_ahead(BIG)
    assert step.state is ReadAheadState.READING and step.received == 3 * 1024


def test_an_error_after_the_rows_is_raised_after_the_held_rows():  # Review Focus 2
    body = (b'{"signature":{"a":"json"},"results":[{"a":1},{"a":2},{"a":3}],'
            b'"errors":[{"code":1080,"msg":"Timeout 3s exceeded"}],"status":"fatal"}')
    result, _, released = result_of(*chunked(body, 16), sql="SELECT a FROM ks", bare_star=False)
    result.columns()
    assert result.fetch(1) == [[1]]
    step = result.read_ahead(BIG)
    while step.state is ReadAheadState.READING:  # 16-byte chunks: a step makes READ_AHEAD_STEP_CHUNKS reads (B35)
        step = result.read_ahead(BIG)
    assert step.state is ReadAheadState.DONE and released
    assert result.fetch(10) == [[2], [3]]
    with pytest.raises(StatementError, match="Timeout 3s exceeded"):
        result.fetch(1)


def test_a_cut_answer_while_reading_ahead_keeps_the_rows_read():
    result, response, _ = result_of(*chunked(numbers_body(3000), 1024), sql="SELECT n FROM ks", bare_star=False)
    result.columns()
    assert result.read_ahead(BIG).state is ReadAheadState.READING
    response.fail_with = ChannelError("Connection reset by peer")
    assert result.read_ahead(BIG).state is ReadAheadState.DONE
    held = numbers(result, 5000)
    assert held == list(range(len(held))) and len(held) > 0
    with pytest.raises(StatementError) as error:
        result.fetch(1)
    assert error.value.code == "connection_lost"


def test_closing_drops_the_held_rows_and_abandons_the_answer():
    result, response, released = result_of(*chunked(numbers_body(5000), 4096), sql="SELECT n FROM ks",
                                           bare_star=False)
    result.columns()
    while result.read_ahead(20_000).state is ReadAheadState.READING:
        pass
    result.close()
    assert result.held == 0 and response.abandoned and released


def test_a_step_never_waits_at_a_chunk_boundary():  # B33, Review Focus 1: the server sent a whole chunk, then holds
    hold = threading.Event()
    server, connection = fake_connection([Reply(numbers_body(3000), hold=hold)])
    try:
        result = connection.run("SELECT n FROM ks")
        result.columns()
        states = []
        stepper = threading.Thread(target=lambda: states.extend(result.read_ahead(BIG).state for _ in range(5)),
                                   daemon=True)
        stepper.start()
        stepper.join(scaled(2.0))
        assert not stepper.is_alive(), "a read-ahead step waited for the server"
        assert states[-1] is ReadAheadState.WAITING
        hold.set()
        wait_until(lambda: result.read_ahead(BIG).state is ReadAheadState.DONE, 5)
        assert numbers(result, 5000) == list(range(3000))
    finally:
        hold.set()
        connection.close()
        server.close()


def test_a_row_longer_than_a_step_keeps_reading():  # B34: a chunk that completes no row is still progress
    body = ('{"signature":{"s":"string"},"results":[{"s":"' + "x" * 20_000 + '"}],"status":"success"}').encode()
    result, _, _ = result_of(*chunked(body, 1024), sql="SELECT s FROM ks", bare_star=False)
    result.columns()
    states = [result.read_ahead(BIG).state]
    while states[-1] is ReadAheadState.READING:
        states.append(result.read_ahead(BIG).state)
    assert states[-1] is ReadAheadState.DONE and ReadAheadState.WAITING not in states


def test_the_star_peek_comes_before_the_held_rows():  # Review Focus 2: a page smaller than the peek leaves peeked rows
    rows = ",".join(f'{{"n":{i}}}' for i in range(3000))
    body = f'{{"signature":{{"*":"*"}},"results":[{rows}],"status":"success"}}'.encode()
    result, _, _ = result_of(*chunked(body, 1024))
    result.columns()  # the peek: the first 500 rows
    assert numbers(result, 100) == list(range(100))
    while result.read_ahead(20_000).state is ReadAheadState.READING:
        pass
    assert result.held >= 20_000
    assert numbers(result, 5000) == list(range(100, 3000))  # the peek's rest, the held rows, then the stream


@pytest.mark.parametrize("split_after_rows", [False, True], ids=["end-parsed", "end-in-the-next-read"])
def test_a_last_row_that_fills_the_budget_still_ends_the_answer(split_after_rows):  # Q2: an answer in memory is no stall
    body = numbers_body(100)
    cut = body.index(b"]") + 1 if split_after_rows else len(body)
    result, _, released = result_of(*[part for part in (body[:cut], body[cut:]) if part], sql="SELECT n FROM ks",
                                    bare_star=False)
    result.columns()
    assert numbers(result, 1) == [0]
    budget = sum(row_cost(f'{{"n":{i}}}') for i in range(1, 100))  # the last row fills it exactly
    step = result.read_ahead(budget)
    while step.state is ReadAheadState.READING:
        step = result.read_ahead(budget)
    assert step.state is ReadAheadState.DONE and released and result.held == budget
    assert numbers(result, 200) == list(range(1, 100))


def test_an_answer_already_whole_in_memory_is_never_full():  # its tail came in the read that crossed the budget
    body = numbers_body(3000)
    result, _, released = result_of(body[:200], body[200:], sql="SELECT n FROM ks", bare_star=False)
    result.columns()
    assert numbers(result, 5) == list(range(5))
    budget = row_cost('{"n":100}') * 50  # crossed early in the second read, which holds the rest of the answer
    step = result.read_ahead(budget)
    while step.state is ReadAheadState.READING:
        step = result.read_ahead(budget)
    assert step.state is ReadAheadState.DONE and released  # a full answer the server has finished is no stall
    assert numbers(result, 5000) == list(range(5, 3000))


def test_received_counts_what_a_load_more_read_too():  # B44: every byte of the answer, by any path
    body = numbers_body(5000)
    result, response, _ = result_of(*chunked(body, 1024), sql="SELECT n FROM ks", bare_star=False)
    result.columns()
    assert numbers(result, 2000) == list(range(2000))  # a Load more: fetch() reads the wire itself
    response.arrived = 0  # nothing more on the socket: this step reads nothing
    step = result.read_ahead(BIG)
    assert step.received == len(body) - sum(len(chunk) for chunk in response._chunks) > 1024


@pytest.mark.parametrize("chunked_body", [True, False], ids=["chunked", "content-length"])
def test_an_answer_read_ahead_to_its_end_keeps_its_connection(chunked_body):  # the next statement reuses it
    second = b'{"signature":{"n":"number"},"results":[{"n":7}],"status":"success"}'
    server, connection = fake_connection([Reply(numbers_body(3000), chunked=chunked_body), Reply(second)])
    try:
        result = connection.run("SELECT n FROM ks")
        result.columns()
        assert numbers(result, 10) == list(range(10))
        wait_until(lambda: result.read_ahead(BIG).state is ReadAheadState.DONE, 5)
        assert numbers(result, 5000) == list(range(10, 3000))
        assert numbers(connection.run("SELECT n FROM ks"), 10) == [7] and server.connections == 1
    finally:
        connection.close()
        server.close()


@pytest.mark.parametrize("use_tls", [False, True], ids=["plain", "tls"])
def test_an_end_that_arrives_after_the_read_ahead_keeps_the_connection(use_tls, request):  # TLS: a record of its own
    tls = request.getfixturevalue("tls_pair") if use_tls else (None, None)
    end = threading.Event()
    server, connection = fake_connection([Reply(numbers_body(3000), end_hold=end), SECOND], server_tls=tls[0],
                                         client_tls=tls[1])
    try:
        result = connection.run("SELECT n FROM ks")
        result.columns()
        wait_until(lambda: result.read_ahead(BIG).state is ReadAheadState.DONE, 5)  # the object has closed
        end.set()  # the end of the chunked body comes after the last step
        wait_until(connection._client._channel.readable, 5)
        assert numbers(result, 5000) == list(range(3000))
        assert numbers(connection.run("SELECT n FROM ks"), 10) == [7] and server.connections == 1
    finally:
        end.set()
        connection.close()
        server.close()


def test_tls_bytes_openssl_has_already_decrypted_are_read_ahead(tls_pair):  # `pending()`: no select() sees them
    # The head and 13 KB of body in one record (some TLS terminators fill records): http.client reads the head 8 KiB at a
    # time, so OpenSSL keeps the rest of that record decrypted while the socket itself is empty.
    hold = threading.Event()
    body = numbers_body(2500)
    server, connection = fake_connection([Reply(body, hold=hold)], server_tls=tls_pair[0], client_tls=tls_pair[1])
    try:
        result = connection.run("SELECT n FROM ks")
        result.columns()
        steps = [result.read_ahead(BIG) for _ in range(5)]
        assert steps[-1].state is ReadAheadState.WAITING
        assert steps[-1].received == len(body) // 2  # the whole first record, while the server holds the rest
        hold.set()
        wait_until(lambda: result.read_ahead(BIG).state is ReadAheadState.DONE, 5)
        assert numbers(result, 5000) == list(range(2500))
    finally:
        hold.set()
        connection.close()
        server.close()


@requires_couchbase
def test_a_result_that_fits_is_read_to_its_end_and_stays_loadable():  # Review Focus 3
    clock = Clock()
    worker, rec = start_cb_worker(clock)
    try:
        worker.submit(ExecuteCommand(request_id="e", sql="SELECT META(d).id, * FROM events AS d", query_context=APP,
                                     page_size=500))
        rec.wait_for(paused("e", 500), timeout=30)
        ident = worker.connection.last_client_context_id
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.DONE, 30)
        assert not request_active(ident)  # the server's request is over: it holds no servicer
        clock.now += 10 * PAUSED_RESULT_MAX_IDLE_S
        worker.check_paused_result()
        assert page_through(worker, rec, "e", count=10_000) == 20_000
        assert paused_result_closed_text(PAUSED_RESULT_MAX_IDLE_S) not in [m.text for m in rec.of("e", "message")]
    finally:
        worker.close()


@requires_couchbase
def test_a_larger_result_holds_the_budget_and_keeps_the_server_request_open():
    worker, rec = start_cb_worker()
    try:
        worker.submit(ExecuteCommand(request_id="s", sql=STREAM, page_size=500))
        rec.wait_for(paused("s", 500), timeout=30)
        ident = worker.connection.last_client_context_id
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.FULL, 30)
        held = worker._open_exec.result.held  # the adapter's own figure: a test may look inside
        assert READ_AHEAD_BYTES <= held < READ_AHEAD_BYTES + 1024
        assert request_active(ident)  # the rest is still the server's to send
    finally:
        worker.close()


@requires_couchbase
def test_rows_stay_in_order_across_the_peek_the_read_ahead_and_the_stream():  # Review Focus 2
    worker, rec = start_cb_worker()
    try:
        worker.submit(ExecuteCommand(request_id="s", sql=STREAM, page_size=500))
        rec.wait_for(paused("s", 500), timeout=30)
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.FULL, 30)
        for page in range(1, 31):  # 300,000 more rows: past the ~120,000 held, into the stream
            worker.submit(FetchMoreCommand(request_id="s", count=10_000))
            rec.wait_for(paused("s", 500 + page * 10_000), timeout=30)
        values = [row[0] for event in rec.of("s", "rows") for row in event.rows]
        assert values == list(range(300_500))
    finally:
        worker.close()


@requires_couchbase
def test_a_discard_during_read_ahead_frees_the_server_request():  # Review Focus 1
    worker, rec = start_cb_worker()
    try:
        worker.submit(ExecuteCommand(request_id="s", sql=STREAM, page_size=500))
        rec.wait_for(paused("s", 500), timeout=30)
        ident = worker.connection.last_client_context_id
        started = time.monotonic()
        worker.cancel("s")  # paused, reading ahead: a discard
        rec.wait_for(finished("s"), timeout=10)
        assert time.monotonic() - started < scaled(1.0)
        wait_until(lambda: not request_active(ident), 5)
    finally:
        worker.close()


@requires_couchbase
def test_tls_reads_ahead_in_order_and_a_discard_closes_the_request(tmp_path):  # B43: pending TLS bytes and ready()
    ca = couchbase_ca_file(tmp_path)
    worker, rec = start_cb_worker(port=COUCHBASE_TLS_PORT, ssl=SslConfig(mode="verify-ca", ca_file=str(ca)))
    try:
        worker.submit(ExecuteCommand(request_id="t", sql=STREAM, page_size=500))
        rec.wait_for(paused("t", 500), timeout=30)
        ident = worker.connection.last_client_context_id
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.FULL, 30)
        assert READ_AHEAD_BYTES <= worker._open_exec.result.held < READ_AHEAD_BYTES + 1024
        for page in range(1, 4):  # 30,000 more rows, from the held ones
            worker.submit(FetchMoreCommand(request_id="t", count=10_000))
            rec.wait_for(paused("t", 500 + page * 10_000), timeout=30)
        assert [row[0] for event in rec.of("t", "rows") for row in event.rows] == list(range(30_500))
        time.sleep(1.2)  # system:completed_requests keeps requests over 1 s
        worker.cancel("t")  # paused: a discard
        rec.wait_for(finished("t"), timeout=10)
        assert completed_request_state(ident) == "closed"
    finally:
        worker.close()
