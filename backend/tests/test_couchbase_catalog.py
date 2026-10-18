import json
import time

import pytest

from dbaguy_backend.adapters.couchbase.catalog import (
    build_snapshot,
    index_ddl,
    index_detail,
    list_children,
    object_detail,
)
from dbaguy_backend.domain.errors import NotFound
from dbaguy_backend.domain.metadata import PathSegment
from tests.support import COUCHBASE_PASSWORD, COUCHBASE_READER, couchbase_query, requires_couchbase


def path(*pairs):
    return [PathSegment(kind=kind, name=name) for kind, name in pairs]


BUCKET = ("bucket", "b")
SCOPE = ("scope", "s")
COLLECTION = ("collection", "c")


@pytest.mark.parametrize("index, ddl", [
    ({"name": "#primary", "is_primary": True, "index_key": [], "state": "online", "metadata": {"num_replica": 0}},
     "CREATE PRIMARY INDEX `#primary` ON `b`.`s`.`c`;"),
    ({"name": "idx_amount", "index_key": ["`amount` DESC"], "state": "online"},
     "CREATE INDEX `idx_amount` ON `b`.`s`.`c`(`amount` DESC);"),
    ({"name": "adv", "index_key": ["(distinct (array `t` for `t` in `tags` end))", "`name`"], "condition": "(`type` = \"x\")",
      "partition": "HASH(`id`)", "state": "deferred", "metadata": {"num_replica": 1}},
     "CREATE INDEX `adv` ON `b`.`s`.`c`((distinct (array `t` for `t` in `tags` end)), `name`) PARTITION BY HASH(`id`) "
     "WHERE (`type` = \"x\") WITH {\"defer_build\": true, \"num_replica\": 1};"),
    ({"name": "odd`name", "index_key": ["`a`"], "state": "online"}, "CREATE INDEX `odd``name` ON `b`.`s`.`c`(`a`);"),
    # WITH carries only what differs from the default: replicas above 0, a deferred build (not a building or online one)
    ({"name": "r", "index_key": ["`a`"], "state": "online", "metadata": {"num_replica": 2}},
     "CREATE INDEX `r` ON `b`.`s`.`c`(`a`) WITH {\"num_replica\": 2};"),
    ({"name": "building", "index_key": ["`a`"], "state": "building", "metadata": {"num_replica": 0}},
     "CREATE INDEX `building` ON `b`.`s`.`c`(`a`);"),
    ({"name": "bad", "index_key": ["`a`"], "state": "online", "metadata": {"num_replica": True}},  # a bool is no count
     "CREATE INDEX `bad` ON `b`.`s`.`c`(`a`);"),
    ({"name": "bare", "state": "online"}, "CREATE INDEX `bare` ON `b`.`s`.`c`();"),  # a row without keys still reads
])
def test_index_ddl(index, ddl):
    assert index_ddl(index, "b", "s", "c") == ddl


def test_index_ddl_quotes_the_keyspace_parts_it_is_given():
    assert index_ddl({"name": "i", "index_key": ["`a`"]}, "travel-sample", "_default", "_default") == (
        "CREATE INDEX `i` ON `travel-sample`.`_default`.`_default`(`a`);"
    )
    assert index_ddl({"name": "i", "index_key": ["`a`"]}, "b", "s", "we`ird\\") == (
        "CREATE INDEX `i` ON `b`.`s`.`we``ird\\\\`(`a`);"
    )


def test_index_detail():
    assert index_detail({"is_primary": True, "state": "online"}) == "primary · online"
    assert index_detail({"state": "deferred"}) == "deferred"
    assert index_detail({}) == "unknown"
    assert index_detail({"is_primary": True, "state": None}) == "primary · unknown"


