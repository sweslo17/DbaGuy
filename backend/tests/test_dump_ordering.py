"""`Ordering` (adapters/dump.py): dependencies first, otherwise creation order, and a cycle never stops it. The heap
version must give exactly what the plan's direct rule gives ("the first key whose dependencies are all placed goes
next; when none is ready, the first one does"), only faster (pre-flight L15). Pure."""

import random
import time

from dbaguy_backend.adapters.dump import Ordering


def reference(depends: dict[str, set[str]], keys: list[str]) -> list[str]:
    """The plan's Ordering.order, word for word (a self-dependency is none: the catalogs never record one)."""
    wanted = set(keys)
    placed: list[str] = []
    done: set[str] = set()
    pending = list(keys)
    while pending:
        ready = next((key for key in pending if not ((depends.get(key, set()) - {key}) & wanted) - done), pending[0])
        placed.append(ready)
        done.add(ready)
        pending.remove(ready)
    return placed


def test_dependencies_first_then_creation_order_and_a_cycle_goes_in_creation_order():
    ordering = Ordering({"view_top": {"view_base"}, "a": {"b"}, "b": {"a"}, "x": {"not in the dump"}})
    assert ordering.order(["view_top", "view_base", "x", "a", "b"]) == ["view_base", "view_top", "x", "a", "b"]
    assert Ordering({"a": {"a"}}).order(["a", "b"]) == ["a", "b"]
    assert Ordering().order([]) == []


def test_it_gives_what_the_plans_rule_gives_on_random_graphs():
    rng = random.Random(6)
    for _ in range(3000):
        size = rng.randint(1, 14)
        keys = [f"k{i}" for i in rng.sample(range(40), size)]
        depends = {key: {rng.choice([*keys, "outside"]) for _ in range(rng.randint(0, 3))} for key in keys
                   if rng.random() < 0.7}
        assert Ordering({k: set(v) for k, v in depends.items()}).order(keys) == reference(depends, keys), (keys, depends)


def test_a_long_reverse_chain_is_not_quadratic():
    keys = [f"v{i}" for i in range(20_000)]
    depends = {keys[i]: {keys[i + 1]} for i in range(len(keys) - 1)}  # each view reads the next, made later
    started = time.perf_counter()
    assert Ordering(depends).order(keys) == list(reversed(keys))
    assert time.perf_counter() - started < 2.0
