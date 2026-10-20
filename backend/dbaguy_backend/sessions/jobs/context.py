"""The job framework (spec ⑥ D-06-2): a job is an object with one `run(ctx)`; the worker gives it a JobContext — the
worker's own statement path (arm/disarm, cancel checks, `describe=False`), progress, messages, the History record —
and sends whatever the job reports as exactly one `job_outcome`. Jobs import this module, never the worker."""

import math
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, ClassVar

from loguru import logger
from pydantic import BaseModel

from ...adapters.base import (
    AdapterCapabilities, DatabaseConnection, RunOptions, StatementCancelled, StatementError, StatementResult,
)
from ...domain.classification import Classification, StatementKind
from ...domain.data_jobs import (
    MAX_JOB_MESSAGES, PROGRESS_INTERVAL_S, JobCommand, JobError, JobKind, JobOutcomeEvent, JobOutcomeKind,
    JobProgressEvent, JobStage,
)
from ...domain.execution import MessageEvent, MessageSeverity, ServerMessage
from ...domain.history import HistoryStatus
from ...services.safety import GuardAction, GuardDecision
from ...sql.dialects import Dialect

NO_DESCRIBE = RunOptions(describe=False)  # a job shows no result set: SQL Server skips the describe (⑤ M1)
# A broken job's transaction that would not roll back (pre-flight L2): a job's dialog has no Rollback, and the server
# rolls it back when the job's session closes (its dialog closes it, or the reaper once it is idle)
JOB_ROLLBACK_FAILED = "The transaction could not be rolled back: it ends when the job's session closes"
_DRAIN_CHUNK = 1000  # rows read and dropped at a time


def _no_job(command: JobCommand) -> "Job":
    raise StatementError("This connection cannot run this job", code="not_supported")


def _run_everything(sql: str) -> GuardDecision:
    return GuardDecision(action=GuardAction.RUN, classification=Classification(kind=StatementKind.UNKNOWN))


def _writable() -> bool:
    return False


def _no_record(sql: str, started_at: datetime) -> int | None:
    return None


def _ignore(*_: object) -> None:
    return None


@dataclass(frozen=True)
class JobHooks:
    """How a job reaches the rest of the app (a session's WorkerHooks.jobs). The defaults run nothing and record
    nothing."""

    build: Callable[[JobCommand], "Job"] = _no_job  # raises StatementError for a command it cannot run
    gate: Callable[[str], GuardDecision] = _run_everything  # SafetyPolicy.gate_confirmed for this session (R-06-6)
    read_only: Callable[[], bool] = _writable  # the profile is read-only now
    begin_record: Callable[[str, datetime], int | None] = _no_record  # History (D-06-9): the record's id
    finish_record: Callable[[int, HistoryStatus, int, int, str | None], None] = _ignore  # id, status, ms, rows, error
    announce: Callable[[str, JobKind, str], None] = _ignore  # the job list (D-06-10): request id, kind, title
    report: Callable[[BaseModel], None] = _ignore  # the job list: each job_progress and the job_outcome


@dataclass
class JobProgress:
    """What the next job_progress says: the job updates it, `JobContext.tick` sends it."""

    stage: JobStage = "starting"
    object: str | None = None
    rows: int = 0
    bytes: int = 0
    total_bytes: int | None = None
    statements: int = 0
    errors: int = 0
    line: int | None = None


@dataclass(frozen=True)
class JobReport:
    """How a job ended: its outcome, the error sent right before it (if any), and whether the profile's schema cache
    must be refreshed (DDL may have run)."""

    outcome: JobOutcomeEvent
    error: StatementError | None = None
    schema_changed: bool = False

    def sent_outcome(self) -> JobOutcomeEvent:
        """The job_outcome as sent: with `error` — the live `error` event's code and text, and where the run stopped
        when it did — so a window that attaches later, or the job list, knows why it stopped or failed (06a fix wave
        F3)."""
        if self.error is None:
            return self.outcome
        at = self.outcome.stopped_at
        error = JobError(line=at.line if at else None, statement=at.statement if at else None, code=self.error.code,
                         message=self.error.message)
        return self.outcome.model_copy(update={"error": error})

    def warned(self, text: str) -> "JobReport":
        """This report with one more of the outcome's warnings (a job-level warning also sent live, F3)."""
        return replace(self, outcome=self.outcome.model_copy(update={"warnings": [*self.outcome.warnings, text]}))


Runner = Callable[[str, RunOptions, Callable[[ServerMessage], None]], StatementResult]


