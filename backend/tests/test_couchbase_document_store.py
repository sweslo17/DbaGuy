"""SqlppDocumentStore's failure mapping without a cluster (rulings C7, C25, C40; Task 4 fix round 2): a fake
`Statements` for what each failure of a write becomes and how many statements went out, and the fake query service for
the server's own answers (12009 for a document that still has the CAS sent, 1080 on each write)."""

import pytest

from dbaguy_backend.adapters.base import StatementCancelled, StatementError, StatementNotSent
from dbaguy_backend.adapters.couchbase.documents import SqlppDocumentStore
from dbaguy_backend.adapters.couchbase.messages import PROTOCOL_ERROR
from dbaguy_backend.adapters.couchbase.stream import parse_row
from dbaguy_backend.domain.couchbase import Keyspace
from dbaguy_backend.domain.errors import (
    BinaryDocument, DocumentConflict, DocumentMissing, QueryFailed, ValidationFailed, WriteOutcomeUnknown,
)
from tests.couchbase_fakes import Reply
from tests.test_couchbase_result import PONG, fake_connection, statements

KEYSPACE = Keyspace(bucket="dbaguy", scope="app", collection="orders")
OPENED_CAS = 5
STILL_OPENED = '{"cas":5,"expiration":0,"type":"json"}'  # the state read: the document still has the CAS sent
CHANGED = '{"cas":6,"expiration":0,"type":"json"}'
BINARY = '{"cas":5,"expiration":0,"type":"base64"}'
WRITES = {
    "replace": ("UPDATE", lambda store, cas: store.replace(KEYSPACE, "k", "{}", cas)),
    "insert": ("INSERT", lambda store, cas: store.insert(KEYSPACE, "k", "{}")),
    "remove": ("DELETE", lambda store, cas: store.remove(KEYSPACE, "k", cas)),
}
CAS_WRITES = ("replace", "remove")


class FakeStatements:
    """Answers a write with `write` and a read with `read` (row texts, or an exception to raise); records each call as
    (first word, write)."""

    def __init__(self, write=(), read=()):
        self.write, self.read = write, read
        self.calls = []

    def statement_rows(self, statement, named=None, *, write=False, preserve_expiry=False):
        self.calls.append((statement.split()[0], write))
        answer = self.write if write else self.read
        if isinstance(answer, BaseException):
            raise answer
        return [parse_row(text) for text in answer]


def attempt(write, statements, cas=OPENED_CAS):
    return WRITES[write][1](SqlppDocumentStore(statements), cas)


# ---- what a failed write becomes (rulings C7, C25; fix round 2) ----

@pytest.mark.parametrize("failure", [
    StatementError("Connection to the query node was lost: reset by peer", code="connection_lost"),
    StatementError(PROTOCOL_ERROR, code="protocol_error"),
    StatementCancelled(),
    StatementError("HTTP 502 Bad Gateway", code="http_502"),
    StatementError("Timeout 30s exceeded", code="1080"),  # the server's timeout: the write may have been applied
], ids=["lost", "protocol", "cancelled", "http_502", "timeout_1080"])
@pytest.mark.parametrize("write", WRITES)
def test_a_write_whose_answer_never_came_or_timed_out_is_unknown_and_sent_once(write, failure):
    sent = FakeStatements(write=failure, read=[STILL_OPENED])
    with pytest.raises(WriteOutcomeUnknown):
        attempt(write, sent)
    assert sent.calls == [(WRITES[write][0], True)]  # nothing after it: no state read, no second attempt


@pytest.mark.parametrize("code", ["5000", "12008", "13014"])
@pytest.mark.parametrize("write", WRITES)
def test_the_servers_own_error_answer_to_a_write_is_final(write, code):  # B39
    sent = FakeStatements(write=StatementError("the server's own words", code=code))
    with pytest.raises(QueryFailed) as refused:
        attempt(write, sent)
    assert refused.value.message == "the server's own words" and sent.calls == [(WRITES[write][0], True)]


@pytest.mark.parametrize("write", WRITES)
def test_a_write_that_never_went_out_fails_like_a_read(write):  # ruling C40: the read before it failed
    not_sent = StatementNotSent("Connection to the query node was lost: reset by peer", code="connection_lost")
    sent = FakeStatements(write=not_sent)
    with pytest.raises(StatementNotSent) as failed:
        attempt(write, sent)
    assert failed.value is not_sent and sent.calls == [(WRITES[write][0], True)]


