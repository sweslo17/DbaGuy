"""Whether a PostgreSQL plan reads a table in its key order without waiting for a sort (05b K13): the walk over EXPLAIN
(FORMAT JSON)'s tree that gives an edit target its `stable_order`. Pure: no database."""

from dbaguy_backend.adapters.postgres.editing import ordered_plan


def node(kind: str, *children: dict, rows: float = 1_000_000) -> dict:
    return {"Node Type": kind, "Plan Rows": rows, **({"Plans": list(children)} if children else {})}


def explained(root: dict) -> list:
    return [{"Plan": root, "Planning Time": 0.1}]


def test_an_index_scan_and_ordered_merges_read_in_order():
    assert ordered_plan(explained(node("Index Scan")))
    assert ordered_plan(explained(node("Index Only Scan")))
    partitions = node("Merge Append", node("Index Scan"), node("Index Scan"), node("Index Only Scan"))
    assert ordered_plan(explained(partitions))
    assert ordered_plan(explained(node("Gather Merge", node("Index Scan"))))
    assert ordered_plan(explained(node("Result")))  # a partitioned table without partitions: nothing to sort


def test_a_sort_anywhere_in_a_big_plan_is_not_a_stable_order():
    assert not ordered_plan(explained(node("Sort", node("Seq Scan"))))
    assert not ordered_plan(explained(node("Incremental Sort", node("Index Scan"))))
    deep = node("Merge Append", node("Index Scan"), node("Append", node("Sort", node("Seq Scan"))))
    assert not ordered_plan(explained(deep))
    assert not ordered_plan(explained(node("Gather Merge", node("Sort", node("Parallel Seq Scan")))))


def test_a_small_sort_is_fine_and_the_root_estimate_decides():
    # A small table: the planner sorts because that is cheapest, and the first page waits for a few rows only.
    assert ordered_plan(explained(node("Sort", node("Seq Scan", rows=50), rows=50)))
    assert ordered_plan(explained(node("Sort", node("Seq Scan"), rows=10_000)))
    assert not ordered_plan(explained(node("Sort", node("Seq Scan"), rows=10_001)))
    # Each partition's sort is small, the table is not.
    parts = node("Merge Append", *(node("Sort", node("Seq Scan"), rows=5_000) for _ in range(4)), rows=20_000)
    assert not ordered_plan(explained(parts))


def test_any_other_shape_is_not_a_stable_order():
    for value in (None, "[]", [], {}, [None], [{}], [{"Plan": None}], [{"Plan": []}],
                  explained({"Node Type": "Index Scan", "Plans": "x"}),
                  explained({"Node Type": "Index Scan", "Plans": [None]}),
                  explained({"Node Type": "Sort", "Plan Rows": "50"}),
                  explained({"Node Type": "Sort", "Plan Rows": True}),
                  explained({"Node Type": "Sort"})):
        assert not ordered_plan(value), value


def test_a_deep_plan_is_walked_without_recursion():
    root = node("Index Scan")
    for _ in range(5_000):
        root = node("Append", root)
    assert ordered_plan(explained(root))
    root["Plans"].append(node("Sort", node("Seq Scan")))
    assert not ordered_plan(explained(root))
