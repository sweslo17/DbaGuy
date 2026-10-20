from uuid import UUID

from ..adapters.registry import AdapterRegistry
from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable
from ..domain.history import HistoryRecord
from ..services.edit_plans import EditPlanStore
from ..services.history_service import HistoryService
from ..services.profile_service import ProfileService
from ..services.safety import GuardDecision, SafetyPolicy
from ..services.schema_cache import SchemaCache
from .worker import ExecutionRecord, WorkerHooks


class SessionHooksFactory:
    """Connects a session's worker to the safety policy, the history, the schema cache and the grid-edit plans."""

    def __init__(
        self, registry: AdapterRegistry, profiles: ProfileService, policy: SafetyPolicy, history: HistoryService,
        schema_cache: SchemaCache, plans: EditPlanStore | None = None,
    ) -> None:
        self._registry = registry
        self._profiles = profiles
        self._policy = policy
        self._history = history
        self._schema_cache = schema_cache
        self._plans = plans

    def build(self, profile_id: UUID, database: str | None) -> WorkerHooks:
        profile = self._profiles.get(profile_id)
        adapter = self._registry.get(profile.engine)
        dialect, capabilities = adapter.dialect, adapter.capabilities

        def check(sql: str, confirmed: bool) -> GuardDecision:
            current = self._profiles.get(profile_id)  # safety level and read-only may change while it is open
            return self._policy.check(sql, dialect, current.safety, current.read_only, confirmed)

        def on_record(record: ExecutionRecord) -> None:
            # Engines with a per-request query context keep that request's context (spec §3.2.3); the others the
            # session's database.
            if capabilities.query_context:
                shown = record.query_context.text() if record.query_context is not None else None
            else:
                shown = database
            self._history.record(HistoryRecord(
                profile_id=profile_id, profile_name=profile.name, database=shown, sql=record.sql,
                started_at=record.started_at, elapsed_ms=record.elapsed_ms, rows=record.rows,
                status=record.status, error=record.error,
            ))

        plans = self._plans

        def take_edit_plan(plan_id: str) -> EditPlan:
            # Only this profile's plans, and only for this session's database when the plan's path names one (CR-2)
            if plans is None:
                raise PlanUnavailable(PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE)
            return plans.take(plan_id, profile_id, database)

        return WorkerHooks(
            dialect=dialect, check=check, on_record=on_record, capabilities=capabilities,
            on_schema_change=lambda: self._schema_cache.invalidate(profile_id),  # every database of the profile
            take_edit_plan=take_edit_plan,
        )
