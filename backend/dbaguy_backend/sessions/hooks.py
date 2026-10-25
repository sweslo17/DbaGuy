import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import UUID

from ..adapters.base import StatementError
from ..adapters.registry import AdapterRegistry
from ..domain.classification import Classification, GuardReason, StatementKind
from ..domain.data_jobs import BE_DUMPED, RUN_SQL_FILES, JobCommand, cannot
from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable
from ..domain.errors import DomainError
from ..domain.execution import CopyDataCommand, DumpCommand, RunFileCommand
from ..domain.history import HistoryRecord, HistoryStatus
from ..domain.profiles import AnyProfile, Engine, SqliteProfile
from ..domain.transfer import COPY_DATA
from ..services.edit_plans import EditPlanStore
from ..services.agent_gate import agent_read
from ..services.history_service import HistoryService
from ..services.profile_service import ProfileService
from ..services.safety import GuardAction, GuardDecision, SafetyPolicy
from ..services.schema_cache import SchemaCache
from ..services.transfer_plans import TransferPlanStore
from .jobs.context import Job, JobHooks, PreparedJob
from .jobs.dump import DumpJob
from .jobs.run_file import RunFileJob
from .jobs.transfer import SOURCE_UNAVAILABLE, TransferSink
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
    """Connects a session's worker to the safety policy, the history, the schema cache, the grid-edit plans and the
    copy plans."""

    def __init__(
        self, registry: AdapterRegistry, profiles: ProfileService, policy: SafetyPolicy, history: HistoryService,
        schema_cache: SchemaCache, plans: EditPlanStore | None = None, transfers: TransferPlanStore | None = None,
    ) -> None:
        self._registry = registry
        self._profiles = profiles
        self._policy = policy
        self._history = history
        self._schema_cache = schema_cache
        self._plans = plans
        self._transfers = transfers

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

    def build_agent(self, profile_id: UUID) -> WorkerHooks:
        """An MCP agent's session (spec ⑧ D-08-1): the agent gate as its check (the MCP layer ran it already: this is
        the backstop), no read-ahead while paused and no rows read ahead of a page (CR-08-7), and no History, schema
        invalidation, edit plans or jobs: the MCP layer writes its own History (one record per tool call)."""
        adapter = self._registry.get(self._profiles.get(profile_id).engine)
        dialect = adapter.dialect

        def check(sql: str, confirmed: bool) -> GuardDecision:
            verdict = agent_read(sql, dialect)
            if not verdict.allowed:  # an explain's text: the head and one statement the gate allows
                verdict = agent_read(sql, dialect, "explain")
            if verdict.allowed:
                return GuardDecision(action=GuardAction.RUN, classification=verdict.classification)
            kind = verdict.classification or Classification(kind=StatementKind.UNKNOWN)
            return GuardDecision(action=GuardAction.BLOCK, reasons=[GuardReason.READ_ONLY], classification=kind)

        return WorkerHooks(dialect=dialect, check=check, describe=False,
                           capabilities=replace(adapter.capabilities, read_ahead_bytes=None))

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
            case PreparedJob():  # made by another job (a copy's source, 06c)
                return command.job
            case RunFileCommand() if capabilities.run_file:
                return RunFileJob(command, profile.name)
            case RunFileCommand():
                raise StatementError(cannot(profile.engine, RUN_SQL_FILES), code="not_supported")
            case DumpCommand() if capabilities.dump:
                source = Path(profile.path) if isinstance(profile, SqliteProfile) else None  # never its own target
                return DumpJob(command, profile.name, Engine(profile.engine), source_file=source)
            case DumpCommand():
                raise StatementError(cannot(profile.engine, BE_DUMPED), code="not_supported")
            case CopyDataCommand() if capabilities.copy_data and self._transfers is not None:
                return self._copy(command, profile, database)
            case CopyDataCommand():
                raise StatementError(cannot(profile.engine, COPY_DATA), code="not_supported")
        raise StatementError(f"{type(command).__name__} is not a job this connection runs", code="not_supported")

    def _copy(self, command: CopyDataCommand, profile: AnyProfile, database: str | None) -> Job:
        """The previewed copy, taken once: by this profile's session and, where the engine connects per database, this
        database's (D-06-8; `plan_expired`, `plan_mismatch` refuse it before anything is sent)."""
        assert self._transfers is not None
        try:
            plan = self._transfers.take(command.plan_id, profile.id, database)
        except PlanUnavailable as exc:
            raise StatementError(exc.message, code=exc.code) from None
        try:  # the source connection was deleted since the preview
            source = self._registry.get(self._profiles.get(plan.source_profile_id).engine)
        except DomainError as exc:
            raise StatementError(SOURCE_UNAVAILABLE.format(exc.message), code=exc.code) from None
        return TransferSink(command, plan, source.dialect)
