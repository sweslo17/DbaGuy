"""Sample fields (R-04-11): field paths derived in Python from documents read with a plain SELECT, never INFER; binary
documents count as binary; the sample stops at 32 MiB; results are cached per profile and keyspace."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from loguru import logger

from dbaguy_backend.adapters.couchbase import sampling
from dbaguy_backend.adapters.couchbase.adapter import CouchbaseConnection
from dbaguy_backend.adapters.base import FieldSampling
from dbaguy_backend.adapters.couchbase.sampling import FieldCollector, parse_sample_row, sample_statement
from dbaguy_backend.domain.couchbase import FieldSample, Keyspace
from dbaguy_backend.domain.errors import ValidationFailed
from dbaguy_backend.services.field_sample_service import SAMPLES_PER_PROFILE_MAX, FieldSampleService
from dbaguy_backend.services.settings_service import SettingsService
from dbaguy_backend.sql.dialects import Dialect, quote_ident
from tests.support import COUCHBASE_PASSWORD, COUCHBASE_READER, couchbase_query, requires_couchbase
from tests.test_api import AUTH
from tests.test_couchbase_adapter import open_couchbase
from tests.test_couchbase_api import COUCHBASE, app, cb  # noqa: F401  (fixtures)

USERS = Keyspace(bucket="dbaguy", scope="app", collection="users")


def collect(*documents: tuple[str, str]) -> FieldCollector:
    collector = FieldCollector()
    for meta_type, text in documents:
        collector.add(meta_type, text)
    return collector


def by_path(collector: FieldCollector):
    return {tuple(f.path): f for f in collector.fields()}


def test_paths_types_and_shares():
    collector = collect(
        ("json", '{"name":"Ann","age":31,"tags":["a","b"],"address":{"city":"Taipei","zip":"100"}}'),
        ("json", '{"name":"Bob","age":null,"address":{"city":"Tainan"}}'),
        ("json", '"just a string document"'),
        ("base64", '"<binary (9 b)>"'),
    )
    fields = by_path(collector)
    assert collector.documents == 4
    assert fields[()].types == {"object": 2, "string": 1, "binary": 1} and fields[()].docs == 4
    age = fields[("age",)]
    assert (age.types, age.docs, age.percent) == ({"number": 1, "null": 1}, 2, 50.0)
    assert fields[("tags", None)].types == {"string": 1} and fields[("tags", None)].docs == 1  # once per document
    assert fields[("address", "zip")].percent == 25.0
    assert list(fields)[:5] == [(), ("name",), ("age",), ("tags",), ("tags", None)]  # first-seen order


def test_array_elements_and_the_depth_cap():
    deep = '{"a":{"b":{"c":{"d":{"e":{"f":{"g":1}}}}}}}'
    collector = collect(("json", '{"items":[{"sku":1},{"sku":2,"qty":3}]}'), ("json", deep))
    paths = [tuple(f.path) for f in collector.fields()]
    assert ("items", None, "sku") in paths and ("items", None, "qty") in paths
    assert max(len(p) for p in paths) == 6 and ("a", "b", "c", "d", "e", "f") in paths
    assert not collector.paths_capped and not collector.long_keys_skipped  # each cap has its own flag
    assert collector.depth_capped  # `g` is below the cap and is not listed: the sample says so (ruling B50)


def test_the_depth_cap_is_reported_only_when_something_below_it_is_left_out():
    at_the_cap = '{"a":{"b":{"c":{"d":{"e":{"f":%s}}}}}}'
    for value in ('1', '"text"', 'null', '{}', '[]'):  # a scalar or an empty container at the cap loses nothing
        collector = collect(("json", at_the_cap % value))
        assert not collector.depth_capped and ("a", "b", "c", "d", "e", "f") in by_path(collector), value
    for value in ('{"g":1}', '[1]', '[[]]', '[{"g":1}]'):  # the first thing below the cap that is not listed
        collector = collect(("json", at_the_cap % value))
        assert collector.depth_capped and not collector.paths_capped, value
        assert max(len(p) for p in by_path(collector)) == 6, value
    assert not collect(("json", '{"a":{"b":1}}')).depth_capped
    assert collect(("json", '{"ok":1}'), ("json", at_the_cap % '{"g":1}')).depth_capped  # any one document is enough


def flags(collector: FieldCollector) -> tuple[bool, bool, bool]:
    return collector.paths_capped, collector.long_keys_skipped, collector.depth_capped


def test_each_cap_sets_its_own_flag(monkeypatch):
    monkeypatch.setattr(sampling, "FIELD_PATHS_MAX", 2)
    assert flags(collect(("json", '{"a":1,"b":2}'))) == (True, False, False)  # the number of paths
    monkeypatch.undo()
    long = json.dumps({"k" * (sampling.FIELD_KEY_MAX_CHARS + 1): 1})
    assert flags(collect(("json", long))) == (False, True, False)  # a key too long to list
    assert flags(collect(("json", '{"a":{"b":{"c":{"d":{"e":{"f":{"g":1}}}}}}}'))) == (False, False, True)  # depth
    assert flags(collect(("json", '{"a":{"b":1}}'))) == (False, False, False)


def test_a_sample_reports_the_limits_it_works_to(monkeypatch):
    assert sampling.limits().model_dump() == {"max_bytes": 32 * 1024 * 1024, "max_paths": 2000, "max_depth": 6,
                                              "max_key_chars": 1024}
    monkeypatch.setattr(sampling, "SAMPLE_MAX_BYTES", 1024)  # read when asked: a test that lowers a cap is believed
    monkeypatch.setattr(sampling, "FIELD_PATHS_MAX", 5)
    assert (sampling.limits().max_bytes, sampling.limits().max_paths) == (1024, 5)


def test_paths_display_as_sqlpp_writes_them():
    document = {"x": {"a.b": 1, "c": 2}, "odd key": 3, "": 4, "[]": 5, "value": 6, "a`b": 7, "a\\b": 8, "(document)": 9,
                "tags": [{"k": 1}], "ünï": 10}
    shown = {tuple(f.path): f.display for f in collect(("json", json.dumps(document))).fields()}
    assert shown[()] == "(document)"
    assert shown[("x",)] == "x" and shown[("x", "c")] == "x.c"
    assert shown[("x", "a.b")] == "x.`a.b`"  # a dot inside a key is not nesting
    assert shown[("odd key",)] == "`odd key`"
    assert shown[("",)] == "``" and shown[("[]",)] == "`[]`" and shown[("(document)",)] == "`(document)`"
    assert shown[("value",)] == "`value`"  # a reserved word
    assert shown[("a`b",)] == "`a``b`"  # the closing back-tick doubles
    assert shown[("a\\b",)] == "`a\\\\b`"  # and so do backslashes, which escape inside back-ticks
    assert shown[("ünï",)] == "`ünï`"  # SQL++ names are ASCII
    assert shown[("tags",)] == "tags" and shown[("tags", None)] == "tags.[]"
    assert shown[("tags", None, "k")] == "tags.[].k"
    assert len(set(shown.values())) == len(shown)  # no two paths read alike


def test_a_displayed_key_is_the_one_completion_inserts():
    for key in ("name", "odd key", "a.b", "select", "x-y", "9lives", "", "[]", "naïve"):
        assert sampling.display_path((key,)) == quote_ident(Dialect.SQLPP, key)


def test_the_path_cap(monkeypatch):
    monkeypatch.setattr(sampling, "FIELD_PATHS_MAX", 5)
    collector = collect(("json", '{"a":1,"b":2,"c":3,"d":4,"e":5,"f":6}'))
    assert len(collector.fields()) == 5 and flags(collector) == (True, False, False)


def test_numbers_are_never_parsed():
    fields = by_path(collect(("json", '{"huge":1e400,"big":' + "9" * 5000 + ',"pi":3.14}')))
    assert fields[("huge",)].types == fields[("big",)].types == {"number": 1}


def test_a_path_keeps_counting_after_the_cap_and_each_kind_counts_once_per_document(monkeypatch):
    monkeypatch.setattr(sampling, "FIELD_PATHS_MAX", 4)
    collector = collect(("json", '{"a":1,"b":[1,"x",2,"y"]}'), ("json", '{"a":"s","c":1}'), ("json", '{"b":[true]}'))
    fields = by_path(collector)
    assert list(fields) == [(), ("a",), ("b",), ("b", None)] and collector.paths_capped  # `c` came after the cap
    assert (fields[("a",)].docs, fields[("a",)].types) == (2, {"number": 1, "string": 1})
    assert (fields[("b",)].docs, fields[("b",)].types) == (2, {"array": 2})
    assert (fields[("b", None)].docs, fields[("b", None)].types) == (2, {"number": 1, "string": 1, "boolean": 1})
    assert fields[()].docs == 3 and fields[()].types == {"object": 3} and fields[()].percent == 100.0


def test_a_key_over_the_limit_is_left_out_with_everything_below_it():
    edge, long = "e" * sampling.FIELD_KEY_MAX_CHARS, "k" * (sampling.FIELD_KEY_MAX_CHARS + 1)
    collector = collect(("json", json.dumps({"ok": 1, edge: 2, long: {"inner": 3}})))
    assert [tuple(f.path) for f in collector.fields()] == [(), ("ok",), (edge,)]
    assert flags(collector) == (False, True, False)  # not `paths_capped`: far fewer than FIELD_PATHS_MAX were kept
    assert flags(collect(("json", '{"a":{"b":1}}'))) == (False, False, False)


def test_the_sample_statement_quotes_names():
    assert sample_statement(Keyspace(bucket="travel-sample", scope="inventory", collection="airline")) == (
        "SELECT META(d).type AS t, d FROM `travel-sample`.inventory.airline AS d LIMIT $n")
    assert sample_statement(Keyspace(bucket="dbaguy", scope="app", collection="value")).endswith(
        "dbaguy.app.`value` AS d LIMIT $n")


def test_a_sample_row_is_decoded_once_into_its_type_and_document():
    meta_type, document = parse_sample_row('{"d":{"n":1.10,"big":1e400,"s":"x","o":{"a":[1,null]}},"t":"json"}')
    assert meta_type == "json"  # either order of the two members
    collector = FieldCollector()
    collector.add_decoded(meta_type, document)
    fields = by_path(collector)
    assert fields[("big",)].types == {"number": 1} and fields[("s",)].types == {"string": 1}
    assert fields[("o", "a", None)].types == {"number": 1, "null": 1} and collector.documents == 1
    binary = FieldCollector()
    binary.add_decoded(*parse_sample_row('{"t":"base64","d":"<binary (9 b)>"}'))
    assert [(tuple(f.path), f.types) for f in binary.fields()] == [((), {"binary": 1})]


def test_what_is_not_a_sample_row_is_refused():
    for bad in ('{"d":1}', '{"t":1,"d":1}', '{"t":"json"}', "[1]", "7", "{not json"):
        with pytest.raises(ValueError):
            parse_sample_row(bad)
    with pytest.raises((ValueError, RecursionError)):  # nesting beyond what the decoder takes
        parse_sample_row('{"t":"json","d":' + "[" * 100_000 + "}")


@requires_couchbase
def test_sampling_users_sees_every_shape():
    connection = open_couchbase()
    try:
        sample = connection.sample_fields(USERS, 1000)
    finally:
        connection.close()
    fields = {tuple(f.path): f for f in sample.fields}
    assert sample.documents == couchbase_query("SELECT RAW COUNT(*) FROM dbaguy.app.users")["results"][0]
    assert not sample.cut_at_bytes and sample.requested == 1000
    assert {"object", "string", "array", "number"} <= set(fields[()].types)
    assert fields[("u64max",)].types == {"number": 1} and ("address", "city") in fields
    assert not (sample.paths_capped or sample.long_keys_skipped or sample.depth_capped)
    assert sample.limits == sampling.limits()
    shown = {tuple(f.path): f.display for f in sample.fields}
    assert shown[()] == "(document)" and shown[("address", "city")] == "address.city"
    assert shown[("odd key",)] == "`odd key`"


@requires_couchbase
def test_binary_documents_count_as_binary():  # A24: SELECT RAW d gives them as "<binary (N b)>" strings
    connection = open_couchbase()
    try:
        sample = connection.sample_fields(Keyspace(bucket="dbaguy", scope="app", collection="blobs"), 100)
    finally:
        connection.close()
    assert {tuple(f.path): f for f in sample.fields}[()].types == {"object": 1, "binary": 2}


@requires_couchbase
def test_the_sample_stops_at_its_byte_cap(monkeypatch):
    monkeypatch.setattr(sampling, "SAMPLE_MAX_BYTES", 1024 * 1024)  # u_big alone is 5 MiB
    connection = open_couchbase()
    try:
        sample = connection.sample_fields(USERS, 1000)
        assert connection.query("SELECT RAW 1") == [1]  # the cut request left the connection usable
    finally:
        connection.close()
    total = couchbase_query("SELECT RAW COUNT(*) FROM dbaguy.app.users")["results"][0]
    assert sample.cut_at_bytes and sample.documents < total
    assert sample.limits.max_bytes == 1024 * 1024  # the cap that cut it, as reported


@requires_couchbase
def test_a_sample_of_exactly_the_requested_documents_is_not_a_cut(monkeypatch):
    monkeypatch.setattr(sampling, "SAMPLE_MAX_BYTES", 1024)
    connection = open_couchbase()
    try:
        sample = connection.sample_fields(Keyspace(bucket="dbaguy", scope="app", collection="orders"), 10)
    finally:
        connection.close()
    assert sample.documents == 10 and not sample.cut_at_bytes  # the cap is crossed by the last document asked for


@requires_couchbase
def test_sampling_never_puts_a_document_or_its_names_in_the_log(app, cb):
    client, _ = app
    log: list[str] = []
    sink = logger.add(lambda m: log.append(m.record["message"]), level="DEBUG")
    try:
        response = client.post("/couchbase/fields/sample", json={"profile_id": cb, "keyspace": USERS.model_dump()},
                               headers=AUTH)
    finally:
        logger.remove(sink)
    assert response.status_code == 200
    text = "\n".join(log)
    assert "Sampled" in text and "Couchbase sample:" in text
    for secret in ("Taipei", "Tainan", "no name field", "needs quoting", "Expires in 2100", "just a string"):
        assert secret not in text


@requires_couchbase
def test_a_server_error_of_a_sample_is_sent_once_and_shown_as_it_is(app, cb, monkeypatch):  # B39
    client, _ = app
    sent = []
    sample_fields = CouchbaseConnection.sample_fields

    def counting(self, keyspace, size):
        sent.append(keyspace)
        return sample_fields(self, keyspace, size)

    monkeypatch.setattr(CouchbaseConnection, "sample_fields", counting)
    missing = {"bucket": "dbaguy", "scope": "app", "collection": "no_such_collection"}
    response = client.post("/couchbase/fields/sample", json={"profile_id": cb, "keyspace": missing}, headers=AUTH)
    error = response.json()["error"]
    assert response.status_code == 400 and error["code"] == "query_failed" and "no_such_collection" in error["message"]
    assert len(sent) == 1
    assert client.get("/couchbase/fields", params={"profile_id": cb, **missing}, headers=AUTH).json() is None


@requires_couchbase
def test_a_user_who_may_not_read_the_keyspace_gets_the_servers_refusal(app):
    client, _ = app
    body = {"profile": {**COUCHBASE, "name": "reader", "username": COUCHBASE_READER},
            "secrets": {"password": COUCHBASE_PASSWORD}}
    reader = client.post("/profiles", json=body, headers=AUTH).json()["profile"]["id"]
    allowed = client.post("/couchbase/fields/sample", json={"profile_id": reader, "keyspace": USERS.model_dump(),
                                                            "size": 10}, headers=AUTH)
    assert allowed.status_code == 200 and allowed.json()["documents"] == 10
    orders = {"bucket": "dbaguy", "scope": "app", "collection": "orders"}
    refused = client.post("/couchbase/fields/sample", json={"profile_id": reader, "keyspace": orders}, headers=AUTH)
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "query_failed"


@requires_couchbase
def test_the_api_caches_until_sampled_again_or_the_profile_changes(app, cb):
    client, _ = app
    keyspace = {"bucket": "dbaguy", "scope": "app", "collection": "orders"}
    query = {"profile_id": cb, **keyspace}
    assert client.get("/couchbase/fields", params=query, headers=AUTH).json() is None
    sample = client.post("/couchbase/fields/sample", json={"profile_id": cb, "keyspace": keyspace},
                         headers=AUTH).json()
    assert (sample["requested"], sample["documents"]) == (1000, 500)  # the settings' default size; o0 … o499
    assert [sample[flag] for flag in ("paths_capped", "long_keys_skipped", "depth_capped")] == [False] * 3
    assert sample["limits"] == {"max_bytes": 32 * 1024 * 1024, "max_paths": 2000, "max_depth": 6, "max_key_chars": 1024}
    assert sample["fields"][0]["path"] == [] and sample["fields"][0]["display"] == "(document)"
    assert client.get("/couchbase/fields", params=query, headers=AUTH).json()["sampled_at"] == sample["sampled_at"]
    small = client.post("/couchbase/fields/sample", json={"profile_id": cb, "keyspace": keyspace, "size": 10},
                        headers=AUTH).json()
    assert small["documents"] == 10
    too_small = client.post("/couchbase/fields/sample", json={"profile_id": cb, "keyspace": keyspace, "size": 5},
                            headers=AUTH)
    assert too_small.status_code == 422
    client.put(f"/profiles/{cb}", json={"profile": {**COUCHBASE, "name": "renamed"}}, headers=AUTH)
    assert client.get("/couchbase/fields", params=query, headers=AUTH).json() is None


@requires_couchbase
def test_the_setting_decides_the_default_size(app, cb):
    client, _ = app
    settings = client.get("/settings", headers=AUTH).json()
    assert settings["couchbase"] == {"sample_size": 1000}
    client.put("/settings", json={**settings, "couchbase": {"sample_size": 25}}, headers=AUTH)
    keyspace = {"bucket": "dbaguy", "scope": "app", "collection": "orders"}
    sample = client.post("/couchbase/fields/sample", json={"profile_id": cb, "keyspace": keyspace}, headers=AUTH).json()
    assert (sample["requested"], sample["documents"]) == (25, 25)
    too_small = client.put("/settings", json={**settings, "couchbase": {"sample_size": 9}}, headers=AUTH)
    assert too_small.status_code == 422


def test_other_engines_have_no_field_sampling(app, tmp_path):
    client, _ = app
    sqlite = client.post("/profiles", json={"profile": {"engine": "sqlite", "name": "s", "path": str(tmp_path / "x.db")}},
                         headers=AUTH).json()["profile"]["id"]
    keyspace = {"bucket": "b", "scope": "s", "collection": "c"}
    sample = client.post("/couchbase/fields/sample", json={"profile_id": sqlite, "keyspace": keyspace}, headers=AUTH)
    assert sample.status_code == 422 and "not a Couchbase connection" in sample.json()["error"]["message"]
    assert client.get("/couchbase/fields", params={"profile_id": sqlite, **keyspace}, headers=AUTH).status_code == 422


def test_a_connection_that_cannot_sample_is_refused_and_caches_nothing(tmp_path):
    class Metadata:
        def with_connection(self, profile_id, database, fn):
            return fn(object())  # not a FieldSampling connection

    service = FieldSampleService(Metadata(), SettingsService(tmp_path / "settings.json", tmp_path / "scripts"))
    profile_id = uuid4()
    with pytest.raises(ValidationFailed, match="cannot sample documents"):
        service.sample(profile_id, USERS, 10)
    assert service.cached(profile_id, USERS) is None


def sample_of(keyspace: Keyspace) -> FieldSample:
    return FieldSample(keyspace=keyspace, sampled_at=datetime.now(UTC), requested=10, documents=0, cut_at_bytes=False,
                       paths_capped=False, long_keys_skipped=False, depth_capped=False, limits=sampling.limits(),
                       elapsed_ms=0, fields=[])


def test_the_cache_keeps_the_most_recently_used_samples_of_each_profile(tmp_path):
    class Connection(FieldSampling):
        def sample_fields(self, keyspace, size):
            return sample_of(keyspace)

    class Metadata:
        def with_connection(self, profile_id, database, fn):
            return fn(Connection())

    service = FieldSampleService(Metadata(), SettingsService(tmp_path / "settings.json", tmp_path / "scripts"))
    mine, other = uuid4(), uuid4()
    keyspaces = [Keyspace(bucket="b", scope="s", collection=f"c{i}") for i in range(SAMPLES_PER_PROFILE_MAX + 1)]
    for keyspace in keyspaces[:SAMPLES_PER_PROFILE_MAX]:
        service.sample(mine, keyspace, 10)
    service.sample(other, keyspaces[0], 10)
    assert service.cached(mine, keyspaces[0]) is not None  # a read is a use: c1 is now the least recently used
    service.sample(mine, keyspaces[-1], 10)  # one more than the bound
    assert service.cached(mine, keyspaces[1]) is None
    assert all(service.cached(mine, k) is not None for k in (keyspaces[0], keyspaces[2], keyspaces[-1]))
    assert service.cached(other, keyspaces[0]) is not None  # another profile's samples are its own
    service.invalidate(mine)
    assert service.cached(mine, keyspaces[0]) is None and service.cached(other, keyspaces[0]) is not None
