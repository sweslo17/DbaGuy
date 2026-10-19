"""The documents API over the dev cluster (spec §3.9.3–§3.9.4): the save flow with its preview and confirmation, the
refusals, no resend of a write whose answer was lost, nothing of a document in the log. Writes only zz_04c_ keys in
dbaguy.app.orders (removed by key)."""

from uuid import UUID

import pytest
from loguru import logger

from dbaguy_backend.adapters.base import StatementError
from dbaguy_backend.adapters.couchbase.adapter import CouchbaseConnection
from dbaguy_backend.adapters.couchbase.documents import SqlppDocumentStore
from dbaguy_backend.domain.couchbase import Keyspace
from dbaguy_backend.domain.errors import ValidationFailed
from dbaguy_backend.services import document_service
from tests.support import (
    COUCHBASE_PASSWORD, put_raw_document, requires_couchbase, scratch_documents, scratch_key, stored_document,
)
from tests.test_api import AUTH
from tests.test_couchbase_api import COUCHBASE, app, cb  # noqa: F401  (fixtures)
from tests.test_dev_env import U_NUMBERS

pytestmark = requires_couchbase
ORDERS = {"bucket": "dbaguy", "scope": "app", "collection": "orders"}
USERS = {"bucket": "dbaguy", "scope": "app", "collection": "users"}
NEVER = "zz_04c_never_there"


def post(client, path, **body):
    return client.post(f"/couchbase/documents/{path}", json=body, headers=AUTH)


def error_of(response):
    return response.status_code, response.json()["error"]["code"]


@pytest.fixture
def scratch():
    with scratch_documents() as keys:
        yield keys


def new_document(scratch, text, **options):
    key = scratch_key()
    scratch.append(key)
    put_raw_document(key, text, **options)
    return key


def test_get_answers_the_document_and_404_when_missing(app, cb):
    client, _ = app
    answer = post(client, "get", profile_id=cb, keyspace=USERS, id="u_numbers").json()
    assert (answer["text"], answer["type"], answer["expiration"]) == (U_NUMBERS, "json", 0)
    assert error_of(post(client, "get", profile_id=cb, keyspace=ORDERS, id=NEVER)) == (404, "document_missing")


def test_a_save_without_token_changes_is_written_at_once(app, cb, scratch):  # Review Focus 1, spec §12
    client, _ = app
    key = new_document(scratch, '{"a":1,"b":[1,2]}', expiry=2_000_000_000)
    cas = post(client, "get", profile_id=cb, keyspace=ORDERS, id=key).json()["cas"]
    saved = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas,
                 text='{\n  "a": 2,\n  "b": [1, 2]\n}').json()
    assert saved["status"] == "saved" and saved["cas"] != cas and saved["expiration"] == 2_000_000_000
    assert stored_document(key).text == '{"a":2,"b":[1,2]}'


def test_a_save_that_changes_tokens_asks_first_then_saves_when_confirmed(app, cb, scratch):  # Review Focus 1
    client, _ = app
    key = new_document(scratch, '{"zeta": 1}')
    cas = post(client, "get", profile_id=cb, keyspace=ORDERS, id=key).json()["cas"]
    text = '{"zeta": 1, "alpha": {"z": 1, "a": 2}, "dec": 1.10}'
    preview = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas, text=text).json()
    assert preview["status"] == "needs_confirmation"
    assert preview["server_text"] == '{"alpha":{"a":2,"z":1},"dec":1.1,"zeta":1}'
    assert [(c["path"], c["kind"], c["value_changed"]) for c in preview["changes"]] == [
        ("$.dec", "number", False), ("$", "key_order", False), ("$.alpha", "key_order", False)]
    assert preview["counts"] == {"key_order": 2, "number": 1, "duplicate_key": 0, "other": 0}
    assert stored_document(key).text == '{"zeta": 1}'  # nothing written before the confirmation
    saved = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas, text=text, confirmed=True).json()
    assert saved["status"] == "saved" and stored_document(key).text == preview["server_text"]