@pytest.mark.parametrize("cas", [0, -1, 2**63, True, 1.0], ids=["0", "-1", "2^63", "True", "1.0"])
@pytest.mark.parametrize("write", CAS_WRITES)
def test_a_cas_this_server_cannot_hold_is_refused_before_anything_is_sent(write, cas):  # ruling C11
    sent = FakeStatements(write=['{"cas":6}'], read=[STILL_OPENED])
    with pytest.raises(ValidationFailed, match="^Not a CAS value of this server$"):
        attempt(write, sent, cas)
    assert sent.calls == []


# ---- why a CAS-checked write changed nothing (ruling C6; fix round 2: 12009 is not always a conflict) ----

@pytest.mark.parametrize("write", CAS_WRITES)
def test_a_12009_for_a_document_that_still_has_the_cas_sent_is_the_servers_refusal(write):
    sent = FakeStatements(write=StatementError("durability impossible", code="12009"), read=[STILL_OPENED])
    with pytest.raises(QueryFailed) as refused:  # a conflict carrying the user's own CAS: "Overwrite" would loop
        attempt(write, sent)
    assert refused.value.message == "durability impossible"
    assert sent.calls == [(WRITES[write][0], True), ("SELECT", False)]


@pytest.mark.parametrize("answer", [StatementError("concurrent modification", code="12009"), ()],
                         ids=["12009", "no_row"])
@pytest.mark.parametrize("write", CAS_WRITES)
def test_a_changed_missing_or_binary_document_is_told_apart(write, answer):
    for state, refusal in (([CHANGED], DocumentConflict), ([], DocumentMissing), ([BINARY], BinaryDocument)):
        with pytest.raises(refusal) as refused:
            attempt(write, FakeStatements(write=answer, read=state))
        if refusal is DocumentConflict:
            assert refused.value.detail == {"cas": "6"}


@pytest.mark.parametrize("write", CAS_WRITES)
def test_no_row_for_a_document_that_still_has_the_cas_sent_is_never_a_conflict(write):
    with pytest.raises(QueryFailed, match="^The server wrote nothing and gave no reason"):
        attempt(write, FakeStatements(write=(), read=[STILL_OPENED]))


# ---- the same over the fake query service: the server's own error answers ----

def error_answer(code: int, message: str) -> bytes:
    return (b'{"requestID":"r","errors":[{"code":%d,"msg":"%s"}],"status":"errors"}' % (code, message.encode()))


def state_answer(row: str) -> bytes:
    return b'{"signature":{"cas":"json","expiration":"json","type":"json"},"results":[%s],"status":"success"}' % (
        row.encode())


@pytest.mark.parametrize("write", CAS_WRITES)
def test_a_12009_the_server_gives_for_an_unchanged_document_is_its_refusal(write):  # fix round 2, F2
    refusal = "Failed to perform UPDATE - cause: Unable to complete action after 1 attempts"
    server, connection = fake_connection([Reply(PONG), Reply(error_answer(12009, refusal)),
                                          Reply(state_answer(STILL_OPENED))])
    try:
        with pytest.raises(QueryFailed) as refused:
            WRITES[write][1](connection.documents(), OPENED_CAS)
        assert refused.value.message == refusal
        assert [text.split()[0] for text in statements(server)] == ["SELECT", WRITES[write][0], "SELECT"]
    finally:
        connection.close()
        server.close()


@pytest.mark.parametrize("write", WRITES)
def test_a_write_the_server_timed_out_is_unknown_and_sent_once(write):  # fix round 2, F3 (live: 45 of 192 applied)
    server, connection = fake_connection([Reply(PONG), Reply(error_answer(1080, "Timeout 30s exceeded"))])
    try:
        with pytest.raises(WriteOutcomeUnknown):
            WRITES[write][1](connection.documents(), OPENED_CAS)
        sent_now = statements(server)  # the C40 read, then the write once: no state read, never sent again
        assert len(sent_now) == 2 and sent_now[0] == "SELECT RAW 1" and sent_now[1].split()[0] == WRITES[write][0]
    finally:
        connection.close()
        server.close()
