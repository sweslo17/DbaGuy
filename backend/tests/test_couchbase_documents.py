"""SqlppDocumentStore against the dev cluster (spec §3.9.2): exact stored text and META, the server's form, blocked
numbers, CAS checks that keep the expiry, the binary guard, duplicates, documents that are not objects, a lean read.
Writes only zz_04c_ keys in dbaguy.app.orders (removed by key)."""

import tracemalloc

import pytest

from dbaguy_backend.adapters.base import StatementError
from dbaguy_backend.adapters.couchbase.adapter import CouchbaseConnection
from dbaguy_backend.adapters.couchbase.documents import SqlppDocumentStore
from dbaguy_backend.adapters.couchbase.stream import parse_row
from dbaguy_backend.domain.couchbase import Keyspace
from dbaguy_backend.domain.errors import (
    BinaryDocument, DocumentConflict, DocumentExists, DocumentMissing, WriteOutcomeUnknown,
)
from tests.support import (
    COUCHBASE_READER, StoredDocument, put_raw_document, requires_couchbase, scratch_documents, scratch_key,
    stored_document,
)
from tests.test_couchbase_adapter import U_KEYORDER, open_couchbase
from tests.test_dev_env import U_NUMBERS

pytestmark = requires_couchbase
USERS = Keyspace(bucket="dbaguy", scope="app", collection="users")
BLOBS = Keyspace(bucket="dbaguy", scope="app", collection="blobs")
ORDERS = Keyspace(bucket="dbaguy", scope="app", collection="orders")
NEVER = "zz_04c_never_there"


@pytest.fixture
def connection():
    conn = open_couchbase()
    yield conn
    conn.close()


@pytest.fixture
def store(connection):
    return connection.documents()


@pytest.fixture
def scratch():
    with scratch_documents() as keys:
        yield keys


def test_get_keeps_the_stored_text_and_meta(store):  # Review Focus 5's exactness, R-04-2
    numbers = store.get(USERS, "u_numbers")
    assert (numbers.text, numbers.type, numbers.expiration) == (U_NUMBERS, "json", 0)
    assert numbers.size == len(U_NUMBERS.encode()) and numbers.cas == stored_document("u_numbers", collection="users").cas
    assert store.get(USERS, "u_keyorder").text == U_KEYORDER
    assert store.get(USERS, "u_ttl").expiration == 4102444800
    assert [store.get(USERS, key).text for key in ("s1", "a1", "n1")] == ['"just a string document"', "[1,2,3]", "42"]
    assert store.get(USERS, NEVER) is None


def test_binary_documents_show_their_size_only(store):
    assert store.get(BLOBS, "bin1").model_dump(exclude={"id", "cas"}) == {
        "expiration": 0, "type": "base64", "text": None, "size": 9}


def test_a_big_document_is_read_lean(store):  # Review Focus 5, ruling C9
    store.get(USERS, "u1")  # the connection is open and warm
    tracemalloc.start()
    try:
        big = store.get(USERS, "u_big")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert len(big.text) > 5_000_000 and big.size == len(big.text)
    assert peak < 3.5 * len(big.text), peak / len(big.text)  # planning: 3.0× lean, 4.0× with the row decoded


def test_server_form_is_what_the_server_would_store(store):  # A2
    assert store.server_form(U_KEYORDER) == '{"alpha":{"a":2,"z":1},"mid":"a b","ws":[1,2],"zeta":1}'


def test_blocked_numbers_name_their_paths(store):  # ruling C4
    assert store.blocked_numbers(U_NUMBERS) == ["$.u64max", "$.beyond", "$.huge"]
    fine = '{"i":9223372036854775807,"j":-9223372036854775808,"k":1.7976931348623157e308,"t":1e-400,"s":"1e400"}'
    assert store.blocked_numbers(fine) == []
    assert store.blocked_numbers("[9223372036854775808, -9223372036854775809, 2e308, 1234567890123456789012.5]") == [
        "$[0]", "$[1]", "$[2]"]