def test_a_stale_cas_is_a_conflict_carrying_the_current_cas(app, cb, scratch):  # Review Focus 3
    client, _ = app
    key = new_document(scratch, '{"a":1}')
    first = post(client, "get", profile_id=cb, keyspace=ORDERS, id=key).json()["cas"]
    saved = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=first, text='{"a":2}').json()
    stale = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=first, text='{"a":3}')
    assert error_of(stale) == (409, "document_conflict") and stale.json()["error"]["detail"] == {"cas": saved["cas"]}


def test_numbers_the_server_would_change_are_refused_with_their_paths(app, cb, scratch, monkeypatch):  # ruling C4
    client, _ = app
    key = new_document(scratch, '{"a":1}')
    cas = post(client, "get", profile_id=cb, keyspace=ORDERS, id=key).json()["cas"]
    sent = []
    begin = CouchbaseConnection._begin
    monkeypatch.setattr(CouchbaseConnection, "_begin",
                        lambda self, sql, **kwargs: (sent.append(sql), begin(self, sql, **kwargs))[1])
    answer = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas,
                  text='{"ok":1,"u":18446744073709551615,"h":1e400}')
    assert error_of(answer) == (422, "number_out_of_range")
    assert answer.json()["error"]["detail"] == {"paths": ["$.u", "$.h"]}
    assert not [sql for sql in sent if sql.startswith(("UPDATE", "SELECT RAW"))]  # refused before anything is sent


def test_invalid_json_points_at_the_spot_in_utf16(app, cb):
    client, _ = app
    answer = post(client, "save", profile_id=cb, keyspace=ORDERS, id=NEVER, cas="1", text='{"😀": 1,}')
    assert error_of(answer) == (422, "invalid_json")
    assert answer.json()["error"]["detail"] == {"offset": 8, "reason": "Illegal trailing comma before end of object"}


def test_read_only_profiles_cannot_write(app):  # M11
    client, _ = app
    body = {"profile": {**COUCHBASE, "read_only": True}, "secrets": {"password": COUCHBASE_PASSWORD}}
    read_only = client.post("/profiles", json=body, headers=AUTH).json()["profile"]["id"]
    assert post(client, "get", profile_id=read_only, keyspace=USERS, id="u1").status_code == 200
    for path, extra in (("save", {"cas": "1", "text": "{}"}), ("insert", {"text": "{}"}), ("remove", {"cas": "1"})):
        assert error_of(post(client, path, profile_id=read_only, keyspace=ORDERS, id=NEVER, **extra)) == (403, "read_only")


def test_a_binary_document_is_refused(app, cb, scratch):
    client, _ = app
    key = new_document(scratch, "not json")
    view = post(client, "get", profile_id=cb, keyspace=ORDERS, id=key).json()
    assert (view["type"], view["text"], view["size"]) == ("base64", None, 8)
    answer = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=view["cas"], text='{"x":1}')
    assert error_of(answer) == (422, "binary_document") and stored_document(key).type == "base64"


def test_insert_previews_then_creates_and_remove_deletes(app, cb, scratch):
    client, _ = app
    key = scratch_key()
    scratch.append(key)
    text = '{"b": 1, "a": 2}'
    preview = post(client, "insert", profile_id=cb, keyspace=ORDERS, id=key, text=text).json()
    assert preview["status"] == "needs_confirmation" and preview["counts"]["key_order"] == 1
    assert stored_document(key) is None
    saved = post(client, "insert", profile_id=cb, keyspace=ORDERS, id=key, text=text, confirmed=True).json()
    assert saved["status"] == "saved" and saved["expiration"] == 0
    assert error_of(post(client, "insert", profile_id=cb, keyspace=ORDERS, id=key, text="{}")) == (409, "document_exists")
    assert post(client, "remove", profile_id=cb, keyspace=ORDERS, id=key, cas=saved["cas"]).status_code == 204
    assert error_of(post(client, "get", profile_id=cb, keyspace=ORDERS, id=key)) == (404, "document_missing")


