"""EXPLAIN / ADVISE / profile texts as a plan view (R-04-13, spec §3.10): operator trees from the dev cluster's own
answers (fixtures, 04b Task 1), the four anti-pattern flags and the profile's slowest operator."""

import json

import pytest

from dbaguy_backend.adapters.couchbase.explain import analyze, go_duration_ns
from dbaguy_backend.domain.couchbase import IndexAdvice, PlanNode, PlanView
from tests.support import plan_fixture


def walk(node: PlanNode):
    yield node
    for child in node.children:
        yield from walk(child)


def titles(node: PlanNode) -> list[str]:
    return [n.title for n in walk(node)]


def by_id(view: PlanView) -> dict[int, PlanNode]:
    roots = [view.root, *(s.root for s in view.subqueries)]
    return {n.id: n for root in roots if root for n in walk(root)}


def flags(view: PlanView) -> list[tuple[str, str, str]]:
    nodes = by_id(view)
    return [(f.badge, f.severity, nodes[f.node_id].title) for f in view.findings]


def test_a_covered_scan_is_clean():
    view = analyze(plan_fixture("explain_covered"))
    assert view.kind == "plan" and view.statement_text.startswith("SELECT o.amount FROM dbaguy.app.orders")
    assert titles(view.root) == ["Sequence", "IndexScan3 · idx_orders_amount", "Parallel", "Sequence", "Filter",
                                 "InitialProject"]
    scan = view.root.children[0]
    assert ("keyspace", "dbaguy.app.orders") in [(d.label, d.value) for d in scan.details]
    assert any(d.label == "covers" and "cover ((`o`.`amount`))" in d.value for d in scan.details)
    assert view.findings == []


def test_a_fetch_after_an_uncovering_scan_is_noted():
    assert flags(analyze(plan_fixture("explain_fetch"))) == [("not_covered", "info", "IndexScan3 · idx_orders_amount")]


def test_a_primary_scan_is_flagged():
    assert flags(analyze(plan_fixture("explain_primary"))) == [("primary_scan", "warning", "PrimaryScan3 · #primary")]


def test_a_sequential_scan_is_flagged():
    assert flags(analyze(plan_fixture("explain_sequential"))) == [
        ("sequential_scan", "warning", "PrimaryScan3 · #sequentialscan"),
    ]


def test_an_intersect_scan_is_flagged_with_its_scans_as_children():
    view = analyze(plan_fixture("explain_intersect"))
    assert flags(view) == [("intersect_scan", "warning", "IntersectScan"), ("not_covered", "info", "IntersectScan")]
    intersect = next(n for n in walk(view.root) if n.operator == "IntersectScan")
    assert [c.title for c in intersect.children] == ["IndexScan3 · def_inventory_airport_faa",
                                                     "IndexScan3 · def_inventory_airport_city"]


def test_a_join_carries_estimates():
    view = analyze(plan_fixture("explain_join"))
    assert view.estimates is not None and view.estimates.cost > 0
    join = next(n for n in walk(view.root) if n.operator == "Join")
    assert join.title == "Join · a" and any(d.label == "on_keys" for d in join.details)
    assert join.estimates is not None and join.estimates.cardinality is not None


def test_subqueries_get_their_own_trees_and_ids_never_repeat():
    view = analyze(plan_fixture("explain_subquery"))
    assert len(view.subqueries) == 1 and view.subqueries[0].correlated is False
    assert titles(view.subqueries[0].root)[:2] == ["Sequence", "CountScan · users"]
    ids = [n.id for root in (view.root, view.subqueries[0].root) for n in walk(root)]
    assert ids == list(range(len(ids)))


def test_advice_lists_current_then_recommended_then_covering():
    view = analyze(plan_fixture("advise_covering"))
    assert view.kind == "advice" and view.statement_text.startswith("SELECT a.name FROM `travel-sample`")
    assert [a.kind for a in view.advice] == ["current", "recommended", "covering"]
    assert view.advice[1].statement.startswith("CREATE INDEX adv_country_geo_alt ON ")
    assert view.advice[1].rule.startswith("Index keys follow order of predicate types")
    simple = analyze(plan_fixture("advise_indexes"))
    assert simple.advice[0] == IndexAdvice(
        kind="current", keyspace_alias="orders_o",
        statement="CREATE INDEX idx_orders_amount ON `default`:`dbaguy`.`app`.`orders`(`amount` DESC)")


def test_no_recommendation_keeps_the_servers_words():
    view = analyze(plan_fixture("advise_none"))
    assert view.advice_note == "No index recommendation at this time."
    assert view.advice[0].rule == "THIS IS AN OPTIMAL COVERING INDEX."


@pytest.mark.parametrize("name, slowest", [("profile_timings", "IndexScan3 · idx_orders_amount"),
                                          ("profile_join", None)])
