"""SQL++ completion (spec §3.12, SQL++ Should 1): keyspace paths from the snapshot, field paths from samples and from
results seen in the tab, back-ticks for special names."""

import random
import time
from datetime import UTC, datetime

import pytest
from loguru import logger

from dbaguy_backend.domain.couchbase import FieldPath, Keyspace
from dbaguy_backend.domain.execution import QueryContext
from dbaguy_backend.domain.schema import SchemaSnapshot, SnapshotKeyspace
from dbaguy_backend.sql.completion import CompletionResult, SnapshotIndex
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.keyspaces import NO_KEYSPACES, KeyspaceTree
from dbaguy_backend.sql.sqlpp_completion import _complete, _Known, complete_sqlpp

KEYSPACES = [SnapshotKeyspace(bucket=b, scope=s, collection=c) for b, s, c in [
    ("dbaguy", "_default", "_default"), ("dbaguy", "app", "events"), ("dbaguy", "app", "odd-name"),
    ("dbaguy", "app", "orders"), ("dbaguy", "app", "users"), ("dbaguy", "app", "value"),
    ("travel-sample", "_default", "_default"), ("travel-sample", "inventory", "airline"),
    ("travel-sample", "inventory", "airport"), ("travel-sample", "inventory", "route"),
]]
INDEX = SnapshotIndex(SchemaSnapshot(database=None, default_schema=None, search_path=[], schemas=[],
                                     built_at=datetime.now(UTC), keyspaces=KEYSPACES), Dialect.SQLPP)
USERS = Keyspace(bucket="dbaguy", scope="app", collection="users")
CTX = QueryContext(bucket="dbaguy", scope="app")


def path(*segments, kind="string", docs=8):
    return FieldPath(path=list(segments), types={kind: docs}, docs=docs, percent=docs * 10.0)


FIELDS = {USERS: [
    path(kind="object", docs=10), path("name"), path("age", kind="number", docs=6),
    path("address", kind="object", docs=5), path("address", "city", docs=5), path("address", "zip", docs=3),
    path("odd key", docs=1), path("select", kind="number", docs=1), path("tags", kind="array", docs=4),
    path("tags", None, docs=4),
]}


def run(text, context=CTX, seen=()):
    cursor = text.index("|")
    return complete_sqlpp(text.replace("|", ""), cursor, INDEX, context, FIELDS.get, list(seen))


def items(result):
    return {i.label: i for i in result.items}


def test_from_offers_the_context_scopes_collections_and_every_bucket():
    found = items(run("SELECT * FROM |"))
    assert {k: found[k].kind for k in ("users", "odd-name", "value", "dbaguy", "travel-sample")} == {
        "users": "collection", "odd-name": "collection", "value": "collection", "dbaguy": "bucket",
        "travel-sample": "bucket"}
    assert (found["odd-name"].apply, found["value"].apply, found["travel-sample"].apply) == (
        "`odd-name`", "`value`", "`travel-sample`")
    assert "airline" not in found  # another scope's collection


def test_a_bucket_only_context_offers_its_default_scope_and_no_context_only_buckets():
    assert items(run("SELECT * FROM |", context=QueryContext(bucket="travel-sample")))["_default"].kind == "collection"
    assert {i.kind for i in run("SELECT * FROM |", context=None).items} == {"bucket"}


def test_after_a_bucket_the_scopes_and_after_a_scope_the_collections():
    assert [(i.label, i.kind) for i in run("SELECT * FROM `travel-sample`.|").items] == [
        ("_default", "scope"), ("inventory", "scope")]
    assert [i.label for i in run("SELECT * FROM `travel-sample`.inventory.ai|").items] == ["airline", "airport"]


@pytest.mark.parametrize("text", [
    "SELECT * FROM a JOIN |", "SELECT * FROM a AS x NEST |", "UPDATE |", "INSERT INTO |", "UPSERT INTO |",
    "DELETE FROM |", "MERGE INTO |", "MERGE INTO a USING |", "SELECT * FROM a, |",
])
def test_every_keyspace_position(text):
    assert "users" in items(run(text))