def test_replace_checks_the_cas_and_keeps_the_expiry(store, scratch):  # Review Focus 3, A27, A28
    key = scratch_key()
    scratch.append(key)
    put_raw_document(key, '{"b": 1, "a": 1.10}', expiry=2_000_000_000)
    old = store.get(ORDERS, key)
    saved = store.replace(ORDERS, key, '{"b": 2, "a": 1.10}', int(old.cas))
    assert saved.status == "saved" and saved.cas != old.cas and saved.expiration == 2_000_000_000
    assert stored_document(key) == StoredDocument('{"a":1.1,"b":2}', saved.cas, 2_000_000_000, "json")
    for stale in (int(old.cas), int(saved.cas) + 1, int(saved.cas) - 1):
        with pytest.raises(DocumentConflict) as conflict:
            store.replace(ORDERS, key, '{"c": 3}', stale)
        assert conflict.value.detail == {"cas": saved.cas}
    assert stored_document(key).text == '{"a":1.1,"b":2}'


def test_replace_never_turns_a_binary_document_into_json(store, scratch):  # Review Focus 3, A30
    key = scratch_key("bin")
    scratch.append(key)
    put_raw_document(key, "not json")
    binary = store.get(ORDERS, key)
    assert (binary.type, binary.text, binary.size) == ("base64", None, 8)
    with pytest.raises(BinaryDocument):
        store.replace(ORDERS, key, '{"x":1}', int(binary.cas))
    with pytest.raises(BinaryDocument):
        store.remove(ORDERS, key, int(binary.cas))
    assert stored_document(key).type == "base64"


def test_replace_and_remove_of_a_missing_document_say_so(store):
    with pytest.raises(DocumentMissing):
        store.replace(ORDERS, NEVER, "{}", 1)
    with pytest.raises(DocumentMissing):
        store.remove(ORDERS, NEVER, 1)


def test_insert_twice_is_exists_and_remove_checks_the_cas(store, scratch):  # Review Focus 3, A29
    key = scratch_key()
    scratch.append(key)
    saved = store.insert(ORDERS, key, '{"b": 1, "a": 2}')
    assert saved.expiration == 0 and stored_document(key).text == '{"a":2,"b":1}'
    with pytest.raises(DocumentExists):
        store.insert(ORDERS, key, "{}")
    with pytest.raises(DocumentConflict):
        store.remove(ORDERS, key, int(saved.cas) + 1)
    store.remove(ORDERS, key, int(saved.cas))
    assert stored_document(key) is None and store.get(ORDERS, key) is None


def test_documents_that_are_not_objects(store, scratch):  # A3
    key = scratch_key()
    scratch.append(key)
    saved = store.insert(ORDERS, key, '"just text"')
    assert stored_document(key).text == '"just text"'
    for text in ("[1, 2]", "42.50", '{"back": "to an object"}', "null"):
        saved = store.replace(ORDERS, key, text, int(saved.cas))
        assert stored_document(key).text == store.server_form(text)


def test_a_limited_user_reads_documents():  # FP §8: the company's users are limited
    connection = open_couchbase(username=COUCHBASE_READER)
    try:
        assert connection.documents().get(USERS, "u1").text.startswith('{"name":"Ann"')
    finally:
        connection.close()


def test_writes_are_never_resent_and_reads_are_read_only(store, scratch, monkeypatch):  # D-04-9, ruling C7
    sent = []
    begin = CouchbaseConnection._begin

    def recording(self, sql, **kwargs):
        sent.append((sql.split()[0], kwargs["readonly"], kwargs["retry_safe"], kwargs.get("preserve_expiry", False)))
        return begin(self, sql, **kwargs)

    monkeypatch.setattr(CouchbaseConnection, "_begin", recording)
    key = scratch_key()
    scratch.append(key)
    saved = store.insert(ORDERS, key, '{"a":1}')
    store.replace(ORDERS, key, '{"a":2}', int(saved.cas))
    assert ("INSERT", False, False, False) in sent and ("UPDATE", False, False, True) in sent
    assert {(readonly, retry) for verb, readonly, retry, _ in sent if verb == "SELECT"} == {(True, True)}


def test_a_failed_read_after_a_write_is_an_unknown_outcome():  # ruling C25: written, but no META to report
    class Statements:
        def statement_rows(self, statement, named=None, *, write=False, preserve_expiry=False):
            if write:
                return [parse_row('{"cas":1792325990605258752}')]
            raise StatementError("Connection to the query node was lost: the test dropped it", code="connection_lost")

    with pytest.raises(WriteOutcomeUnknown):
        SqlppDocumentStore(Statements()).replace(ORDERS, NEVER, "{}", 1)
    with pytest.raises(WriteOutcomeUnknown):
        SqlppDocumentStore(Statements()).insert(ORDERS, NEVER, "{}")
