import threading
import time
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from ..adapters.base import StatementError
from ..adapters.registry import AdapterRegistry
from ..domain.data_jobs import RUN_SQL_FILES, JobCommand, cannot
from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable
from ..domain.execution import RunFileCommand
from ..domain.history import HistoryRecord, HistoryStatus
from ..domain.profiles import AnyProfile
from ..services.edit_plans import EditPlanStore
from ..services.history_service import HistoryService
from ..services.profile_service import ProfileService
from ..services.safety import GuardDecision, SafetyPolicy
from ..services.schema_cache import SchemaCache
from .jobs.context import Job, JobHooks
from .jobs.run_file import RunFileJob
from .worker import ExecutionRecord, WorkerHooks

READ_ONLY_RECHECK_S = 1.0  # a job's statement gate re-reads the profile at most this often (R-06-6)


class _Recent:
    """`read()`, re-read at most every `every_s`: ProfileStore.get reads profiles.json each time, and a run of a
    million statements gates each one (R-06-6). Any thread."""

    def __init__(self, read: Callable[[], bool], every_s: float = READ_ONLY_RECHECK_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._read = read
        self._every_s = every_s
        self._clock = clock
        self._lock = threading.Lock()
        self._value = False
        self._read_at: float | None = None

    def __call__(self) -> bool:
        with self._lock:
            now = self._clock()
            if self._read_at is None or now - self._read_at >= self._every_s:
                self._value, self._read_at = self._read(), now
            return self._value


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
            take_edit_plan=take_edit_plan, jobs=self._job_hooks(profile, database),
        )

    def _job_hooks(self, profile: AnyProfile, database: str | None) -> JobHooks:
        """A job's statement gate (gate_confirmed, the profile re-read at most once a second) and its History record."""
        profile_id = profile.id
        dialect = self._registry.get(profile.engine).dialect
        read_only = _Recent(lambda: self._profiles.get(profile_id).read_only)

        def build(command: JobCommand) -> Job:
            return self.build_job(command, profile, database)

        def begin_record(sql: str, started_at: datetime) -> int | None:
            return self._history.begin(HistoryRecord(
                profile_id=profile_id, profile_name=profile.name, database=database, sql=sql, started_at=started_at,
                elapsed_ms=0, rows=0, status="running",
            ))

        def finish_record(record_id: int, status: HistoryStatus, elapsed_ms: int, rows: int, error: str | None) -> None:
            self._history.finish(record_id, status, elapsed_ms, rows, error)

        return JobHooks(
            build=build, gate=lambda sql: self._policy.gate_confirmed(sql, dialect, read_only()), read_only=read_only,
            begin_record=begin_record, finish_record=finish_record,
        )

    def build_job(self, command: JobCommand, profile: AnyProfile, database: str | None) -> Job:
        """The job object for a job command on a session of `profile` (the composition root of data tools' jobs). A
        command the engine has no tool for is refused with §4's sentence (pre-flight L4)."""
        capabilities = self._registry.get(profile.engine).capabilities
        match command:
            case RunFileCommand() if capabilities.run_file:
                return RunFileJob(command, profile.name)
            case RunFileCommand():
                raise StatementError(cannot(profile.engine, RUN_SQL_FILES), code="not_supported")
        raise StatementError(f"{type(command).__name__} is not a job this connection runs", code="not_supported")