def test_alias_dot_offers_sampled_fields_with_back_ticks():
    found = items(run("SELECT u.| FROM users AS u"))
    assert set(found) == {"name", "age", "address", "odd key", "select", "tags"}
    assert (found["odd key"].apply, found["select"].apply, found["name"].apply) == ("`odd key`", "`select`", None)
    assert found["name"].kind == "field" and found["name"].detail == "users · string · 80%"


def test_nested_paths_and_implicit_aliases():
    assert set(items(run("SELECT u.address.| FROM users u"))) == {"city", "zip"}
    assert "name" in items(run("SELECT users.| FROM users"))  # an unaliased keyspace qualifies by its name
    assert "name" in items(run("SELECT d.| FROM dbaguy.app.users AS d", context=None))  # a full path needs no context
    assert items(run("SELECT d.| FROM app.users AS d")) == {}  # two parts name no keyspace


BUCKET = QueryContext(bucket="dbaguy")  # no scope


def test_under_a_bucket_only_context_a_two_part_path_is_scope_dot_collection():  # ruling B47, as the server reads it
    assert [(i.label, i.kind) for i in run("SELECT * FROM app.|", context=BUCKET).items] == [
        ("users", "collection"), ("value", "collection"), ("events", "collection"), ("orders", "collection"),
        ("odd-name", "collection")]
    assert [i.label for i in run("SELECT * FROM app.us|", context=BUCKET).items] == ["users"]
    assert "users" in items(run("SELECT * FROM a JOIN app.|", context=BUCKET))
    # the first part may still be a bucket: its scopes are offered as before
    assert [(i.label, i.kind) for i in run("SELECT * FROM dbaguy.|", context=BUCKET).items] == [
        ("app", "scope"), ("_default", "scope")]
    assert items(run("SELECT * FROM `travel-sample`.|", context=BUCKET))["inventory"].kind == "scope"


def test_under_a_bucket_only_context_a_scope_dot_collection_alias_has_the_sampled_fields():
    found = items(run("SELECT u.| FROM app.users AS u", context=BUCKET))
    assert set(found) == set(items(run("SELECT u.| FROM users AS u")))
    assert found["name"].detail == "users · string · 80%"
    assert "name" in items(run("SELECT users.| FROM app.users", context=BUCKET))  # by its last part
    assert "name" in items(run("SELECT | FROM app.users AS u", context=BUCKET))  # the only keyspace, unqualified
    assert items(run("SELECT u.| FROM app.orders AS u", context=BUCKET)) == {}  # not sampled
    assert items(run("SELECT u.| FROM users AS u", context=BUCKET)) == {}  # one part: `dbaguy._default.users`
    assert items(run("SELECT u.| FROM x.y.z AS u", context=BUCKET)) == {}


def test_under_a_scoped_context_or_none_a_two_part_path_names_nothing():  # the server: ambiguous / no bucket
    assert run("SELECT * FROM app.|").items == [] and run("SELECT * FROM app.|", context=None).items == []
    assert items(run("SELECT d.| FROM app.users AS d")) == {}
    assert items(run("SELECT d.| FROM app.users AS d", context=None)) == {}


def test_one_keyspace_offers_its_fields_unqualified_then_the_fields_seen():
    result = run("SELECT na| FROM users AS u", seen=["name", "nation"])
    labels = [i.label for i in result.items]
    assert labels.index("name") < labels.index("nation") and labels.count("name") == 1
    assert items(result)["nation"].detail == "seen in results"


def test_aliases_in_column_positions_and_no_guessing_with_two_keyspaces():
    found = items(run("SELECT | FROM users AS u JOIN orders AS o ON KEYS u.oid"))
    assert found["u"].kind == "alias" and found["o"].kind == "alias" and "name" not in found


def test_unsampled_keyspaces_offer_no_fields():
    assert items(run("SELECT o.| FROM orders AS o")) == {}


def test_strings_and_comments_get_nothing():
    assert run("SELECT 'FROM |' FROM users").items == []
    assert run("SELECT * FROM users -- FROM |").items == []


def test_keywords_at_the_start():
    assert "ADVISE" in items(run("|"))