def test_a_write_whose_answer_is_lost_is_not_sent_again(app, cb, scratch, monkeypatch):  # Review Focus 4, D-04-9
    client, _ = app
    key = new_document(scratch, '{"a":1}')
    cas = post(client, "get", profile_id=cb, keyspace=ORDERS, id=key).json()["cas"]
    sent = []
    begin = CouchbaseConnection._begin

    def losing(self, sql, **kwargs):
        stream = begin(self, sql, **kwargs)
        if not sql.startswith("UPDATE"):
            return stream
        sent.append(sql)
        while stream.next() is not None:  # the server has answered: the write is done
            pass
        self._end()
        raise StatementError("Connection to the query node was lost: the test dropped it", code="connection_lost")

    monkeypatch.setattr(CouchbaseConnection, "_begin", losing)
    answer = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas, text='{"a":2}')
    assert error_of(answer) == (502, "write_outcome_unknown")
    assert len(sent) == 1 and stored_document(key).text == '{"a":2}'
    monkeypatch.undo()
    again = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas, text='{"a":2}')
    assert error_of(again) == (409, "document_conflict")  # a second write never lands on top of the first


def test_document_text_and_ids_never_reach_the_info_log(app, cb, scratch):  # spec §5
    client, _ = app
    key = new_document(scratch, '{"a":"zz-marker-before"}')
    records = []
    sink = logger.add(lambda message: records.append((message.record["level"].no, message.record["message"])),
                      level=0)
    try:
        cas = post(client, "get", profile_id=cb, keyspace=ORDERS, id=key).json()["cas"]
        text = '{"b":"zz-marker-after","a":"zz-marker-before"}'
        post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas, text=text)
        post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas, text=text, confirmed=True)
        post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas="1", text='{"u":18446744073709551615}')
    finally:
        logger.remove(sink)
    assert not [text for _, text in records if "zz-marker" in text]
    assert not [text for level, text in records if level >= 20 and (key in text or "$.u" in text)]
    assert [text for level, text in records if level < 20 and key in text]  # the ID only at DEBUG


def test_other_engines_have_no_documents(app, tmp_path):
    client, _ = app
    sqlite = client.post("/profiles", json={"profile": {"engine": "sqlite", "name": "s", "path": str(tmp_path / "x.db")}},
                         headers=AUTH).json()["profile"]["id"]
    assert error_of(post(client, "get", profile_id=sqlite, keyspace=USERS, id="u1")) == (422, "validation_failed")


def test_nesting_too_deep_to_check_is_refused_before_anything_is_sent(app, cb):  # ruling C27
    client, _ = app
    text = '{"a":' * 1100 + "12345678901234567890" + "}" * 1100  # valid JSON; blocked_numbers walks it
    answer = post(client, "save", profile_id=cb, keyspace=ORDERS, id=NEVER, cas="1", text=text)
    assert error_of(answer) == (422, "invalid_json")


# ---- the order of the checks, no second run of a write, the depth mapping, numbers refused when confirmed ----


@pytest.fixture
def no_connection(app, monkeypatch):
    """Every metadata call fails the test: what it guards must be refused before any connection is used."""
    _, container = app
    used = []

    def connection(*args, **kwargs):
        used.append(args)
        raise AssertionError("a connection was used")

    monkeypatch.setattr(container.metadata, "with_connection", connection)
    return used


@pytest.mark.parametrize(("path", "extra", "text"), [
    ("save", {"cas": "1"}, '{"a": 1,}'),
    ("insert", {}, '{"a":' * 101 + "1" + "}" * 101),  # deeper than MAX_DOCUMENT_DEPTH
    ("insert", {}, ""),
])
def test_a_text_that_is_not_json_is_refused_before_any_connection_is_used(app, cb, no_connection, path, extra, text):
    client, _ = app
    answer = post(client, path, profile_id=cb, keyspace=ORDERS, id=NEVER, text=text, **extra)
    assert error_of(answer) == (422, "invalid_json") and not no_connection


