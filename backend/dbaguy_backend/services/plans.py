"""Previewed plans a session may run once (spec ⑥ D-06-8, from ⑤ D-05-7): in memory, each for PLAN_TTL_S, taken once
and only by a session the plan names (its profile, and its database when it names one, ⑤ E3). At most MAX_PLANS and
MAX_PLAN_CHARS of their text together: the plan made longest ago goes first (a plan is used once, by being taken, so that
is also the least recently used). A backend restart forgets them all: the client previews again. Grid edits keep their
plans in one store (`edit_plans.EditPlanStore`), copies in another (`transfer_plans.TransferPlanStore`)."""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar
from uuid import UUID

from loguru import logger

from ..domain.edits import (
    MAX_PLAN_CHARS, MAX_PLANS, PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, PLAN_MISMATCH, PLAN_TTL_S, PlanUnavailable,
)
from ..domain.errors import ValidationFailed


class Plan(Protocol):
    @property
    def plan_id(self) -> str: ...

    @property
    def profile_id(self) -> UUID: ...

    @property
    def database(self) -> str | None: ...


P = TypeVar("P", bound=Plan)


@dataclass(frozen=True, slots=True)
class _Entry(Generic[P]):
    made: float
    chars: int
    plan: P


def awake_or_asleep() -> float:
    """Seconds that keep counting while the Mac sleeps: `time.monotonic` is `mach_absolute_time` there, which stops, so a
    plan would outlive its 15 minutes across a closed lid."""
    return time.clock_gettime(time.CLOCK_MONOTONIC)


class OneTimePlanStore(Generic[P]):
    """`chars(plan)`: what a plan holds that grows with it (its statements' text); `too_large`: the refusal of a plan
    larger than every plan's bound together; `expired`: what a plan that cannot be taken says (spec §4: plan_expired
    and plan_mismatch share it), ⑤'s "preview the changes again" unless the store's plans are another kind's."""

    def __init__(
        self, chars: Callable[[P], int], too_large: str, *, what: str = "plan", ttl_s: float = PLAN_TTL_S,
        capacity: int = MAX_PLANS, clock: Callable[[], float] = awake_or_asleep, max_chars: int = MAX_PLAN_CHARS,
        expired: str = PLAN_EXPIRED_MESSAGE,
    ) -> None:
        self._chars_of = chars
        self._too_large = too_large
        self._expired = expired
        self._what = what
        self._ttl_s = ttl_s
        self._capacity = capacity
        self._max_chars = max_chars
        self._clock = clock
        self._plans: OrderedDict[str, _Entry[P]] = OrderedDict()  # oldest made first
        self._chars = 0  # the sum of every entry's `chars`
        self._lock = threading.Lock()

    def put(self, plan: P) -> None:
        """Keep `plan`, making room: the expired go first, then the oldest until both bounds hold. A plan larger than the
        whole text bound is refused (ValidationFailed, 422) and nothing else is touched."""
        chars = self._chars_of(plan)
        if chars > self._max_chars:
            raise ValidationFailed(self._too_large)
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
                logger.debug("{} older {}(s) made room for a new one ({} stored)", evicted, self._what,
                             len(self._plans))

    def take(self, plan_id: str, profile_id: UUID, database: str | None) -> P:
        """The plan, removed: it runs once. PlanUnavailable(plan_expired) when there is none (expired, made room for,
        taken already, never made); PlanUnavailable(plan_mismatch) when it is another profile's or another database's,
        and it then stays for its own session."""
        with self._lock:
            self._prune()
            entry = self._plans.get(plan_id)
            if entry is None:
                raise PlanUnavailable(PLAN_EXPIRED, self._expired)
            plan = entry.plan
            if plan.profile_id != profile_id or (plan.database is not None and plan.database != database):
                raise PlanUnavailable(PLAN_MISMATCH, self._expired)
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