def test_a_profile_has_stats_and_names_a_dominant_operator(name, slowest):
    view = analyze(plan_fixture(name))
    assert view.kind == "profile" and view.root.operator == "Authorize" and view.phase_times
    timed = [n for n in walk(view.root) if n.stats is not None]
    assert timed and all(go_duration_ns(n.stats.exec_time) is not None for n in timed if n.stats.exec_time)
    found = [title for badge, _, title in flags(view) if badge == "slowest"]
    if slowest is not None:  # the index scan's service time dominates this tiny query
        assert found == [slowest]
    else:  # a join spreads its time: at most one operator may hold more than half
        assert len(found) <= 1


def test_unknown_operators_are_still_walked():
    text = json.dumps({"plan": {"#operator": "Sequence", "~children": [
        {"#operator": "UnionScan", "scans": [{"#operator": "IndexScan3", "index": "a", "covers": ["c"]}]},
        {"#operator": "NestedLoopJoin", "alias": "b", "~child": {"#operator": "IndexScan3", "index": "b"}},
        {"#operator": "Future", "inner": {"#operator": "Deeper"}, "#stats": {"#operator": "not a child"}},
    ]}, "text": "SELECT 1"})
    assert titles(analyze(text).root) == ["Sequence", "UnionScan", "IndexScan3 · a", "NestedLoopJoin · b",
                                          "IndexScan3 · b", "Future", "Deeper"]


def test_a_fetch_of_another_alias_is_not_an_uncovered_scan():
    text = json.dumps({"plan": {"#operator": "Sequence", "~children": [
        {"#operator": "IndexScan3", "index": "i", "as": "a", "keyspace": "k"},
        {"#operator": "Fetch", "as": "b", "keyspace": "k2"},
    ]}, "text": "x"})
    assert analyze(text).findings == []


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '{"a": 1}', "null"])
def test_anything_else_is_unknown(text):
    assert analyze(text).kind == "unknown"


@pytest.mark.parametrize("text, ns", [
    ("1h2m3.5s", 3_723_500_000_000), ("34.611027ms", 34_611_027), ("10µs", 10_000), ("10μs", 10_000),
    ("4.792us", 4_792), ("0s", 0), ("1.9ns", 1), ("-2ms", -2_000_000), ("1m0.001s", 60_001_000_000),
    ("", None), ("5", None), ("abc", None), ("1x", None), ("ms", None),
])
def test_go_duration_ns(text, ns):
    assert go_duration_ns(text) == ns


def test_details_come_in_a_fixed_order_and_are_clipped():
    text = json.dumps({"plan": {"#operator": "Fetch", "as": "o", "bucket": "b", "scope": "s", "keyspace": "k",
                                "namespace": "default", "index_id": "x", "limit": 5, "condition": "c" * 400,
                                "sort_terms": [{"expr": "a", "desc": True}], "preserve_order": True, "early": ["a"],
                                "#stats": {"execTime": "1ms"}, "~extra": "no"}})
    details = [(d.label, d.value) for d in analyze(text).root.details]
    assert details == [("keyspace", "b.s.k"), ("condition", "c" * 299 + "…"), ("limit", "5"),
                       ("sort_terms", '[{"expr":"a","desc":true}]'), ("preserve_order", "true")]


def test_what_the_server_never_writes_does_not_break_the_view():
    nested: dict = {"#operator": "Leaf"}
    for _ in range(3000):
        nested = {"#operator": "Wrap", "~child": nested}
    assert analyze(json.dumps({"plan": nested})).kind == "unknown"  # deeper than the interpreter follows
    odd = '{"cost": 1e999, "cardinality": true, "text": 5, "plan": {"#operator": "Sequence", "~children": [1, null]}}'
    view = analyze(odd)
    assert view.kind == "plan" and view.estimates is None and view.statement_text is None and view.root.children == []


def test_advice_takes_several_infos_and_the_distinct_scan_is_checked_like_the_others():
    advice = analyze(json.dumps({"#operator": "Advise", "query": "q", "advice": {"adviseinfo": [
        {"recommended_indexes": {"indexes": [{"index_statement": "CREATE INDEX a ON k(x)",
                                              "update_statistics": "UPDATE STATISTICS FOR k(x)"}]}},
        {"current_indexes": [{"index_statement": "CREATE INDEX b ON k(y)"}, {"keyspace_alias": "no statement"}]},
    ]}}))
    assert [(a.kind, a.statement, a.update_statistics) for a in advice.advice] == [
        ("current", "CREATE INDEX b ON k(y)", None),
        ("recommended", "CREATE INDEX a ON k(x)", "UPDATE STATISTICS FOR k(x)")]
    plan = json.dumps({"plan": {"#operator": "Sequence", "~children": [
        {"#operator": "DistinctScan", "scan": {"#operator": "IndexScan3", "index": "i", "as": "a"}},
        {"#operator": "Fetch", "as": "a"}]}})
    assert flags(analyze(plan)) == [("not_covered", "info", "DistinctScan")]