# --- what the rules above leave out ---------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "SELECT a, |", 'SELECT * FROM users USE KEYS ["a", |', "SELECT * FROM users AS u ORDER BY u.age, |",
    "INSERT INTO users (KEY, VALUE) VALUES ('k', {'a': 1}), |",
    "MERGE INTO a USING b ON KEYS 1 WHEN MATCHED THEN UPDATE |",
])
def test_these_are_not_keyspace_positions(text):
    assert {"bucket", "collection", "scope"}.isdisjoint(i.kind for i in run(text).items)


def test_a_comma_after_a_join_condition_still_continues_the_from_list():
    assert "orders" in items(run("SELECT * FROM users AS u JOIN orders AS o ON KEYS u.oid, |"))


def test_fields_below_an_array_element_are_not_completed_and_nothing_follows_a_dot_in_an_expression():
    assert run("SELECT u.tags[0].| FROM users AS u").items == []
    assert run("SELECT u.tags.| FROM users AS u").items == []  # `tags` has no key below it; `tags[]` is not a key
    assert run("SELECT (1).| FROM users AS u").items == []


def test_a_back_ticked_alias_and_the_first_reference_of_a_name_win():
    assert "name" in items(run("SELECT `my alias`.| FROM users AS `my alias`"))
    assert run("SELECT x.| FROM users AS u").items == []
    assert "name" in items(run("SELECT u.| FROM users AS u JOIN orders AS u2 ON KEYS u.oid"))


def test_a_subquery_or_a_cte_is_one_more_source_so_nothing_is_guessed():
    assert "name" not in items(run("SELECT | FROM (SELECT * FROM users) AS s"))
    assert "name" not in items(run("WITH x AS (SELECT * FROM users) SELECT | FROM x"))
    assert run("SELECT s.| FROM (SELECT * FROM users) AS s").items == []


def test_the_fields_of_a_single_keyspace_in_any_clause_of_any_statement():
    for text in ("UPDATE users SET na|", "DELETE FROM users WHERE na|", "SELECT COUNT(na|) FROM users"):
        assert "name" in items(run(text)), text


def test_seen_fields_are_offered_once_and_after_the_sampled_ones():
    result = run("SELECT | FROM users AS u", seen=["late", "late", "name", "u", ""])
    labels = [i.label for i in result.items]
    assert labels.count("late") == labels.count("name") == labels.count("u") == 1 and "" not in labels
    assert items(result)["name"].detail == "users · string · 80%" and items(result)["u"].kind == "alias"
    assert items(run("SELECT | FROM orders AS o", seen=["a b"]))["a b"].apply == "`a b`"


def test_without_a_snapshot_there_are_no_keyspaces_but_fields_still_come_from_the_sample():
    cursor = len("SELECT * FROM ")
    assert complete_sqlpp("SELECT * FROM ", cursor, None, CTX, FIELDS.get, []).items == []
    assert "name" in {i.label for i in complete_sqlpp("SELECT u. FROM users u", 9, None, CTX, FIELDS.get, []).items}


def test_system_keyspaces_come_last_and_a_missing_context_part_names_nothing():
    keyspaces = [*KEYSPACES, SnapshotKeyspace(bucket="dbaguy", scope="_system", collection="_query", system=True)]
    snapshot = SchemaSnapshot(database=None, default_schema=None, search_path=[], schemas=[],
                              built_at=datetime.now(UTC), keyspaces=keyspaces)
    index = SnapshotIndex(snapshot, Dialect.SQLPP)
    scopes = complete_sqlpp("SELECT * FROM dbaguy.", 21, index, None, FIELDS.get, []).items
    assert [i.label for i in scopes] == ["app", "_default", "_system"]  # by length, the system scope last
    collections = complete_sqlpp("SELECT * FROM dbaguy._system.", 29, index, None, FIELDS.get, []).items
    assert [(i.label, i.boost) for i in collections] == [("_query", -1 + 2)]  # boosted by the prefix match


def test_a_name_too_long_for_a_keyspace_is_no_keyspace_and_does_not_fail():
    long = "b" * 300
    assert complete_sqlpp(f"SELECT u. FROM `{long}`.s.c AS u", 9, INDEX, None, FIELDS.get, []).items == []