class Recording:
    """A Query that answers from a table and records what it was asked."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def __call__(self, statement, named):
        self.asked.append((statement, dict(named)))
        return next(rows for words, rows in self.answers if words in statement)


def test_names_are_parameters_never_spliced():
    query = Recording([("system:scopes", ["app"]), ("system:keyspaces", [])])
    list_children(query, path(("bucket", "x\"; DROP")))
    statement, named = query.asked[0]
    assert "DROP" not in statement and named == {"b": json.dumps('x"; DROP')}


def test_a_path_that_names_nothing_is_not_found():
    with pytest.raises(NotFound):
        object_detail(Recording([("system:indexes", [])]), path(("bucket", "b"), ("scope", "s"), ("collection", "c"), ("index", "nope")))


EVIL = 'e`vil" OR 1=1 --'
EVERY_PATH = [
    [],
    [("bucket", EVIL)],
    [("bucket", EVIL), ("scope", EVIL)],
    [("bucket", EVIL), ("scope", "_default")],
    [("bucket", EVIL), ("scope", EVIL), ("collection", EVIL)],
    [("bucket", EVIL), ("scope", "_default"), ("collection", "_default")],
]


def anything(rows_for_index):
    """Answers every catalog statement with a row, so no existence check ends the call early."""
    return Recording([("system:indexes", rows_for_index), ("system:scopes", ["s"]), ("system:keyspaces", ["c"]),
                      ("system:buckets", ["b"])])


@pytest.mark.parametrize("pairs", EVERY_PATH)
def test_no_name_reaches_a_statement_and_every_listing_is_ordered(pairs):
    query = anything([])
    list_children(query, path(*pairs))
    for statement, named in query.asked:
        assert "e`vil" not in statement and "OR 1=1" not in statement
        assert set(named) <= {"b", "s", "c"}
        assert all(value == json.dumps(EVIL) or value == json.dumps("_default") for value in named.values())
        assert "ORDER BY" in statement or "LIMIT 1" in statement  # a listing is ordered; an existence check reads one row
        assert "readonly" not in statement.lower() and statement.lstrip().upper().startswith("SELECT")


@pytest.mark.parametrize("pairs", [p for p in EVERY_PATH if p])
def test_details_ask_the_same_way(pairs):
    query = anything([])
    object_detail(query, path(*pairs))
    for statement, named in query.asked:
        assert "e`vil" not in statement and statement.lstrip().upper().startswith("SELECT")
        assert set(named) <= {"b", "s", "c"}


def test_a_path_of_the_wrong_shape_is_not_found():
    for pairs in ([("scope", "s")], [("bucket", "b"), ("collection", "c")], [("bucket", "b"), ("scope", "s"), ("index", "i")],
                  [("bucket", "b"), ("scope", "s"), ("collection", "c"), ("index", "i"), ("index", "j")]):
        with pytest.raises(NotFound):
            list_children(anything([]), path(*pairs))
    for pairs in ([], [("scope", "s")], [("bucket", "b"), ("scope", "s"), ("collection", "c"), ("index", "i"), ("index", "j")]):
        with pytest.raises(NotFound):
            object_detail(anything([]), path(*pairs))
    with pytest.raises(NotFound):  # an index has no children
        list_children(anything([]), path(BUCKET, SCOPE, COLLECTION, ("index", "i")))


def test_scopes_put_default_first_and_system_last():
    query = Recording([("system:scopes", ["_system", "app", "many"])])
    scopes = list_children(query, path(BUCKET))
    assert [s.name for s in scopes] == ["_default", "app", "many", "_system"]
    assert [s.system for s in scopes] == [False, False, False, True]
    assert all(s.kind == "scope" and s.has_children for s in scopes)
    assert scopes[1].path == path(BUCKET, ("scope", "app"))
    assert len(query.asked) == 1  # no existence check when the bucket showed scopes


def test_a_listed_default_scope_is_not_doubled():
    scopes = list_children(Recording([("system:scopes", ["_default", "app"])]), path(BUCKET))
    assert [s.name for s in scopes] == ["_default", "app"]


def test_a_bucket_with_only_the_default_scope_lists_it():
    query = Recording([("system:scopes", []), ("system:buckets", ["b"])])
    assert [s.name for s in list_children(query, path(BUCKET))] == ["_default"]


def test_a_bucket_that_is_gone_is_not_found():
    with pytest.raises(NotFound):
        list_children(Recording([("system:scopes", []), ("system:buckets", [])]), path(BUCKET))


def test_buckets_are_the_roots():
    nodes = list_children(Recording([("system:buckets", ["a", "travel-sample"])]), [])
    assert [(n.kind, n.name, n.has_children, n.system) for n in nodes] == [
        ("bucket", "a", True, False), ("bucket", "travel-sample", True, False)]
    assert nodes[1].path == path(("bucket", "travel-sample"))


def test_the_default_scope_starts_with_the_bucket_keyspace_row():
    query = Recording([("`bucket` IS MISSING", ["b"]), ("k.`scope` = $s", ["_default", "extra", "zeta"])])
    nodes = list_children(query, path(BUCKET, ("scope", "_default")))
    assert [n.name for n in nodes] == ["_default", "extra", "zeta"]  # the default collection once, first
    assert all(n.kind == "collection" and n.has_children for n in nodes)
    assert nodes[1].path == path(BUCKET, ("scope", "_default"), ("collection", "extra"))
    assert query.asked[0][1] == {"b": json.dumps("b")}  # the bucket-level row has no scope to filter on


def test_a_scope_without_the_default_collection_row():  # the default collection can be dropped
    query = Recording([("`bucket` IS MISSING", []), ("k.`scope` = $s", ["x"])])
    assert [n.name for n in list_children(query, path(BUCKET, ("scope", "_default")))] == ["x"]


def test_collections_of_the_system_scope_are_system():
    query = Recording([("k.`scope` = $s", ["_mobile", "_query"])])
    nodes = list_children(query, path(BUCKET, ("scope", "_system")))
    assert [(n.name, n.system) for n in nodes] == [("_mobile", True), ("_query", True)]
    assert not any(n.system for n in list_children(Recording([("k.`scope` = $s", ["users"])]), path(BUCKET, SCOPE)))


def test_an_empty_scope_is_listed_empty_and_a_gone_scope_is_not_found():
    assert list_children(Recording([("k.`scope` = $s", []), ("system:scopes", ["s"])]), path(BUCKET, SCOPE)) == []
    with pytest.raises(NotFound):
        list_children(Recording([("k.`scope` = $s", []), ("system:scopes", [])]), path(BUCKET, SCOPE))
    with pytest.raises(NotFound):  # the default scope exists as long as the bucket does
        list_children(Recording([("`bucket` IS MISSING", []), ("k.`scope` = $s", []), ("system:buckets", [])]),
                      path(BUCKET, ("scope", "_default")))


def test_indexes_come_with_state_and_primary():
    rows = [{"name": "#primary", "is_primary": True, "state": "online"}, {"name": "i1", "state": "building"}]
    query = Recording([("system:indexes", rows)])
    nodes = list_children(query, path(BUCKET, SCOPE, COLLECTION))
    assert [(n.kind, n.name, n.detail, n.has_children) for n in nodes] == [
        ("index", "#primary", "primary · online", False), ("index", "i1", "building", False)]
    assert nodes[0].path == path(BUCKET, SCOPE, COLLECTION, ("index", "#primary"))
    assert query.asked[0][1] == {"b": json.dumps("b"), "s": json.dumps("s"), "c": json.dumps("c")}


def test_the_default_collection_finds_its_indexes_by_the_bucket_name():
    query = Recording([("system:indexes", [{"name": "def_primary", "is_primary": True, "state": "online"}])])
    list_children(query, path(BUCKET, ("scope", "_default"), ("collection", "_default")))
    statement, named = query.asked[0]
    assert named == {"b": json.dumps("b")} and "bucket_id IS MISSING" in statement and "keyspace_id = $b" in statement


def test_a_collection_without_indexes_is_empty_and_a_gone_one_is_not_found():
    assert list_children(Recording([("system:indexes", []), ("system:keyspaces", ["c"])]),
                         path(BUCKET, SCOPE, COLLECTION)) == []
    with pytest.raises(NotFound):
        list_children(Recording([("system:indexes", []), ("system:keyspaces", [])]), path(BUCKET, SCOPE, COLLECTION))
    # the default collection is checked by the bucket-level row
    gone = Recording([("system:indexes", []), ("`bucket` IS MISSING", [])])
    with pytest.raises(NotFound):
        list_children(gone, path(BUCKET, ("scope", "_default"), ("collection", "_default")))


def test_bucket_detail_has_no_ddl():
    detail = object_detail(Recording([("system:buckets", ["b"])]), path(BUCKET))
    assert (detail.kind, detail.name, detail.path) == ("bucket", "b", path(BUCKET))
    assert detail.ddl is None and detail.ddl_is_generated and detail.columns is None
    assert "cluster management" in detail.comment
    with pytest.raises(NotFound):
        object_detail(Recording([("system:buckets", [])]), path(BUCKET))


def test_scope_detail():
    detail = object_detail(Recording([("system:scopes", ["s"])]), path(BUCKET, SCOPE))
    assert detail.ddl == "CREATE SCOPE `b`.`s`;" and detail.ddl_is_generated
    detail = object_detail(Recording([("system:buckets", ["b"])]), path(BUCKET, ("scope", "_default")))
    assert detail.ddl.startswith("--") and "CREATE" not in detail.ddl and detail.ddl_is_generated
    with pytest.raises(NotFound):
        object_detail(Recording([("system:scopes", [])]), path(BUCKET, SCOPE))


def test_system_scope_and_its_collections_have_no_create():
    detail = object_detail(Recording([("system:scopes", ["_system"])]), path(BUCKET, ("scope", "_system")))
    assert detail.ddl.startswith("--") and "CREATE" not in detail.ddl and "system scope" in detail.ddl
    query = Recording([("system:indexes", [{"name": "i", "index_key": ["`a`"], "state": "online"}])])
    detail = object_detail(query, path(BUCKET, ("scope", "_system"), ("collection", "_query")))
    head, _, rest = detail.ddl.partition("\n\n")
    assert head.startswith("--") and "CREATE COLLECTION" not in detail.ddl
    assert rest == "CREATE INDEX `i` ON `b`.`_system`.`_query`(`a`);"


def test_scope_detail_quotes_its_name():
    detail = object_detail(Recording([("system:scopes", ["a`b"])]), path(("bucket", "travel-sample"), ("scope", "a`b")))
    assert detail.ddl == "CREATE SCOPE `travel-sample`.`a``b`;"


def test_collection_detail_lists_every_index_after_the_create():
    rows = [{"name": "#primary", "is_primary": True, "state": "online"},
            {"name": "i1", "index_key": ["`a`"], "state": "online"}]
    detail = object_detail(Recording([("system:indexes", rows)]), path(BUCKET, SCOPE, COLLECTION))
    assert detail.ddl == ("CREATE COLLECTION `b`.`s`.`c`;\n\nCREATE PRIMARY INDEX `#primary` ON `b`.`s`.`c`;\n\n"
                          "CREATE INDEX `i1` ON `b`.`s`.`c`(`a`);")
    assert detail.ddl_is_generated and detail.kind == "collection" and detail.name == "c"


def test_collection_detail_without_indexes_is_the_create_alone():
    detail = object_detail(Recording([("system:indexes", []), ("system:keyspaces", ["c"])]), path(BUCKET, SCOPE, COLLECTION))
    assert detail.ddl == "CREATE COLLECTION `b`.`s`.`c`;"
    with pytest.raises(NotFound):
        object_detail(Recording([("system:indexes", []), ("system:keyspaces", [])]), path(BUCKET, SCOPE, COLLECTION))


def test_default_collection_detail_is_a_comment_then_its_indexes():
    rows = [{"name": "def_primary", "is_primary": True, "state": "online"}]
    detail = object_detail(Recording([("system:indexes", rows)]), path(BUCKET, ("scope", "_default"), ("collection", "_default")))
    head, _, rest = detail.ddl.partition("\n\n")
    assert head.startswith("--") and "CREATE COLLECTION" not in detail.ddl
    assert rest == "CREATE PRIMARY INDEX `def_primary` ON `b`.`_default`.`_default`;"


def test_index_detail_ddl_and_comment():
    rows = [{"name": "other", "state": "online"},
            {"name": "i1", "index_key": ["`a`"], "state": "online", "using": "gsi",
             "metadata": {"last_scan_time": "2026-10-18T14:58:44.123Z", "num_replica": 0}}]
    detail = object_detail(Recording([("system:indexes", rows)]), path(BUCKET, SCOPE, COLLECTION, ("index", "i1")))
    assert detail.ddl == "CREATE INDEX `i1` ON `b`.`s`.`c`(`a`);" and detail.ddl_is_generated
    assert detail.comment == "online · last scanned 2026-10-18T14:58:44.123Z · using gsi"
    assert (detail.kind, detail.name) == ("index", "i1")


def test_index_comment_leaves_out_what_the_server_did_not_say():
    for metadata in ({}, {"last_scan_time": None}, None):
        row = {"name": "i1", "index_key": ["`a`"], "state": "deferred", "metadata": metadata}
        detail = object_detail(Recording([("system:indexes", [row])]), path(BUCKET, SCOPE, COLLECTION, ("index", "i1")))
        assert detail.comment == "deferred"


def test_the_snapshot_renames_the_bucket_row_and_marks_system_scopes():
    rows = [{"n": "b"}, {"b": "b", "s": "_system", "n": "_query"}, {"b": "b", "s": "app", "n": "users"},
            {"b": "a", "s": "_default", "n": "x"}, {"n": "a"}]
    query = Recording([("system:keyspaces", rows)])
    snapshot = build_snapshot(query)
    assert [(k.bucket, k.scope, k.collection, k.system) for k in snapshot.keyspaces] == [
        ("a", "_default", "_default", False), ("a", "_default", "x", False),
        ("b", "_default", "_default", False), ("b", "_system", "_query", True), ("b", "app", "users", False)]
    assert snapshot.database is None and snapshot.default_schema is None and snapshot.search_path == []
    assert snapshot.schemas == [] and snapshot.built_at is not None
    assert len(query.asked) == 1 and query.asked[0][1] == {}  # one query (ruling P7)


def test_an_empty_snapshot():
    assert build_snapshot(Recording([("system:keyspaces", [])])).keyspaces == []


def dev_query(user="dbaguy"):
    def query(statement, named):
        response = couchbase_query(statement, {f"${k}": json.loads(v) for k, v in named.items()},
                                   user=user, password=COUCHBASE_PASSWORD)
        assert not response.get("errors"), response.get("errors")
        return response["results"]
    return query


@requires_couchbase
def test_the_dev_cluster_tree():
    query = dev_query()
    assert [n.name for n in list_children(query, [])] == ["dbaguy", "travel-sample"]
    scopes = list_children(query, path(("bucket", "dbaguy")))
    names = [s.name for s in scopes]
    assert names[0] == "_default" and {"app", "many"} <= set(names)
    assert "_system" not in names[:-1] and all(s.system == (s.name == "_system") for s in scopes)  # last, when listed
    started = time.monotonic()
    many = list_children(query, path(("bucket", "dbaguy"), ("scope", "many")))
    assert len(many) == 300 and many[0].name == "c001" and time.monotonic() - started < 1.0  # spec: < 0.5 s locally
    assert [c.name for c in list_children(query, path(("bucket", "dbaguy"), ("scope", "_default")))] == ["_default"]
    users = path(("bucket", "dbaguy"), ("scope", "app"), ("collection", "users"))
    indexes = {n.name: n.detail for n in list_children(query, users)}
    assert indexes == {"#primary": "primary · online", "idx_users_deferred": "deferred"}
    detail = object_detail(query, [*users, PathSegment(kind="index", name="idx_users_deferred")])
    assert detail.ddl == "CREATE INDEX `idx_users_deferred` ON `dbaguy`.`app`.`users`(`name`) WITH {\"defer_build\": true};"
    assert detail.ddl_is_generated and "deferred" in detail.comment
    collection = object_detail(query, users)
    assert collection.ddl.startswith("CREATE COLLECTION `dbaguy`.`app`.`users`;\n\nCREATE PRIMARY INDEX `#primary`")


@requires_couchbase
def test_the_dev_cluster_objects_that_are_not_plain():
    query = dev_query()
    app = path(("bucket", "dbaguy"), ("scope", "app"))
    collections = [n.name for n in list_children(query, app)]
    assert {"users", "odd-name", "value", "noindex"} <= set(collections) and collections == sorted(collections)
    assert list_children(query, [*app, PathSegment(kind="collection", name="noindex")]) == []
    assert object_detail(query, [*app, PathSegment(kind="collection", name="odd-name")]).ddl == (
        "CREATE COLLECTION `dbaguy`.`app`.`odd-name`;")
    assert object_detail(query, [*app, PathSegment(kind="collection", name="value")]).ddl == (
        "CREATE COLLECTION `dbaguy`.`app`.`value`;")
    assert object_detail(query, app).ddl == "CREATE SCOPE `dbaguy`.`app`;"
    assert object_detail(query, app[:1]).ddl is None
    # the default collection of a bucket, its indexes by the bucket name
    default = path(("bucket", "travel-sample"), ("scope", "_default"), ("collection", "_default"))
    indexes = list_children(query, default)
    assert "def_primary" in {n.name for n in indexes}
    assert next(n for n in indexes if n.name == "def_primary").detail == "primary · online"
    ddl = object_detail(query, default).ddl
    assert ddl.startswith("--") and "CREATE PRIMARY INDEX `def_primary` ON `travel-sample`.`_default`.`_default`;" in ddl
    # a condition and a partition as the server wrote them
    airport = object_detail(query, path(("bucket", "travel-sample"), ("scope", "inventory"), ("collection", "route"),
                                        ("index", "def_inventory_route_route_src_dst_day")))
    assert airport.ddl.startswith("CREATE INDEX `def_inventory_route_route_src_dst_day` ON `travel-sample`.`inventory`.`route`(")


@requires_couchbase
def test_the_dev_cluster_says_not_found_for_what_is_gone():
    query = dev_query()
    for gone in (path(("bucket", "nope")), path(("bucket", "dbaguy"), ("scope", "nope")),
                 path(("bucket", "dbaguy"), ("scope", "app"), ("collection", "nope")),
                 path(("bucket", "dbaguy"), ("scope", "app"), ("collection", "users"), ("index", "nope"))):
        with pytest.raises(NotFound):
            object_detail(query, gone)
        if gone[-1].kind != "index":
            with pytest.raises(NotFound):
                list_children(query, gone)
    with pytest.raises(NotFound):
        list_children(query, path(("bucket", "dbaguy"), ("scope", "app"), ("collection", "nope")))


@requires_couchbase
def test_a_limited_user_sees_only_its_keyspace():  # A18 for indexes
    query = dev_query(COUCHBASE_READER)
    assert [n.name for n in list_children(query, [])] == ["dbaguy"]
    assert [n.name for n in list_children(query, path(("bucket", "dbaguy"), ("scope", "app")))] == ["users"]
    users = path(("bucket", "dbaguy"), ("scope", "app"), ("collection", "users"))
    # A18: system:indexes is filtered by privileges, and query_select + data_reader is not the privilege to list indexes
    assert {n.name for n in list_children(query, users)} == set()
    assert object_detail(query, users).ddl == "CREATE COLLECTION `dbaguy`.`app`.`users`;"
    snapshot = build_snapshot(query)
    assert [(k.bucket, k.scope, k.collection) for k in snapshot.keyspaces] == [("dbaguy", "app", "users")]


@requires_couchbase
def test_the_snapshot_holds_every_keyspace():
    snapshot = build_snapshot(dev_query())
    keyspaces = {(k.bucket, k.scope, k.collection) for k in snapshot.keyspaces}
    assert ("dbaguy", "_default", "_default") in keyspaces and ("travel-sample", "inventory", "airline") in keyspaces
    assert len([k for k in keyspaces if k[:2] == ("dbaguy", "many")]) == 300
    assert snapshot.schemas == [] and snapshot.database is None
    assert all(k.system == (k.scope == "_system") for k in snapshot.keyspaces)
