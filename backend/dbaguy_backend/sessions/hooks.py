from uuid import UUID

from ..adapters.registry import AdapterRegistry
from ..domain.history import HistoryRecord
from ..services.history_service import HistoryService
from ..services.profile_service import ProfileService
from ..services.safety import GuardDecision, SafetyPolicy
from ..services.schema_cache import SchemaCache
from .worker import ExecutionRecord, WorkerHooks


class SessionHooksFactory:
    """Connects a session's worker to the safety policy, the history and the schema cache."""

    def __init__(
        self, registry: AdapterRegistry, profiles: ProfileService, policy: SafetyPolicy, history: HistoryService,
        schema_cache: SchemaCache,
    ) -> None:
        self._registry = registry
        self._profiles = profiles
        self._policy = policy
        self._history = history
        self._schema_cache = schema_cache

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

        return WorkerHooks(
            dialect=dialect, check=check, on_record=on_record, capabilities=capabilities,
            on_schema_change=lambda: self._schema_cache.invalidate(profile_id),  # every database of the profile
        )