@dataclass
class JobContext:
    """A job's view of its session (any method: the worker thread only)."""

    request_id: str
    kind: JobKind
    connection: DatabaseConnection  # catalog and dump methods are called on it directly, on this thread
    dialect: Dialect
    capabilities: AdapterCapabilities
    hooks: JobHooks
    _run: Runner
    _close: Callable[[StatementResult], None]
    _raise_if_cancelled: Callable[[], None]
    _send: Callable[[BaseModel], None]  # an event of the request (a message)
    _publish: Callable[[BaseModel], None]  # a job event: sent, kept for a replay, and given to the job list
    _stop_cancels: Callable[[], bool]
    _sent: Callable[[], bool]
    _clock: Callable[[], float] = time.monotonic
    progress: JobProgress = field(default_factory=JobProgress)
    record_id: int | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    _last_tick: float = -math.inf
    _messages: int = 0
    messages_dropped: int = 0

    @property
    def sent(self) -> bool:
        """Whether a statement of this job reached the connection (`run()` was called on it)."""
        return self._sent()

    def run(self, sql: str, options: RunOptions = NO_DESCRIBE) -> StatementResult:
        """One statement through the worker's path: refused text, the last cancel check and the armed window, then
        `run()`. Its live messages (SQL Server PRINT) go to `message`."""
        return self._run(sql, options, self.server_message)

    def drain(self, result: StatementResult) -> int | None:
        """Read `result` to its end without keeping a row, forward its messages and close it: the rows its sets
        affected (None when none said), for a statement whose answer is not shown (Run SQL file)."""
        affected: int | None = None
        try:
            while True:
                if result.columns() is None:
                    if (rows := result.affected_rows()) is not None:
                        affected = (affected or 0) + rows
                else:
                    while len(result.fetch(_DRAIN_CHUNK)) == _DRAIN_CHUNK:
                        self.raise_if_cancelled()
                self._forward(result)
                if not result.next_result():
                    return affected
        finally:
            self._close(result)

    def close(self, result: StatementResult) -> None:
        self._close(result)

    def raise_if_cancelled(self) -> None:
        """A Cancel that came while no driver call was there to stop: StatementCancelled."""
        self._raise_if_cancelled()

    def stop_cancels(self) -> bool:
        """From now on a Cancel never reaches the driver (before a COMMIT, ⑤ R-05-6). Whether one had been asked."""
        return self._stop_cancels()

    def tick(self, *, force: bool = False) -> None:
        """Send `progress` when PROGRESS_INTERVAL_S has passed since the last one, or now with `force` (an object
        starts or ends)."""
        now = self._clock()
        if force or now - self._last_tick >= PROGRESS_INTERVAL_S:
            self._last_tick = now
            self._publish(JobProgressEvent(request_id=self.request_id, **asdict(self.progress)))

    def message(self, severity: MessageSeverity, text: str) -> None:
        """A message in the job's Messages: the first MAX_JOB_MESSAGES are sent, the rest counted (D-06-11)."""
        if self._messages >= MAX_JOB_MESSAGES:
            self.messages_dropped += 1
            return
        self._messages += 1
        self._send(MessageEvent(request_id=self.request_id, severity=severity, text=text))

    def server_message(self, message: ServerMessage) -> None:
        self.message(message.severity, message.text)

    def begin_record(self, sql: str) -> None:
        """Write the job's History record as running (D-06-9); a History that fails never stops the job."""
        try:
            self.record_id = self.hooks.begin_record(sql, self.started_at)
        except Exception:
            logger.warning("The History record of job {} could not be written", self.request_id, exc_info=True)

    def outcome(self, outcome: JobOutcomeKind, **fields: Any) -> JobOutcomeEvent:
        """The job's outcome event, with a note on the server messages that were not shown."""
        warnings = list(fields.pop("warnings", []))
        if self.messages_dropped:
            warnings.append(f"{self.messages_dropped:,} more server messages were not shown")
        return JobOutcomeEvent(request_id=self.request_id, job=self.kind, outcome=outcome, warnings=warnings, **fields)

    def _forward(self, result: StatementResult) -> None:
        for message in result.take_messages():
            self.server_message(message)


class Job(ABC):
    """One job of one command. `run` classifies every way it ends (R-06-12) and never resends anything."""

    kind: ClassVar[JobKind]

    @property
    @abstractmethod
    def title(self) -> str:
        """What the job list calls it: `Run orders.sql on Local PG`."""

    @abstractmethod
    def run(self, ctx: JobContext) -> JobReport: ...

    def broke(self, ctx: JobContext, exc: Exception) -> JobReport:
        """An exception `run` did not classify (a bug, or an error it let through): nothing is known to have been
        applied when nothing was sent; otherwise the outcome is unknown."""
        error = exc if isinstance(exc, StatementError) else StatementError(f"Internal error: {exc}")
        outcome = ctx.outcome("unknown" if ctx.sent else "not_started", cancelled=isinstance(exc, StatementCancelled))
        return JobReport(outcome, error, schema_changed=ctx.sent)


def history_status(outcome: JobOutcomeEvent, error: StatementError | None) -> tuple[HistoryStatus, str | None]:
    """How a job's History record ends (D-06-9): ok, cancelled, or error with what happened in front of the error."""
    text = error.message if error is not None else None
    if outcome.cancelled:
        return "cancelled", text
    if outcome.outcome == "completed":
        skipped = f"{outcome.error_count:,} error(s) skipped" if outcome.error_count else None
        return "ok", skipped
    where = f" at line {outcome.stopped_at.line:,}" if outcome.stopped_at is not None else ""
    prefix = {
        "rolled_back": "Rolled back", "stopped": f"Stopped{where}", "partial": f"Partly applied{where}",
        "unknown": f"Outcome unknown{where}", "failed": "Failed", "not_started": "Not started",
    }[outcome.outcome]
    return "error", f"{prefix}: {text}" if text else prefix