def test_a_bug_degrades_to_no_items_and_is_logged_without_sql():
    messages = []
    sink = logger.add(messages.append, level="ERROR", format="{message}")

    def broken(keyspace):
        raise RuntimeError("boom with SELECT secret")

    try:
        result = complete_sqlpp("SELECT u. FROM users AS u", 9, INDEX, CTX, broken, [])
    finally:
        logger.remove(sink)
    assert result == CompletionResult(start=9, end=9, items=[]) and len(messages) == 1
    assert "RuntimeError" in messages[0] and "SELECT" not in messages[0] and "boom" not in messages[0]


_PIECES = [
    "SELECT", "FROM", "JOIN", "NEST", "UNNEST", "UPDATE", "INTO", "USING", "THEN", "ON", "KEYS", "AS", "WHERE", "users",
    "orders", "u", "o", "dbaguy", "app", ".", ",", "(", ")", "[", "]", "{", "}", "*", "=", "'", '"', "`", "--", "/*",
    "*/", ";", " ", " ", "\n", "$1", "$x", "?", "\\", "`a\\`b`", "0", "1.", "USE", "LET", "ORDER", "BY", "VALUES",
]


def test_the_unguarded_core_survives_random_token_soup():
    rng = random.Random(20261019)
    for _ in range(500):
        text = "".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 20)))
        cursor = rng.randint(0, len(text))
        for index in (INDEX, None):
            known = _Known(index.keyspaces if index else NO_KEYSPACES, rng.choice([CTX, None]), FIELDS.get, ["seen"])
            _complete(text, cursor, known)


def test_a_statement_with_thousands_of_commas_is_read_in_one_pass():
    """Every comma asking where its clause began took minutes over a big INSERT … VALUES or an IN [ … ] list."""
    inserts = "INSERT INTO users (KEY, VALUE) VALUES " + ", ".join(f"('k{i}', {{'a': [{i}, 2]}})" for i in range(4000))
    started = time.perf_counter()
    assert "RETURNING" in items(run(inserts + " RETURN|"))
    in_list = "SELECT * FROM users AS u WHERE u.age IN [" + ", ".join(map(str, range(20000))) + "] AND u.a|"
    assert "age" in items(run(in_list))
    assert time.perf_counter() - started < 5


def test_seen_fields_rank_before_every_keyword_with_or_without_a_prefix():
    """No sample is the common tab: its seen fields must not sit below AS / ALL / AND / … (or below 56 keywords)."""
    plain = run("SELECT | FROM orders AS o", seen=["age", "zip"])
    labels = [i.label for i in plain.items]
    assert labels[:3] == ["o", "age", "zip"] and {i.kind for i in plain.items[3:]} == {"keyword"}
    typed = run("SELECT * FROM orders AS o WHERE a|", seen=["age", "zip"])  # AS, ALL, AND, ANY, ASC, ARRAY match too
    assert (typed.items[0].label, typed.items[0].detail) == ("age", "seen in results")
    assert typed.items[1].kind == "keyword"
    sampled = [i.label for i in run("SELECT | FROM users AS u", seen=["late"]).items]
    assert sampled.index("name") < sampled.index("late") < sampled.index("AS")  # sampled, then seen, then keywords


def test_matching_the_parentheses_of_a_pasted_statement_is_one_pass():
    """`_after_group` walked to each parenthesis's end: 20,000 unclosed `FROM (` took 83 s."""
    started = time.perf_counter()
    unclosed = "SELECT * FROM (" * 20000
    assert run(unclosed + "|").items  # keywords
    nested = "SELECT * FROM (" * 4000 + "SELECT 1 FROM users AS u" + ") AS s" * 4000 + " WHERE s.|"
    assert run(nested).items == []
    assert time.perf_counter() - started < 3


def test_a_quoted_name_under_the_cursor_is_a_prefix_and_the_items_replace_all_of_it():
    sql = "SELECT * FROM `trav`"
    result = run("SELECT * FROM `trav|`")
    assert (result.start, result.end) == (sql.index("`"), len(sql))  # both quotes
    assert items(result)["travel-sample"].apply == "`travel-sample`" and not {i.kind for i in result.items} & {"keyword"}
    assert [i.label for i in result.items] == ["travel-sample"]
    middle = run("SELECT * FROM `tra|vel`")  # inside a closed name: the whole name goes
    assert [i.label for i in middle.items] == ["travel-sample"] and (middle.start, middle.end) == (14, 22)


