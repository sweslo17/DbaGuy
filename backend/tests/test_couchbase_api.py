"""The Couchbase endpoints over a real dev cluster: query contexts for the picker, plan analysis, index advice on the
metadata connection, history entries' contexts, ⌘P over keyspaces."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from dbaguy_backend.adapters.couchbase.adapter import CouchbaseAdapter, CouchbaseConnection
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.app import create_app
from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.execution import QueryContext
from dbaguy_backend.domain.history import HistoryEntry, HistoryPage, HistoryRecord
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.domain.schema import SchemaSnapshot, SnapshotKeyspace
from dbaguy_backend.services.couchbase_service import CouchbaseService
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.services.schema_cache import SchemaCache
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.support import COUCHBASE_PASSWORD, plan_fixture, requires_couchbase
from tests.test_api import AUTH, BASE_URL, TOKEN

COUCHBASE = {"engine": "couchbase", "name": "cb", "host": "127.0.0.1", "database": "dbaguy.app",
             "username": "dbaguy", "ssl": {"mode": "disable"}, "save_password": False}


@pytest.fixture
def app(tmp_path):
    settings = Settings(token=TOKEN, data_dir=tmp_path / "data", log_to_file=False)
    container = AppContainer(settings, secret_store=InMemorySecretStore())
    with TestClient(create_app(settings, container), base_url=BASE_URL) as client:
        yield client, container


@pytest.fixture
def cb(app):
    client, _ = app
    body = {"profile": COUCHBASE, "secrets": {"password": COUCHBASE_PASSWORD}}
    return client.post("/profiles", json=body, headers=AUTH).json()["profile"]["id"]


def entry(profile_id, database, name="cb"):
    return HistoryEntry(id=1, profile_id=profile_id, profile_name=name, database=database, sql="SELECT 1",
                        started_at=datetime.now(UTC), elapsed_ms=1, rows=1, status="ok")


def test_statement_keyspace_answers_the_collection_a_statement_reads_or_null(app):  # ruling C44, no cluster needed
    client, _ = app

    def ask(sql, context=None):
        body = {"sql": sql, **({"query_context": context} if context else {})}
        response = client.post("/couchbase/statement-keyspace", json=body, headers=AUTH)
        assert response.status_code == 200
        return response.json()

    users = {"bucket": "dbaguy", "scope": "app", "collection": "users"}
    travel = {"bucket": "travel-sample", "scope": "inventory"}
    assert ask("SELECT META(d).id, * FROM dbaguy.app.users AS d", travel) == users  # not the tab's context
    assert ask("SELECT * FROM users", {"bucket": "dbaguy", "scope": "app"}) == users
    assert ask("SELECT * FROM app.users", {"bucket": "dbaguy"}) == users
    assert ask("SELECT * FROM users u JOIN orders o ON o.uid = u.id", travel) is None  # two keyspaces
    assert ask("SELECT * FROM users") == {"bucket": "users", "scope": "_default", "collection": "_default"}
    assert ask("SELECT 1") is None


@pytest.mark.parametrize("body", [
    {"sql": ""}, {"sql": "x" * (1 << 20) + "y"}, {"sql": "SELECT 1", "query_context": {"bucket": ""}},
    {"sql": "SELECT 1", "profile_id": "x"}, {},
])
def test_statement_keyspace_bounds_its_inputs(app, body):
    client, _ = app
    assert client.post("/couchbase/statement-keyspace", json=body, headers=AUTH).status_code == 422


@requires_couchbase
def test_contexts_list_buckets_and_scopes_with_their_texts(app, cb):
    client, _ = app
    options = client.get("/couchbase/contexts", params={"profile_id": cb}, headers=AUTH).json()
    pairs = [(o["bucket"], o["scope"]) for o in options]
    for pair in [("dbaguy", None), ("dbaguy", "_default"), ("dbaguy", "app"), ("travel-sample", "inventory")]:
        assert pair in pairs
    assert pairs.index(("dbaguy", None)) < pairs.index(("dbaguy", "_default")) < pairs.index(("dbaguy", "app"))
    texts = {(o["bucket"], o["scope"]): o["text"] for o in options}
    assert texts[("travel-sample", "inventory")] == "travel-sample.inventory" and all(o["listed"] for o in options)


@requires_couchbase
def test_the_current_context_is_listed_even_when_the_snapshot_lacks_it(app, cb):  # Review Focus 5
    client, _ = app
    options = client.get("/couchbase/contexts", params={"profile_id": cb, "bucket": "my.bucket", "scope": "app"},
                         headers=AUTH).json()
    assert options[0] == {"bucket": "my.bucket", "scope": "app", "text": "my.bucket.app", "listed": False}
    alone = client.get("/couchbase/contexts", params={"profile_id": cb, "bucket": "a`b"}, headers=AUTH).json()[0]
    assert alone["text"] == QueryContext(bucket="a`b").text() and QueryContext.parse(alone["text"]).bucket == "a`b"
    listed = client.get("/couchbase/contexts", params={"profile_id": cb, "bucket": "dbaguy", "scope": "app"},
                        headers=AUTH).json()
    assert [o for o in listed if (o["bucket"], o["scope"]) == ("dbaguy", "app")] == [
        {"bucket": "dbaguy", "scope": "app", "text": "dbaguy.app", "listed": True}]


def test_contexts_refuse_other_engines(app, tmp_path):
    client, _ = app
    sqlite = client.post("/profiles", json={"profile": {"engine": "sqlite", "name": "s", "path": str(tmp_path / "x.db")}},
                         headers=AUTH).json()["profile"]["id"]
    response = client.get("/couchbase/contexts", params={"profile_id": sqlite}, headers=AUTH)
    assert response.status_code == 422 and "not a Couchbase connection" in response.json()["error"]["message"]


def test_plan_of_text_that_cannot_be_sent_is_unknown_not_a_500(app):
    """`"\\ud800"` is valid JSON, but the lone surrogate it names is not UTF-8: the view could not be answered."""
    client, _ = app
    text = '{"plan": {"#operator": "IndexScan3", "keyspace": "\\ud800", "index": "i"}, "text": "t"}'
    response = client.post("/couchbase/plan", json={"text": text}, headers=AUTH)
    assert response.status_code == 200 and response.json()["kind"] == "unknown"
    assert client.post("/couchbase/plan", json={"text": text.replace("\\ud800", "k")}, headers=AUTH).json()["kind"] == "plan"


def test_plan_analyses_a_text(app):
    client, _ = app
    view = client.post("/couchbase/plan", json={"text": plan_fixture("explain_fetch")}, headers=AUTH).json()
    assert view["kind"] == "plan" and [f["badge"] for f in view["findings"]] == ["not_covered"]


@requires_couchbase
def test_advise_runs_on_the_metadata_connection_in_the_given_context(app, cb):
    client, _ = app
    body = {"profile_id": cb, "statement": 'SELECT * FROM orders WHERE customer = "c1"',
            "query_context": {"bucket": "dbaguy", "scope": "app"}}
    view = client.post("/couchbase/advise", json=body, headers=AUTH).json()
    assert view["kind"] == "advice"
    assert any(a["kind"] == "recommended" and "adv_customer" in a["statement"] for a in view["advice"])


@requires_couchbase
def test_advise_qualifies_the_index_by_the_context_it_was_sent(app, cb):
    """The server recommends an index for a keyspace it never looked up, so only the statement tells the context was
    sent: it names the bucket and scope (the statement can be copied and run as it is, R-04-13)."""
    client, _ = app
    body = {"profile_id": cb, "statement": 'SELECT * FROM orders WHERE customer = "c1"'}

    def recommended(**extra):
        view = client.post("/couchbase/advise", json={**body, **extra}, headers=AUTH).json()
        return [a["statement"] for a in view["advice"] if a["kind"] == "recommended"]

    assert all("`dbaguy`.`app`.`orders`" in s for s in recommended(query_context={"bucket": "dbaguy", "scope": "app"}))
    assert all("`dbaguy`" not in s for s in recommended())


@requires_couchbase
def test_advise_shows_the_servers_error(app, cb):  # A15: ADVISE on app.users panics on 7.6.12
    client, _ = app
    body = {"profile_id": cb, "statement": 'SELECT * FROM users WHERE name = "Ann"',
            "query_context": {"bucket": "dbaguy", "scope": "app"}}
    response = client.post("/couchbase/advise", json=body, headers=AUTH)
    assert response.status_code >= 400 and "Panic" in response.json()["error"]["message"]


def test_history_entries_carry_their_parsed_context(app, cb, tmp_path):  # Review Focus 5
    client, container = app
    sqlite = client.post("/profiles", json={"profile": {"engine": "sqlite", "name": "s", "path": str(tmp_path / "x.db")}},
                         headers=AUTH).json()["profile"]["id"]
    page = HistoryPage(items=[entry(cb, "travel-sample.inventory"), entry(cb, "`my.bucket`"), entry(cb, None),
                              entry(cb, "a..b"), entry(sqlite, "main", "s"), entry(uuid4(), "dbaguy.app", "gone")],
                       next_before=None)
    contexts = [e.query_context for e in container.couchbase.with_query_contexts(page).items]
    assert contexts == [QueryContext(bucket="travel-sample", scope="inventory"), QueryContext(bucket="my.bucket"),
                        None, None, None, None]


def test_get_history_fills_the_context_and_keeps_the_stored_text(app, cb):  # B15: reopened from query_context
    client, container = app
    container.history.record(HistoryRecord(
        profile_id=cb, profile_name="cb", database="`my.bucket`.app", sql="SELECT 1", started_at=datetime.now(UTC),
        elapsed_ms=1, rows=1, status="ok"))
    item = client.get("/history", headers=AUTH).json()["items"][0]
    assert item["database"] == "`my.bucket`.app" and item["query_context"] == {"bucket": "my.bucket", "scope": "app"}


@requires_couchbase
def test_quick_open_finds_collections_with_their_tree_path(app, cb):
    client, _ = app

    def search(q):
        return client.get("/metadata/search", params={"profile_id": cb, "q": q}, headers=AUTH).json()

    hit = search("airl")[0]
    assert (hit["name"], hit["kind"], hit["database"], hit["schema_name"]) == ("airline", "collection",
                                                                                "travel-sample", "inventory")
    assert [(s["kind"], s["name"]) for s in hit["path"]] == [("bucket", "travel-sample"), ("scope", "inventory"),
                                                             ("collection", "airline")]
    assert search("inventory.airl")[0]["name"] == "airline"
    assert search("travel-sample.inventory.airl")[0]["name"] == "airline"
    assert search("app.airl") == []
    assert [h["name"] for h in search("many.c00")][:3] == ["c001", "c002", "c003"]


@requires_couchbase
def test_describe_is_not_offered_for_sqlpp(app, cb):
    client, _ = app
    body = {"profile_id": cb, "text": "SELECT * FROM users", "cursor": 16}
    response = client.post("/sql/describe", json=body, headers=AUTH)
    assert response.status_code == 200 and response.json() is None


@requires_couchbase
def test_a_syntax_error_of_advise_never_puts_the_statement_in_the_info_log(app, cb):
    client, _ = app
    info: list[str] = []
    sink = logger.add(lambda m: info.append(m.record["message"]), level="INFO")
    body = {"profile_id": cb, "statement": 'SELECT * FROM orders WHERE x = "SECRET_LIT_1" "z"',
            "query_context": {"bucket": "dbaguy", "scope": "app"}}
    try:
        response = client.post("/couchbase/advise", json=body, headers=AUTH)
    finally:
        logger.remove(sink)
    assert response.status_code == 400 and "SECRET_LIT_1" in response.json()["error"]["message"]  # the server quotes it
    assert "SECRET_LIT_1" not in "".join(info)


@requires_couchbase
def test_a_server_error_of_advise_is_sent_once(app, cb, monkeypatch):  # B39: a resend is for a lost connection
    client, _ = app
    sent = []
    begin = CouchbaseConnection._begin

    def counting(self, sql, **kwargs):
        if sql.startswith("ADVISE"):
            sent.append(sql)
        return begin(self, sql, **kwargs)

    monkeypatch.setattr(CouchbaseConnection, "_begin", counting)
    body = {"profile_id": cb, "statement": 'SELECT * FROM users WHERE name = "Ann"',
            "query_context": {"bucket": "dbaguy", "scope": "app"}}
    assert client.post("/couchbase/advise", json=body, headers=AUTH).status_code >= 400
    assert len(sent) == 1


# ---- the picker and ⌘P over a snapshot that needs no cluster ----

KEYSPACES = [  # (bucket, scope, collection)
    ("dbaguy", "_default", "_default"), ("dbaguy", "app", "orders"), ("dbaguy", "app", "reorders"),
    ("dbaguy", "old", "orders_archive"), ("dbaguy", "zz", "events"), ("dbaguy", "_system", "_query"),
    ("aaa", "_default", "_default"), ("my.bucket", "app", "orders"), ("my.bucket", "_default", "_default"),
]


@pytest.fixture
def canned(tmp_path):
    """A CouchbaseService and SchemaCache over a fixed snapshot: the metadata connection is replaced by it."""
    snapshot = SchemaSnapshot(
        database=None, default_schema=None, search_path=[], schemas=[], built_at=datetime.now(UTC),
        keyspaces=[SnapshotKeyspace(bucket=b, scope=s, collection=c, system=s == "_system") for b, s, c in KEYSPACES])

    class Metadata:
        def with_connection(self, profile_id, database, fn):
            return snapshot

    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    profile_id = profiles.create(ProfileUpsertRequest.model_validate({"profile": COUCHBASE})).profile.id
    registry = AdapterRegistry([CouchbaseAdapter()])
    cache = SchemaCache(Metadata(), registry, profiles)
    return CouchbaseService(registry, profiles, cache, Metadata()), cache, profile_id


def test_contexts_are_ordered_by_bucket_then_default_then_scopes_then_system(canned):
    service, _, profile_id = canned
    pairs = [(o.bucket, o.scope) for o in service.contexts(profile_id, None)]
    assert pairs == [("aaa", None), ("aaa", "_default"),
                     ("dbaguy", None), ("dbaguy", "_default"), ("dbaguy", "app"), ("dbaguy", "old"),
                     ("dbaguy", "zz"), ("dbaguy", "_system"),
                     ("my.bucket", None), ("my.bucket", "_default"), ("my.bucket", "app")]
    unlisted = service.contexts(profile_id, QueryContext(bucket="dbaguy", scope="gone"))
    assert (unlisted[0].bucket, unlisted[0].scope, unlisted[0].listed) == ("dbaguy", "gone", False)
    assert len(unlisted) == len(pairs) + 1 and all(o.listed for o in unlisted[1:])


def test_quick_open_ranks_collections_like_relations_and_narrows_by_scope_or_bucket(canned):
    _, cache, profile_id = canned

    def found(q, **kwargs):
        return [(h.database, h.schema_name, h.name) for h in cache.search(profile_id, q, **kwargs)]

    # exact, then prefix, then substring (shorter first); the same name in two buckets by bucket
    assert found("orders") == [("dbaguy", "app", "orders"), ("my.bucket", "app", "orders"),
                               ("dbaguy", "old", "orders_archive"), ("dbaguy", "app", "reorders")]
    assert found("orders", limit=2) == [("dbaguy", "app", "orders"), ("my.bucket", "app", "orders")]
    assert found("APP.ORD") == [("dbaguy", "app", "orders"), ("my.bucket", "app", "orders"),
                                ("dbaguy", "app", "reorders")]  # case-insensitive qualifier and name
    assert found("dbaguy.app.ord") == [("dbaguy", "app", "orders"), ("dbaguy", "app", "reorders")]
    assert found("my.bucket.ord") == [("my.bucket", "app", "orders")]  # a bucket alone (with its dot) narrows too
    assert found("my.bucket.app.ord") == [("my.bucket", "app", "orders")]
    assert found("old.") == [("dbaguy", "old", "orders_archive")]  # a qualifier and no name: the whole scope
    assert found("zzz.events") == [] and found(".") == []
    # a system keyspace comes after the others of the same rank, whatever its length
    assert found("quer")[0] == ("dbaguy", "_system", "_query")
    assert found("e", limit=50)[-1] == ("dbaguy", "_system", "_query")


def test_a_collection_hit_opens_at_its_tree_path(canned):
    _, cache, profile_id = canned
    hit = cache.search(profile_id, "my.bucket.app.orders")[0]
    assert hit.kind == "collection" and (hit.database, hit.schema_name) == ("my.bucket", "app")
    assert [(s.kind, s.name) for s in hit.path] == [("bucket", "my.bucket"), ("scope", "app"), ("collection", "orders")]


@requires_couchbase
def test_completion_uses_the_sample_and_the_tab_context(app, cb):
    client, _ = app
    keyspace = {"bucket": "dbaguy", "scope": "app", "collection": "users"}
    client.post("/couchbase/fields/sample", json={"profile_id": cb, "keyspace": keyspace}, headers=AUTH)
    body = {"profile_id": cb, "text": "SELECT u. FROM users AS u", "cursor": 9,
            "query_context": {"bucket": "dbaguy", "scope": "app"}, "seen_fields": ["late"]}
    labels = [i["label"] for i in client.post("/sql/complete", json=body, headers=AUTH).json()["items"]]
    assert "name" in labels and "u64max" in labels


@requires_couchbase
def test_completion_never_starts_a_sample(app, cb, monkeypatch):  # R-04-11: a sample is a user action
    client, container = app
    monkeypatch.setattr(container.field_samples, "sample", lambda *args, **kwargs: pytest.fail("completion sampled"))
    body = {"profile_id": cb, "text": "SELECT u. FROM users AS u", "cursor": 9,
            "query_context": {"bucket": "dbaguy", "scope": "app"}}
    assert client.post("/sql/complete", json=body, headers=AUTH).json()["items"] == []  # nothing sampled yet
    body = {**body, "text": "SELECT la FROM users AS u", "cursor": 9, "seen_fields": ["late"]}
    items = client.post("/sql/complete", json=body, headers=AUTH).json()["items"]
    assert [(i["label"], i["detail"]) for i in items][:1] == [("late", "seen in results")]  # what the results showed