def test_an_id_beyond_250_utf8_bytes_is_refused_before_any_connection_is_used(app, cb, no_connection):
    client, _ = app
    answer = post(client, "get", profile_id=cb, keyspace=ORDERS, id="é" * 126)  # 126 characters, 252 bytes
    assert error_of(answer) == (422, "validation_failed") and not no_connection


def test_what_utf8_cannot_carry_is_refused_by_the_service_too(app, cb, no_connection):
    """The request layer already refuses a lone surrogate (`string_unicode`); the service does not rely on it."""
    _, container = app
    keyspace = Keyspace(**ORDERS)
    with pytest.raises(ValidationFailed, match="document ID holds a character"):
        container.documents.get(UUID(cb), keyspace, "zz_04c_\ud800")
    with pytest.raises(ValidationFailed, match="document holds a character"):
        container.documents.save(UUID(cb), keyspace, NEVER, "1", '{"a": "\ud800"}', False)
    assert not no_connection


WRITES = {"save": "UPDATE", "insert": "INSERT", "remove": "DELETE"}


@pytest.mark.parametrize("write", list(WRITES))
def test_a_write_that_fails_after_it_went_out_is_never_run_again(app, cb, scratch, monkeypatch, write):  # C26
    """A failure that is not a DomainError (a bug, the worker call's timeout) after the server answered the write:
    without `retry=False` the metadata call would reconnect and run the whole call again, write included (an insert
    would then report "exists", a remove "missing")."""
    client, _ = app
    if write == "insert":
        key = scratch_key()
        scratch.append(key)
        body = {"text": '{"a":2}'}
    else:
        key = new_document(scratch, '{"a":1}')
        cas = post(client, "get", profile_id=cb, keyspace=ORDERS, id=key).json()["cas"]
        body = {"cas": cas, "text": '{"a":2}'} if write == "save" else {"cas": cas}
    sent = []
    begin = CouchbaseConnection._begin

    def failing(self, sql, **kwargs):
        stream = begin(self, sql, **kwargs)
        if not sql.startswith(WRITES[write]):
            return stream
        sent.append(sql)
        while stream.next() is not None:  # the server has answered: the write is done
            pass
        self._end()
        raise RuntimeError("the test failed the call after the write")

    monkeypatch.setattr(CouchbaseConnection, "_begin", failing)
    answer = post(client, write, profile_id=cb, keyspace=ORDERS, id=key, **body)
    assert error_of(answer) == (400, "query_failed") and len(sent) == 1
    stored = stored_document(key)
    assert stored is None if write == "remove" else stored.text == '{"a":2}'


@pytest.mark.parametrize("check", ["blocked_numbers", "diff"])
def test_a_document_too_deep_for_the_checks_is_invalid_json(app, cb, monkeypatch, check):  # ruling C27
    """`json_problem` refuses more than MAX_DOCUMENT_DEPTH levels, so this cannot happen today; were the limit raised
    past Python's recursion limit, the checks' RecursionError must not drop the connection as a failed call."""
    client, _ = app

    def too_deep(*args):
        raise RecursionError("maximum recursion depth exceeded")

    if check == "diff":
        monkeypatch.setattr(document_service, "diff", too_deep)
    else:
        monkeypatch.setattr(SqlppDocumentStore, "blocked_numbers", too_deep)
    answer = post(client, "save", profile_id=cb, keyspace=ORDERS, id=NEVER, cas="1", text='{"a":1}')
    assert error_of(answer) == (422, "invalid_json")
    assert answer.json()["error"]["detail"] == {"offset": 0, "reason": "Nested too deeply to check before saving"}


def test_numbers_the_server_would_change_are_refused_even_when_confirmed(app, cb, scratch):  # ruling C4
    client, _ = app
    key = scratch_key()
    scratch.append(key)  # removed afterwards should the refusal ever fail and the insert write it
    text = '{"u":18446744073709551615}'
    for path, extra in (("save", {"id": NEVER, "cas": "1"}), ("insert", {"id": key})):
        answer = post(client, path, profile_id=cb, keyspace=ORDERS, text=text, confirmed=True, **extra)
        assert error_of(answer) == (422, "number_out_of_range")
    assert stored_document(key) is None
