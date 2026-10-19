"""The previewed plans a session may apply (spec ⑤ D-05-7, R-05-7, CR-2, pre-flight L15): in memory, each for PLAN_TTL_S,
taken once and only by a session of its profile and — when its path names one — its database (ruling E3). At most
MAX_PLANS and MAX_PLAN_CHARS of statement and script text together: the plan made longest ago goes first (a plan is
used once, by being taken, so that is also the least recently used). A backend restart forgets them all: the client
previews again."""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from loguru import logger

from ..domain.edits import (
    MAX_PLAN_CHARS, MAX_PLANS, PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, PLAN_MISMATCH, PLAN_MISMATCH_MESSAGE, PLAN_TOO_LARGE,
    PLAN_TTL_S, EditPlan, PlanUnavailable,
)
from ..domain.errors import ValidationFailed


def plan_chars(plan: EditPlan) -> int:
    """What a plan holds that grows with the changes: each statement's text and the script (which repeats them)."""
    return len(plan.script) + sum(len(statement.sql) for statement in plan.statements)


@dataclass(frozen=True, slots=True)
class _Entry:
    made: float
    chars: int
    plan: EditPlan


class EditPlanStore:
    def __init__(
        self, ttl_s: float = PLAN_TTL_S, capacity: int = MAX_PLANS, clock: Callable[[], float] = time.monotonic,
        max_chars: int = MAX_PLAN_CHARS,
    ) -> None:
        self._ttl_s = ttl_s
        self._capacity = capacity
        self._max_chars = max_chars
        self._clock = clock
        self._plans: OrderedDict[str, _Entry] = OrderedDict()  # oldest made first
        self._chars = 0  # the sum of every entry's `chars`
        self._lock = threading.Lock()

    def put(self, plan: EditPlan) -> None:
        """Keep `plan`, making room: the expired go first, then the oldest until both bounds hold. A plan larger than the
        whole text bound is refused (ValidationFailed, 422) and nothing else is touched."""
        chars = plan_chars(plan)
        if chars > self._max_chars:
            raise ValidationFailed(PLAN_TOO_LARGE)
        with self._lock:
            self._prune()
            self._remove(plan.plan_id)
            self._plans[plan.plan_id] = _Entry(self._clock(), chars, plan)
            self._chars += chars
            evicted = 0
            while len(self._plans) > self._capacity or self._chars > self._max_chars:
                self._remove(next(iter(self._plans)))
                evicted += 1
            if evicted:
                logger.debug("{} older edit plan(s) made room for a new one ({} stored)", evicted, len(self._plans))

    def take(self, plan_id: str, profile_id: UUID, database: str | None) -> EditPlan:
        """The plan, removed: it runs once. PlanUnavailable(plan_expired) when there is none (expired, made room for,
        taken already, never made); PlanUnavailable(plan_mismatch) when it is another profile's or another database's,
        and it then stays for its own session."""
        with self._lock:
            self._prune()
            entry = self._plans.get(plan_id)
            if entry is None:
                raise PlanUnavailable(PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE)
            plan = entry.plan
            if plan.profile_id != profile_id or (plan.database is not None and plan.database != database):
                raise PlanUnavailable(PLAN_MISMATCH, PLAN_MISMATCH_MESSAGE)
            self._remove(plan_id)
            return plan

    def _prune(self) -> None:
        now = self._clock()
        for plan_id in [plan_id for plan_id, entry in self._plans.items() if now - entry.made >= self._ttl_s]:
            self._remove(plan_id)

    def _remove(self, plan_id: str) -> None:
        entry = self._plans.pop(plan_id, None)
        if entry is not None:
            self._chars -= entry.chars