def test_the_auto_closed_back_ticks_are_filled_with_a_name():
    result = run("SELECT * FROM `|`")
    assert (result.start, result.end) == (14, 16) and "users" in items(result) and "dbaguy" in items(result)
    assert all(i.kind != "keyword" for i in result.items) and items(result)["odd-name"].apply == "`odd-name`"
    assert items(result)["events"].apply is None  # a name that needs no quotes: the label replaces the quotes too


def test_an_unclosed_back_tick_takes_the_name_up_to_the_cursor_only():
    text = "SELECT * FROM `trav FROM x"
    result = complete_sqlpp(text, 19, INDEX, CTX, FIELDS.get, [])
    assert [i.label for i in result.items] == ["travel-sample"] and (result.start, result.end) == (14, 19)


def test_back_ticked_names_after_a_dot_and_in_columns():
    assert [i.label for i in run("SELECT * FROM `travel-sample`.inv|").items] == ["inventory"]
    after = run("SELECT * FROM `travel-sample`.`inv|`")
    assert [(i.label, i.kind) for i in after.items] == [("inventory", "scope")] and (after.start, after.end) == (30, 35)
    field = run("SELECT u.`odd k|` FROM users AS u")
    assert [(i.label, i.apply) for i in field.items] == [("odd key", "`odd key`")] and (field.start, field.end) == (9, 16)
    columns = run("SELECT `|` FROM users AS u")
    assert [i.label for i in columns.items][:2] == ["u", "name"] and all(i.kind != "keyword" for i in columns.items)
    assert run("`|").items == []  # at the start only keywords fit: none of them is a name


def test_after_a_closed_back_ticked_name_the_keywords_come_back():
    labels = [i.label for i in run("SELECT * FROM `travel-sample`|").items]
    assert "WHERE" in labels and "travel-sample" not in labels


def test_an_alias_is_the_innermost_one_that_defines_it():
    assert items(run("SELECT * FROM users u WHERE EXISTS (SELECT 1 FROM orders u WHERE u.|)")) == {}  # orders: unsampled
    assert "name" in items(run("SELECT * FROM users u WHERE EXISTS (SELECT 1 FROM orders o WHERE u.|)"))  # the outer one
    assert "name" in items(run("WITH x AS (SELECT * FROM orders u) SELECT u.| FROM users u"))  # not the CTE's
    assert "name" in items(run("SELECT COUNT(u.|) FROM users u"))  # a function's parenthesis is no scope of its own
    assert "name" in items(run("SELECT u.| FROM (SELECT * FROM orders u) AS s, users u"))


def test_a_from_that_is_no_keyspace_adds_no_alias_and_no_source():
    found = items(run("SELECT EXTRACT(year FROM u.d), | FROM users AS u"))
    assert "d" not in found and "name" in found  # the one keyspace is still users
    found = items(run("SELECT TRIM(BOTH 'x' FROM u.name) AS t, | FROM users AS u"))
    assert "name" in found and "u" in found
    hinted = items(run("SELECT | FROM users AS u USE INDEX (ix USING GSI)"))
    assert "GSI" not in hinted and "name" in hinted
    assert "name" in items(run("SELECT * FROM (FROM users AS u SELECT u.|)"))  # a FROM-first subquery names one
    assert "users" in items(run("SELECT * FROM (FROM orders AS o, |"))


def test_the_keyspace_tree_keeps_the_snapshot_order_and_flags_system_scopes():
    keyspaces = [SnapshotKeyspace(bucket=b, scope=s, collection=c, system=s == "_system") for b, s, c in [
        ("b", "s", "x"), ("a", "_system", "q"), ("b", "s", "x"), ("b", "t", "y"), ("b", "s", "z")]]
    tree = KeyspaceTree.of(keyspaces)
    assert tree.buckets == ("b", "a") and tree.scopes == {"b": ("s", "t"), "a": ("_system",)}
    assert tree.collections == {("b", "s"): ("x", "z"), ("a", "_system"): ("q",), ("b", "t"): ("y",)}
    assert tree.system_scopes == {("a", "_system")} and tree.is_system("a", "_system") and not tree.is_system("b", "s")
    assert KeyspaceTree.of([]).buckets == ()
