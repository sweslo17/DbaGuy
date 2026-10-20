"""The plan store (spec ⑤ D-05-7, CR-2): single use, 15 minutes, the oldest out first, bound to profile and database."""

from uuid import uuid4

import pytest

from dbaguy_backend.domain.edits import (
    MAX_PLAN_CHARS, MAX_PLANS, PLAN_EXPIRED, PLAN_MISMATCH, PLAN_TOO_LARGE, PLAN_TTL_S, EditPlan, PlannedStatement,
    PlanUnavailable,
)
from dbaguy_backend.domain.errors import ValidationFailed
from dbaguy_backend.services.edit_plans import EditPlanStore, plan_chars

PROFILE, OTHER = uuid4(), uuid4()


def plan(plan_id: str = "p1", profile_id=PROFILE, database: str | None = None) -> EditPlan:
    return EditPlan(plan_id=plan_id, profile_id=profile_id, database=database, table="t", begin="BEGIN", statements=(),
                    commit="COMMIT", transactional=True, script="")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def code_of(store: EditPlanStore, plan_id: str, profile_id=PROFILE, database: str | None = None) -> str:
    with pytest.raises(PlanUnavailable) as raised:
        store.take(plan_id, profile_id, database)
    return raised.value.code


def test_a_plan_is_taken_once_and_only_by_its_profile_and_database():  # Review Focus 4, CR-2
    store = EditPlanStore()
    store.put(plan("p1", database="dbaguy"))
    assert code_of(store, "p1", OTHER, "dbaguy") == PLAN_MISMATCH
    assert code_of(store, "p1", PROFILE, "analytics") == PLAN_MISMATCH  # and it stays for its own session
    assert store.take("p1", PROFILE, "dbaguy").plan_id == "p1"
    assert code_of(store, "p1", PROFILE, "dbaguy") == PLAN_EXPIRED
    assert code_of(store, "never-made") == PLAN_EXPIRED


def test_a_plan_whose_path_names_no_database_goes_to_any_session_of_its_profile():  # ruling E3
    store = EditPlanStore()
    store.put(plan("p2"))
    assert code_of(store, "p2", OTHER, "dbaguy") == PLAN_MISMATCH
    assert store.take("p2", PROFILE, "dbaguy").plan_id == "p2"


def test_a_plan_lasts_fifteen_minutes():
    clock = Clock()
    store = EditPlanStore(clock=clock)
    store.put(plan("p1"))
    store.put(plan("p2"))
    clock.now += PLAN_TTL_S - 1
    assert store.take("p1", PROFILE, None).plan_id == "p1"
    clock.now += 1
    assert code_of(store, "p2") == PLAN_EXPIRED


def test_the_oldest_plans_make_room():
    store = EditPlanStore()
    for i in range(MAX_PLANS + 1):
        store.put(plan(f"p{i}"))
    assert code_of(store, "p0") == PLAN_EXPIRED
    assert store.take(f"p{MAX_PLANS}", PROFILE, None).plan_id == f"p{MAX_PLANS}"


def sized(plan_id: str, sql: str, script: str) -> EditPlan:
    statement = PlannedStatement(change_id="d:1", kind="delete", sql=sql)
    return EditPlan(plan_id=plan_id, profile_id=PROFILE, database=None, table="t", begin="BEGIN",
                    statements=(statement,), commit="COMMIT", transactional=True, script=script)


def forty(plan_id: str) -> EditPlan:
    return sized(plan_id, "s" * 10, "c" * 30)


def test_a_plan_counts_its_statement_text_and_its_script():  # pre-flight L15
    assert plan_chars(sized("p", "s" * 10, "c" * 30)) == 40
    assert plan_chars(plan()) == 0


def test_the_text_of_all_plans_is_bounded_and_the_oldest_make_room():  # L15
    store = EditPlanStore(max_chars=100)
    for i in range(3):  # 40 each: the third does not fit beside the other two
        store.put(forty(f"p{i}"))
    assert code_of(store, "p0") == PLAN_EXPIRED
    assert [store.take(f"p{i}", PROFILE, None).plan_id for i in (1, 2)] == ["p1", "p2"]


def test_one_large_plan_pushes_out_as_many_as_it_needs():  # L15
    store = EditPlanStore(max_chars=100)
    for i in range(3):
        store.put(sized(f"p{i}", "s" * 10, "c" * 20))  # 30 each
    store.put(sized("big", "s" * 35, "c" * 35))  # 70: p0 and p1 go, p2 stays beside it
    assert [code_of(store, name) for name in ("p0", "p1")] == [PLAN_EXPIRED, PLAN_EXPIRED]
    assert [store.take(name, PROFILE, None).plan_id for name in ("p2", "big")] == ["p2", "big"]


def test_a_plan_taken_or_expired_gives_its_text_back():  # L15
    clock = Clock()
    store = EditPlanStore(clock=clock, max_chars=100)
    store.put(forty("p0"))
    store.put(forty("p1"))
    store.take("p0", PROFILE, None)
    store.put(forty("p2"))  # 80 with p1: nothing is pushed out
    clock.now += PLAN_TTL_S
    store.put(sized("p3", "s" * 50, "c" * 50))  # p1 and p2 expired: their 80 are free, and exactly 100 fits
    assert store.take("p3", PROFILE, None).plan_id == "p3"
    assert [code_of(store, name) for name in ("p1", "p2")] == [PLAN_EXPIRED, PLAN_EXPIRED]


def test_the_same_plan_put_again_is_counted_once():  # L15
    store = EditPlanStore(max_chars=100)
    store.put(forty("p0"))
    store.put(forty("p0"))
    store.put(forty("p1"))  # 80, not 120
    assert [store.take(name, PROFILE, None).plan_id for name in ("p0", "p1")] == ["p0", "p1"]


def test_a_plan_larger_than_the_bound_is_refused_and_disturbs_nothing():  # L15
    store = EditPlanStore(max_chars=100)
    store.put(forty("small"))
    with pytest.raises(ValidationFailed) as raised:
        store.put(sized("big", "s" * 51, "c" * 50))
    assert raised.value.message == PLAN_TOO_LARGE and raised.value.status == 422
    assert code_of(store, "big") == PLAN_EXPIRED
    assert store.take("small", PROFILE, None).plan_id == "small"
    store.put(sized("exact", "s" * 50, "c" * 50))  # exactly the bound is kept
    assert store.take("exact", PROFILE, None).plan_id == "exact"


def test_the_default_bound_is_128_mib_of_statement_and_script_text():  # L15
    assert MAX_PLAN_CHARS == 128 * 1024 * 1024
    half = "x" * (MAX_PLAN_CHARS // 2)
    store = EditPlanStore()
    store.put(sized("exact", half, half))
    with pytest.raises(ValidationFailed):
        store.put(sized("over", half, half + "x"))
    assert store.take("exact", PROFILE, None).plan_id == "exact"
