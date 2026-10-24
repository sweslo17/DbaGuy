"""The previewed plans a session may apply (spec ⑤ D-05-7, R-05-7, CR-2, pre-flight L15): the one-time plan store
(`plans.OneTimePlanStore`, ⑥ D-06-8) over grid-edit plans, whose text is their statements and script. A plan larger than
every plan's bound together is refused at the preview (`PLAN_TOO_LARGE`, 422)."""

from collections.abc import Callable

from ..domain.edits import MAX_PLAN_CHARS, MAX_PLANS, PLAN_TOO_LARGE, PLAN_TTL_S, EditPlan
from .plans import OneTimePlanStore, awake_or_asleep


def plan_chars(plan: EditPlan) -> int:
    """What a plan holds that grows with the changes: each statement's text and the script (which repeats them)."""
    return len(plan.script) + sum(len(statement.sql) for statement in plan.statements)


class EditPlanStore(OneTimePlanStore[EditPlan]):
    def __init__(
        self, ttl_s: float = PLAN_TTL_S, capacity: int = MAX_PLANS, clock: Callable[[], float] = awake_or_asleep,
        max_chars: int = MAX_PLAN_CHARS,
    ) -> None:
        super().__init__(plan_chars, PLAN_TOO_LARGE, what="edit plan", ttl_s=ttl_s, capacity=capacity, clock=clock,
                         max_chars=max_chars)
