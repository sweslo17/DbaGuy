import itertools
import queue
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal, TypeVar

from loguru import logger
from pydantic import BaseModel

from ..adapters.base import (
    FAILED_COMMIT_MESSAGE,
    AdapterCapabilities,
    DatabaseConnection,
    RawJsonRows,
    ReadAhead,
    ReadAheadState,
    RunOptions,
    StatementCancelled,
    StatementError,
    StatementResult,
    invalid_text_error,
    transaction_open_error,
)
from ..adapters.values import PLAN_TEXT_LIMIT, TEXT_LIMIT, encode_row
from ..domain.classification import Classification, StatementKind
from ..domain.data_jobs import JobCommand, JobOutcomeEvent
from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable
from ..domain.errors import ConnectionFailed, SessionBusy
from ..domain.execution import (
    AffectedEvent,
    ApplyEditsCommand,
    CancelCommand,
    CommitCommand,
    ConfirmationRequiredEvent,
    DiscardCommand,
    EditOutcomeEvent,
    ErrorEvent,
    ExecuteCommand,
    ExportCommand,
    FetchMoreCommand,
    FinishedEvent,
    MessageEvent,
    MessageSeverity,
    ProgressEvent,
    QueryContext,
    ResultSetEndEvent,
    ResultSetEvent,
    RollbackCommand,
    RowsEvent,
    ServerMessage,
    SessionStateEvent,
    SetAutocommitCommand,
    StartedEvent,
    StatementParams,
)
from ..domain.formats import FormatColumn, FormatKind
from ..services.safety import GuardAction, GuardDecision
from ..sql.dialects import Dialect
from ..sql.formatting import (
    RAW_JSON_FORMATS,
    RawJsonWriter,
    RowWriter,
    open_raw_json_writer,
    open_writer,
    unsupported_reason,
)
from ..storage.atomic_file import atomic_text_file
from .edit_apply import (
    COMMIT_WARNING,
    CONNECTION_LOST,
    ROLLBACK_FAILED,
    ROW_COUNT_UNKNOWN,
    SQL_MODE_CHANGED,
    SQL_MODE_CHANGED_MESSAGE,
    EditProgress,
    EditReport,
    history_error,
    refused,
)
from .jobs.context import (
    JOB_ROLLBACK_FAILED, Job, JobContext, JobHooks, JobReport, PreparedJob, history_status, read_matched,
)
from .options import run_options
from .read_ahead import READ_AHEAD_POLL_S, STALL_RESET_FACTOR, StallClock
from .session_state import SessionStateTracker

T = TypeVar("T")
RequestCommand = TypeVar("RequestCommand", ExecuteCommand, ExportCommand, ApplyEditsCommand, JobCommand)
Emitter = Callable[[BaseModel], None]
TransactionCommand = CommitCommand | RollbackCommand | SetAutocommitCommand
_CHUNK = 200
_STOP = object()
_READ_AHEAD = object()  # what `_next_task` gives when no task is queued and the paused result should read ahead now
_EXPORT_CHUNK = 1000  # rows fetched, written and checked for a cancel at a time
_PROGRESS_EVERY = 10_000  # an export reports its row count each time it passes a multiple of this
_EXPORT_SELECT_ONLY = "Only a statement that returns rows (SELECT, WITH, VALUES…) can be exported"
# The warning of a queued request whose session closed before it ran. src/features/edits/commitFlow.ts matches it to say
# so for an apply (rather than "Cancelled"): change both together
CLOSED_BEFORE_START = "Session closed before the query started"
# A grid commit's texts (BEGIN, its DML, COMMIT) return no result set to show: nothing to describe (05a review M1). A
# trigger's SELECT still comes back as a result set, which has no count (row_count_unknown, ruling E9).
_EDIT_OPTIONS = RunOptions(describe=False)
_JOB_LOG_CHARS = 200  # a job's statement at DEBUG: its first characters only (D-06-12: a million-statement run)


def paused_result_closed_text(limit_s: float) -> str:
    """The warning a paused result gets when the worker closes it `limit_s` after the server was left waiting for it
    (the engine's limit, counted by the stall clock: from where the read-ahead stopped, or from the first pause for an
    engine without read-ahead; whole minutes as minutes, anything else in seconds: "2 min", "45 s"). A Load more need
    not restart that clock (R-04a-T11b, B42), so the text does not promise that loading more keeps the result open."""
    after = f"{limit_s / 60:g} min" if limit_s % 60 == 0 else f"{limit_s:g} s"
    return (f"Closed this result {after} after the server was left waiting for it, to free the query node — run the "
            "statement again to load more, or export it to get every row")


def _param_names(params: StatementParams) -> str:
    """`$name, $1, …`: what the log may say about parameters (never their values, spec §5)."""
    return ", ".join([*(f"${n}" for n in params.named), *(f"${i}" for i in range(1, len(params.positional) + 1))])


def _discard_event(_: BaseModel) -> None:
    return None


@dataclass(frozen=True)
class ExecutionRecord:
    """One execute that passed the policy, as the history keeps it."""

    request_id: str
    sql: str
    started_at: datetime
    elapsed_ms: int  # until the first pause or the end: reading time is not query time
    rows: int  # rows sent plus affected rows
    status: Literal["ok", "error", "cancelled", "timeout"]
    error: str | None = None
    query_context: QueryContext | None = None  # the request's own (engines with `capabilities.query_context`)


def _read_only_message(kind: StatementKind) -> str:
    return f"This connection is read-only: {kind} statements are not allowed"


def _run_everything(sql: str, confirmed: bool) -> GuardDecision:
    return GuardDecision(action=GuardAction.RUN, classification=Classification(kind=StatementKind.UNKNOWN))


def _check_text(sql: str) -> None:
    """Drivers cut the text at U+0000 (C strings): `DELETE FROM t<NUL> WHERE id = 1` would pass the safety check as
    written and reach the server without its WHERE. Refused before the check, for every engine."""
    if (index := sql.find("\x00")) >= 0:
        raise invalid_text_error(sql, index)


Interruption = Literal["cancel", "force", "abort"]  # a Cancel or a timeout; a repeated Cancel; closing the session


def _ignore(*_: object) -> None:
    return None


def _no_edit_plans(plan_id: str) -> EditPlan:
    raise PlanUnavailable(PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE)


@dataclass(frozen=True)
class WorkerHooks:
    """How a worker reaches the rest of the app. The defaults run everything and record nothing."""

    dialect: Dialect = Dialect.SQLITE
    check: Callable[[str, bool], GuardDecision] = _run_everything  # (sql, confirmed)
    on_record: Callable[[ExecutionRecord], None] = _ignore
    on_schema_change: Callable[[], None] = _ignore
    capabilities: AdapterCapabilities = AdapterCapabilities()  # which run() options the engine takes; idle limits
    # The previewed grid-edit plan with this id, taken once (spec ⑤ D-05-7); raises PlanUnavailable
    take_edit_plan: Callable[[str], EditPlan] = _no_edit_plans
    jobs: JobHooks = JobHooks()  # data tools' jobs (spec ⑥ D-06-2)
    # False: an execute's run() reads no rows ahead of fetch() and skips describing the columns first (an MCP agent's
    # session, spec ⑧ CR-08-7: a page is all it holds); RunOptions.describe
    describe: bool = True


@dataclass
class _Request:
    """One execute or export request, from `started` to `finished`."""

    request_id: str
    sql: str
    history: bool = True
    timeout_s: float | None = None
    started: float = field(default_factory=time.monotonic)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    kind: StatementKind = StatementKind.UNKNOWN
    ran: bool = False  # passed the policy and reached the connection
    rows: int = 0
    first_page_ms: int | None = None  # set at the first pause
    timer_armed: bool = False  # guarded by the worker's _state_lock
    timed_out: bool = False
    query_context: QueryContext | None = None  # what run() received, for the history

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


@dataclass(frozen=True)
class _PausedMark:
    """One pause of the open result: the idle close acts only while the mark it saw is still the current one (compared
    by value: a read-ahead step that moves `since` replaces the mark, so a close queued for the old one does nothing).
    """

    token: int  # tells two pauses of the same request apart
    request_id: str
    # The worker's clock where the stall began (the idle limit counts from it): where the read-ahead stopped, or the
    # first pause for an engine without read-ahead (R-04a-T11b). None: nothing holds the server back, or a pause that
    # reads on before its first step (which publishes the stall clock's `since`).
    since: float | None


@dataclass
class _OpenExecution:
    req: _Request
    result: StatementResult
    # When the server may be held back since: fed by every read-ahead step, or by every pause for an engine without
    # read-ahead (the clock then runs from the first pause, and a Load more does not restart it, R-04a-T11b)
    stall: StallClock
    index: int = 0
    sent: int = 0
    columns_sent: bool = False
    peeked: list[list[Any]] = field(default_factory=list)  # raw rows read ahead to detect a full page
    # The last read-ahead step's state (written under the worker's _state_lock); None: the engine does not read ahead
    ahead: ReadAheadState | None = None
    received: int = 0  # the last read-ahead step's `received`
    # The current set's longest cell text sent whole: PLAN_TEXT_LIMIT for a `plan` set, else TEXT_LIMIT. Set when its
    # ResultSetEvent goes out and kept here, since a Load more re-enters _pump with `columns_sent` already set (B37).
    text_limit: int = TEXT_LIMIT

    @property
    def request_id(self) -> str:
        return self.req.request_id


class SessionWorker:
    """Owns one connection and one thread; every driver call happens on that thread.

    Locking: `_queue_lock` guards `_closed` and every enqueue, so nothing can land behind the stop marker.
    `_state_lock` guards `_running_request`, `_cancel_requested`, `_queued`, `_paused`, `_Request.timer_armed` and the
    open execution's `ahead`; it is held across `connection.cancel()`, so a cancel can never hit a statement other than
    the one it was meant for (the worker changes the running request under it). `_tracker` has its own leaf lock for the
    session state that other threads read; `_state_send_lock` keeps a replayed state from overtaking a newer one, and
    `_job_send_lock` does the same for a job's replayed events (taken before `_emit_lock`, never inside it).

    Read-ahead (engines with `capabilities.read_ahead_bytes`): while the open result is paused, the worker thread steps
    `result.read_ahead()` between tasks, only when none is queued. A step never blocks and touches only the open result,
    so a queued task (Cancel, Discard, Load more, a new statement, close) waits at most one step; nothing is running
    meanwhile, so a cancel queues a discard and never reaches the connection. Each step moves the paused mark's `since`
    with the stall clock (`read_ahead.StallClock`), which the idle close counts from; a pause that reads on publishes no
    stall until its first step. A step that fails stops the read-ahead until the next pause.

    Every request ends with exactly one `finished`, and a `session_state` immediately before it.
    """

    def __init__(self, name: str, opener: Callable[[], Any], clock: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self._opener = opener
        self._clock = clock  # how long a result has been paused; when the last request finished
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=f"session-{name}", daemon=True)
        self._lease: Any = None
        self._open_error: BaseException | None = None
        self._opened = threading.Event()
        self._emit: Emitter = _discard_event
        self._emit_lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state_send_lock = threading.Lock()
        self._running_request: str | None = None
        # A cancel reached the running request; honoured right before run() (the connection is armed from just before
        # that check until run() takes over, `_run_statement`), as Load more starts and at the next page boundary, in
        # case no driver call was in flight to interrupt (the driver then has nothing to cancel and the request would
        # go on).
        self._cancel_requested = False
        self._cancels = 0  # Cancels (and a timeout) that reached the running request: a Cancel after one forces
        self._queued: dict[str, bool] = {}  # requests waiting in the queue -> cancelled before they started
        self._open_exec: _OpenExecution | None = None
        # The open result is paused waiting for Load more since then (set by _pause, cleared when it resumes or ends)
        self._paused: _PausedMark | None = None
        self._pause_tokens = itertools.count(1)  # worker thread only
        self._closed = False
        self._hooks = WorkerHooks()
        self._tracker = SessionStateTracker()
        self._ddl_pending = False  # worker thread only: DDL ran inside a transaction that has not ended yet
        self.last_activity = time.monotonic()
        # When the last request sent its `finished`, on `clock` (the reaper counts idle time from it)
        self.finished_at = 0.0
        # The last job_progress and job_outcome of this session's job, by type: sent again to a client that attaches
        # (D-06-10). Written by the worker thread, read by any (under _emit_lock)
        self._job_events: dict[str, BaseModel] = {}
        # A job event is kept and sent under it, and a client attaches under it: a replayed job event is never sent
        # live to the same client too, and never comes after a newer one (pre-flight M2)
        self._job_send_lock = threading.Lock()
        # A job runs, from its start until its last event went out (guarded by _state_lock). Past stop_cancels() no
        # request is "running", yet its COMMIT or rollback is: the reaper must not close it then (pre-flight L1)
        self._job_active = False
        self._job_request: str | None = None  # that job's request id (guarded by _state_lock)
        # A Cancel of that job while its statements could not be cancelled (a copy's COMMIT between two tables): kept
        # for `_resume`, so the next table does not start (06c; guarded by _state_lock)
        self._held_cancel = False

    # ---- lifecycle (any thread) ----

    def start(self, timeout: float = 30.0) -> None:
        self._thread.start()
        if not self._opened.wait(timeout):
            self._begin_close()  # the thread closes the connection itself as soon as the opener returns
            raise ConnectionFailed(f"Timed out opening a connection for {self.name}")
        if self._open_error is not None:
            raise self._open_error

    def configure(self, hooks: WorkerHooks) -> None:
        """Set before the session is registered, so no command can run with the defaults."""
        self._hooks = hooks

    @property
    def closed(self) -> bool:
        """It is closing or closed: nothing more runs on it (a request it ends now was ended by the close)."""
        return self._closed

    @property
    def dialect(self) -> Dialect:
        """The dialect its hooks read statements in: its engine's (an MCP agent's session checks what the lexer
        assumes of it by this, spec ⑧ pre-flight M1)."""
        return self._hooks.dialect

    @property
    def connection(self) -> DatabaseConnection:
        if self._lease is None:
            raise ConnectionFailed("Connection is not open")
        return self._lease.connection

    def set_emitter(self, emit: Emitter) -> None:
        with self._emit_lock:
            self._emit = emit

    def clear_emitter(self, emit: Emitter) -> None:
        with self._emit_lock:
            if self._emit is emit:
                self._emit = _discard_event

    def replay_state(self) -> None:
        """Send the last known `session_state` (with a fresh `idle_seconds`) to the current emitter."""
        with self._state_send_lock:
            state = self._tracker.snapshot()
            if state is not None:
                self._send(state)

    def attach_emitter(self, emit: Emitter) -> None:
        """Make `emit` the emitter, sending it first the last job_progress and job_outcome of this session's job, if
        any (D-06-10: a reloaded window finds where its job is). Under the job-send lock, a job event is either among
        those replayed or sent live after them, never both (pre-flight M2); and the replay goes out before `emit` gets
        any live event, so nothing the worker sends overtakes it. What is sent before `emit` is set reaches the
        previous emitter."""
        with self._job_send_lock:
            with self._emit_lock:
                events = list(self._job_events.values())
            for event in events:
                try:
                    emit(event)
                except Exception:
                    logger.exception("Failed to deliver event on {}", self.name)
            self.set_emitter(emit)

    def busy(self) -> bool:
        """A request runs or waits in the queue, or a job has not sent its last event yet: the session must not be
        reaped (D-06-10, pre-flight L1)."""
        with self._state_lock:
            return self._job_active or self._running_request is not None or bool(self._queued)

    def call(self, fn: Callable[[DatabaseConnection], T], timeout: float | None = 120.0, *,
             refresh_state: bool = False) -> T:
        """Run fn(connection) on the worker thread and wait for its result. `refresh_state`: fn may have begun or ended
        a transaction (an MCP agent's read frame, spec ⑧ D-08-3): the session's state is read again before the result
        is given, so the idle-transaction check never sees one that has ended."""
        future: Future[T] = Future()

        def task() -> None:
            if not future.set_running_or_notify_cancel():
                return
            if self._closed:
                future.set_exception(SessionBusy("Session is closed"))
                return
            failure: BaseException | None = None
            try:
                self._discard_open()
                result = fn(self.connection)
            except BaseException as exc:
                failure = exc
            if refresh_state:
                self._refresh_state()  # never raises: a connection that cannot tell has no transaction
            if failure is not None:
                future.set_exception(failure)
            else:
                future.set_result(result)

        self._put(task)
        return future.result(timeout)

    def submit(self, command: BaseModel) -> None:
        match command:
            case ExecuteCommand():
                self._submit_request(command, self._execute)
            case ExportCommand():
                self._submit_request(command, self._export)
            case ApplyEditsCommand():
                self._submit_request(command, self._apply_edits)
            case JobCommand():
                self._submit_request(command, self._run_job)
            case FetchMoreCommand():
                self._put(lambda: self._fetch_more(command))
            case DiscardCommand():
                self._put(lambda: self._discard(command.request_id))
            case CancelCommand():
                self.cancel(command.request_id)
            case CommitCommand() | RollbackCommand() | SetAutocommitCommand():
                self._put(lambda: self._transaction_command(command))

    def cancel(self, request_id: str) -> None:
        """Cancel a request: stop it if it is running, skip it if it is still queued, close it if it is paused."""
        with self._state_lock:
            if self._running_request == request_id:
                self._cancels += 1
                logger.info("Cancelling request {} on {}{}", request_id, self.name, " (again)" if self._cancels > 1 else "")
                self._interrupt_running("force" if self._cancels > 1 else "cancel")
                return
            if request_id in self._queued:
                logger.info("Request {} on {} cancelled before it started", request_id, self.name)
                self._queued[request_id] = True
                return
            if self._job_active and self._job_request == request_id:  # its COMMIT, say: the job reads it later
                logger.info("Cancel of job {} on {} came while its statements could not be cancelled: kept for its "
                            "next step, if any", request_id, self.name)
                self._held_cancel = True
                return
        try:
            self._put(lambda: self._discard(request_id, cancelled=True))  # a paused result, if it is the open one
        except SessionBusy:
            pass  # closing: the worker finishes the open result itself

    def withdraw(self, request_id: str) -> bool:
        """Skip a request that still waits in the queue, as `cancel()` does (it ends not_started and never reaches the
        driver); False once it has started, when nothing is done. A copy whose sink stopped withdraws its query tab's
        read that has not begun, where a cancel of a running one could abort the tab's transaction (pre-flight H2, 06c
        Task 3 review M-1)."""
        with self._state_lock:
            if request_id not in self._queued:
                return False
            logger.info("Request {} on {} withdrawn before it started", request_id, self.name)
            self._queued[request_id] = True
            return True

    def check_idle_transaction(self, warn_after_s: float, rollback_after_s: float) -> None:
        """Warn once per idle stretch of an open transaction, and roll it back once idle too long (0 disables)."""
        idle = self._tracker.idle_stretch()
        if idle is None:
            return
        if 0 < warn_after_s <= idle.seconds and self._tracker.claim_warning(idle.token):
            logger.info("Transaction on {} idle for {:.0f} s", self.name, idle.seconds)
            self._send(MessageEvent(
                request_id="", severity=MessageSeverity.WARNING,
                text=f"Transaction idle for {idle.seconds / 60:.0f} min — commit or roll back to release its locks",
            ))
        if 0 < rollback_after_s <= idle.seconds:
            try:
                self._put(lambda: self._idle_rollback(idle.token, rollback_after_s))
            except SessionBusy:
                pass  # closing: closing the connection ends the transaction anyway

    def check_paused_result(self) -> None:
        """Close the paused result once the engine's limit (capability `paused_result_max_idle_s`; Couchbase: a paused
        request holds a query servicer, R-04-6) has passed on the stall clock: since where its read-ahead stopped, or,
        for an engine without read-ahead, since the result's FIRST pause (a Load more does not restart it, R-04a-T11b;
        Couchbase ends a stalled request on its own 77–86 s after the stall began, and a small refill does not unstall
        it, A23). A result read to its end, or whose read-ahead never stopped, is never closed. Any thread; the close
        itself runs on the worker thread, and only if the result is still in the same pause with the same stall start by
        then (a Load more or a read-ahead step racing the close wins)."""
        limit = self._hooks.capabilities.paused_result_max_idle_s
        if limit is None:
            return
        with self._state_lock:
            mark = self._paused
        if mark is None or mark.since is None or self._clock() - mark.since < limit:
            return
        try:
            self._put(lambda: self._close_idle_paused(mark, limit))
        except SessionBusy:
            pass  # closing: the worker finishes the open result itself

    def paused_read_ahead(self) -> ReadAheadState | None:
        """The paused result's last read-ahead state (any thread). None when no result is paused or the engine does not
        read ahead."""
        with self._state_lock:  # one take: the mark, the open execution and its state agree (B31)
            mark, execution = self._paused, self._open_exec
            ahead = execution.ahead if execution is not None else None
        if mark is None or execution is None or mark.request_id != execution.request_id:
            return None
        return ahead

    def close(self) -> None:
        if not self._begin_close():
            return
        with self._state_lock:
            if self._running_request is not None and self._lease is not None:
                self._interrupt_running("abort")
        if self._thread.ident is not None:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                logger.warning("Session {} did not stop within 10s", self.name)

    def _begin_close(self) -> bool:
        """Reject new work and queue the stop marker. False when the worker is already closing."""
        with self._queue_lock:
            if self._closed:
                return False
            self._closed = True
            self._queue.put(_STOP)
        return True

    def _interrupt_running(self, how: Interruption = "cancel") -> None:
        """Stop the running request: flag it, for when no driver call is in flight, and interrupt the driver.
        The caller holds `_state_lock`; the connection's cancel methods return without waiting for the driver."""
        self._cancel_requested = True
        self._cancel_connection(how)

    def _cancel_connection(self, how: Interruption) -> None:
        connection = self.connection
        interrupt = {"cancel": connection.cancel, "force": connection.force_cancel, "abort": connection.abort}[how]
        try:
            interrupt()
        except Exception:
            logger.warning("Cancel ({}) failed on {}", how, self.name, exc_info=True)

    def _put(self, task: Callable[[], None]) -> None:
        with self._queue_lock:
            if self._closed:
                raise SessionBusy("Session is closed")
            self.last_activity = time.monotonic()
            self._queue.put(task)

    def _submit_request(self, cmd: RequestCommand, run: Callable[[RequestCommand], None]) -> None:
        """Queue a request that ends with its own `finished`; until it starts, `cancel()` only marks it."""
        with self._state_lock:
            self._queued[cmd.request_id] = False
        try:
            self._put(lambda: run(cmd))
        except BaseException:
            with self._state_lock:
                self._queued.pop(cmd.request_id, None)
            raise

    def _time_out(self, req: _Request) -> None:  # timer thread
        with self._state_lock:
            if not req.timer_armed or self._running_request != req.request_id:
                return  # paused or over: the timeout no longer applies
            logger.info("Request {} on {} timed out after {:g} s", req.request_id, self.name, req.timeout_s)
            req.timed_out = True
            self._cancels += 1  # a timeout is a first Cancel only, but the user's next Cancel then forces
            self._interrupt_running()

    # ---- worker thread ----

    def _run(self) -> None:
        try:
            self._lease = self._opener()
        except BaseException as exc:
            self._open_error = exc
            with self._queue_lock:
                self._closed = True  # nothing will ever run; reject new work and fail what was queued meanwhile
            self._opened.set()
            self._run_queued()
            return
        self._refresh_state()  # cached for replay_state(); start() returning guarantees it exists
        self._opened.set()
        self._run_queued()
        self._discard_open()
        try:
            self._lease.close()
        except Exception:
            logger.exception("Error closing the connection of {}", self.name)
        logger.info("Session {} closed", self.name)

    def _run_queued(self) -> None:
        """Run queued tasks until the stop marker; once closing, tasks only fail fast (see _execute / call). With no
        task queued, a paused result reads ahead one step at a time."""
        while True:
            task = self._next_task()
            if task is None or task is _STOP:
                return  # None: the opener failed, and only what was queued before the failure was left
            try:  # a step too: an error escaping it must not end this thread, or every later command would wait forever
                if task is _READ_AHEAD:
                    self._read_ahead_step()
                else:
                    task()
            except Exception:
                logger.exception("Session task failed on {}", self.name)
                if task is _READ_AHEAD:
                    self._stop_read_ahead()

    def _next_task(self) -> object:
        """The next queued task. While a paused result reads ahead the queue is never waited on for long: an empty
        queue gives `_READ_AHEAD` at once after READING, after READ_AHEAD_POLL_S after WAITING. None: the opener failed
        and nothing is left in the queue."""
        if self._lease is None:
            try:
                return self._queue.get_nowait()
            except queue.Empty:
                return None
        wait = self._read_ahead_wait()
        try:
            if wait is None:
                return self._queue.get()
            return self._queue.get(timeout=wait) if wait > 0 else self._queue.get_nowait()
        except queue.Empty:
            return _READ_AHEAD

    def _read_ahead_wait(self) -> float | None:
        """None: nothing reads ahead (block on the queue); else how long to wait for a task before the next step."""
        ex = self._open_exec
        paused = self._paused  # only this thread changes it
        if ex is None or paused is None or paused.request_id != ex.request_id:
            return None
        if ex.ahead is ReadAheadState.READING:
            return 0.0
        return READ_AHEAD_POLL_S if ex.ahead is ReadAheadState.WAITING else None

    def _read_ahead_step(self) -> None:
        """One read-ahead step of the paused open result (no task queued). A failing `read_ahead()` is a bug (adapters
        keep failures for fetch()): it counts as FULL with the last `received`, and the result waits for Load more."""
        ex = self._open_exec
        if ex is None:  # never: _read_ahead_wait() gives a step only for the paused open result
            return
        try:
            step = ex.result.read_ahead(self._hooks.capabilities.read_ahead_bytes)
        except Exception:
            logger.exception("Read-ahead of {} failed on {}; the result waits for Load more as it is",
                             ex.request_id, self.name)
            step = ReadAhead(ReadAheadState.FULL, ex.received)
        # B31: where this step stopped, read before anything is published; whoever then sees the new state also sees
        # the stall start it led to (publishing the state first would let a reader act on it before the clock is read)
        now = self._clock()
        ex.stall.update(step, now)
        with self._state_lock:
            if self._paused is not None and self._paused.request_id == ex.request_id:
                self._paused = replace(self._paused, since=ex.stall.since)
            previous, ex.ahead, ex.received = ex.ahead, step.state, step.received
        if step.state is previous:
            return
        if step.state is ReadAheadState.DONE:  # never read again: once per result
            logger.info("[{}] paused result {} read to its end ({} bytes)", self.name, ex.request_id, step.received)
        else:
            logger.debug("[{}] read-ahead of {}: {} ({} bytes read)", self.name, ex.request_id, step.state,
                         step.received)

    def _stop_read_ahead(self) -> None:
        """A read-ahead step failed in the worker's own code (a bug): step no more until the next pause, or it would
        fail again at once (a core spinning, a log full of tracebacks). As after a failing read_ahead() (FULL), the
        server is held back from here on: the mark keeps the stall start, or one starts now. Must never raise."""
        ex = self._open_exec
        if ex is None:
            return
        since = ex.stall.since if ex.stall.since is not None else self._clock()
        with self._state_lock:
            ex.ahead = ReadAheadState.FULL
            if self._paused is not None and self._paused.request_id == ex.request_id:
                self._paused = replace(self._paused, since=since)

    def _send(self, event: BaseModel) -> None:
        with self._emit_lock:
            emit = self._emit
        try:
            emit(event)
        except Exception:
            logger.exception("Failed to deliver event on {}", self.name)

    def _notify(self, what: str, hook: Callable[..., None], *args: object) -> None:
        """Call a hook; its failure is logged and never affects execution."""
        try:
            hook(*args)
        except Exception:
            logger.warning("The {} hook of {} failed", what, self.name, exc_info=True)

    def _set_running(self, request_id: str | None) -> bool:
        """Change the running request. Returns whether a cancel had been requested for the previous one."""
        with self._state_lock:
            cancel_requested = self._cancel_requested
            self._running_request = request_id
            self._cancel_requested = False
            self._cancels = 0
            if request_id is not None:  # a Load more ends the idle stretch; `_set_running(None)` after a pause must not
                self._paused = None
            self._tracker.activity(busy=request_id is not None)
        return cancel_requested

    def _clear_paused(self) -> None:
        """The open result ends or resumes: it is not waiting for Load more any more."""
        with self._state_lock:
            self._paused = None

    def _take_cancel_request(self) -> bool:
        """Whether a cancel reached the running request since it started (clears it)."""
        with self._state_lock:
            requested, self._cancel_requested = self._cancel_requested, False
        return requested

    def _begin_request(self, request_id: str) -> str | None:
        """Atomically leave the queue and become the running request. Returns why it must not run, if so."""
        with self._state_lock:
            cancelled = self._queued.pop(request_id, False)
            if cancelled:
                return "Query cancelled before it started"
            if self._closed:
                return CLOSED_BEFORE_START
            self._running_request = request_id
            self._cancel_requested = False
            self._cancels = 0
            self._paused = None  # the result a new request replaces has already been finished (_discard_open)
            self._tracker.activity(busy=True)
        return None

    def _start_request(
        self, req: _Request, *, before_finished: Callable[[str], BaseModel | None] | None = None,
    ) -> bool:
        """Leave the queue and announce `started`. When the request must not run, end it here — with the event
        `before_finished(why)` gives right before its `finished`, when given (None: it sent its own) — and return
        False."""
        skipped = self._begin_request(req.request_id)
        self._send(StartedEvent(request_id=req.request_id))
        if skipped is None:
            return True
        self._send(MessageEvent(request_id=req.request_id, severity=MessageSeverity.WARNING, text=skipped))
        if before_finished is not None and (event := before_finished(skipped)) is not None:
            self._send(event)
        self._send_finished(req.request_id, req.elapsed_ms(), cancelled=True)
        return False

    def _raise_if_cancelled(self) -> None:
        """Honour a cancel that reached the running request while no driver call was there to interrupt."""
        if self._take_cancel_request():
            raise StatementCancelled()

    def _execute(self, cmd: ExecuteCommand) -> None:
        self._discard_open()
        req = _Request(request_id=cmd.request_id, sql=cmd.sql, history=cmd.history, timeout_s=cmd.timeout_s or None)
        if not self._start_request(req):
            return
        try:
            _check_text(cmd.sql)
            options = run_options(cmd, self._hooks.capabilities)  # before the policy: a refused request never asks
            if not self._passes_policy(req, cmd.confirmed):
                return
            req.query_context = options.query_context
            # D-04-14: only a read is resent
            options = replace(options, retry_safe=req.kind == StatementKind.READ, describe=self._hooks.describe)
            with self._timeout(req):
                result = self._run_statement(req, "execute", cmd.sql, options)
                budget = self._hooks.capabilities.read_ahead_bytes
                stall = StallClock(None if budget is None else STALL_RESET_FACTOR * budget)
                self._open_exec = _OpenExecution(req=req, result=result, stall=stall)
                # R-04a-6b1: run() can return before the statement has done its work (PostgreSQL in a transaction:
                # DECLARE, and the first FETCH runs the query), and a cancel landing in between finds the backend idle.
                # A read run() returned from has delivered nothing yet, so it stops here. Never a write: one run()
                # returned from is done, and must not be reported cancelled.
                if req.kind == StatementKind.READ:
                    self._raise_if_cancelled()
                self._pump(self._open_exec, cmd.page_size)
        except StatementError as exc:
            self._fail(req, exc)
        except Exception as exc:
            logger.exception("Unexpected error executing on {}", self.name)
            self._fail(req, StatementError(f"Internal error: {exc}"))
        finally:
            self._set_running(None)

    def _run_statement(
        self, req: _Request, what: str, sql: str, options: RunOptions, *,
        listener: Callable[[ServerMessage], None] | None = None, log_chars: int = 2000,
    ) -> StatementResult:
        """The request's last cancel check, then run(). A cancel that lands between the two finds the driver idle (an
        adapter must ignore an idle cancel, R-03a-P4), so the statement would run to its end: the connection is armed
        before the check and keeps such a cancel, and run() then does not start (Task 6b). Disarmed on every way out,
        once run() has returned or raised; the result's own cancel handling takes over from there. `listener`: where
        live messages go instead of the request's Messages (a job counts them, D-06-11)."""
        connection = self.connection
        try:
            connection.arm_cancel()
            self._raise_if_cancelled()  # cancelled during the checks: the driver had nothing to interrupt
            self._log_statement(what, req.request_id, sql, options, log_chars)
            req.ran = True
            self._listen(req.request_id, listener)
            return connection.run(sql, options)
        finally:
            try:
                connection.disarm_cancel()
            except Exception:  # never lose the result (or the error) over it
                logger.warning("Could not disarm the cancel on {}", self.name, exc_info=True)

    def _log_statement(self, what: str, request_id: str, sql: str, options: RunOptions, chars: int = 2000) -> None:
        """The SQL text and the parameter names, at DEBUG; never parameter values (spec §5)."""
        logger.debug("[{}] {} {}: {}", self.name, what, request_id, sql[:chars])
        if options.params is not None:
            logger.debug("[{}] {} parameters: {}", self.name, request_id, _param_names(options.params))

    def _decide(self, req: _Request, confirmed: bool) -> GuardDecision:
        decision = self._hooks.check(req.sql, confirmed)
        req.kind = decision.classification.kind
        return decision

    def _passes_policy(self, req: _Request, confirmed: bool) -> bool:
        """Check the statement before it reaches the connection; when it may not run, end the request here."""
        decision = self._decide(req, confirmed)
        if decision.action == GuardAction.RUN:
            return True
        self._set_running(None)  # it never runs: from now on a cancel must not reach the connection
        if decision.action == GuardAction.BLOCK:
            logger.info("[{}] blocked {} statement {} (read-only)", self.name, req.kind, req.request_id)
            self._send(ErrorEvent(request_id=req.request_id, code="read_only", message=_read_only_message(req.kind)))
            self._send_finished(req.request_id, req.elapsed_ms())
        else:
            logger.info("[{}] {} needs confirmation: {}", self.name, req.request_id, ", ".join(decision.reasons))
            self._send(ConfirmationRequiredEvent(
                request_id=req.request_id, reasons=decision.reasons, classification=decision.classification,
            ))
            self._send_finished(req.request_id, req.elapsed_ms(), skipped=True)
        return False

    @contextmanager
    def _timeout(self, req: _Request) -> Iterator[None]:
        """Cancel `req` if it is still the running request after `req.timeout_s` (until its first pause or end)."""
        if not req.timeout_s:
            yield
            return
        timer = threading.Timer(req.timeout_s, self._time_out, args=(req,))
        timer.daemon = True
        with self._state_lock:
            req.timer_armed = True
        timer.start()
        try:
            yield
        finally:
            timer.cancel()
            with self._state_lock:  # a timer already firing must not cancel a later request with the same id
                req.timer_armed = False

    def _fetch_more(self, cmd: FetchMoreCommand) -> None:
        execution = self._open_exec
        if self._closed:
            return  # the open result is finished when the thread stops
        if execution is None or execution.request_id != cmd.request_id:
            self._send(ErrorEvent(request_id=cmd.request_id, message="No open result to fetch from", code="no_open_result"))
            # Every request ends with exactly one `finished`; without it a client waiting on this request would hang.
            self._send_finished(cmd.request_id, 0)
            return
        self._set_running(cmd.request_id)
        try:
            self._raise_if_cancelled()  # cancelled as Load more started: before the first driver call
            self._pump(execution, cmd.count)
        except StatementError as exc:
            self._fail(execution.req, exc)
        except Exception as exc:
            logger.exception("Unexpected error fetching on {}", self.name)
            self._fail(execution.req, StatementError(f"Internal error: {exc}"))
        finally:
            self._set_running(None)

    def _discard(self, request_id: str, cancelled: bool = False) -> None:
        execution = self._open_exec
        if execution is None or execution.request_id != request_id:
            return
        if cancelled:
            text = self._cancel_text(execution.req, "Query cancelled")
            self._send(MessageEvent(request_id=request_id, severity=MessageSeverity.WARNING, text=text))
        self._finish(execution, cancelled=cancelled)

    def _pause(self, ex: _OpenExecution) -> None:
        """A page is full and more rows exist: pause, unless a cancel arrived that the driver had nothing to stop."""
        if ex.req.first_page_ms is None:
            ex.req.first_page_ms = ex.req.elapsed_ms()
        # Leave the running state before any event: a cancel from now on discards instead of interrupting.
        if self._set_running(None):
            self._discard(ex.request_id, cancelled=True)
            return
        budget = self._hooks.capabilities.read_ahead_bytes
        if budget is None:  # no read-ahead: the server is held back from the first pause on (R-04a-T11b)
            ex.stall.update(ReadAhead(ReadAheadState.FULL), self._clock())
        reads_on = budget is not None and ex.ahead is not ReadAheadState.DONE
        with self._state_lock:  # before any event: the idle check may look at this pause now
            if reads_on:
                ex.ahead = ReadAheadState.READING  # read on while it waits (a Load more freed room); seen with the mark
            # A result that reads on gets its stall back from its first step, which re-evaluates it first (the Load more
            # may have drained enough to unblock the server, B44); until then nothing may close it on the old start.
            since = None if reads_on else ex.stall.since
            self._paused = _PausedMark(next(self._pause_tokens), ex.request_id, since)
        # In manual-commit mode the statement opened a transaction that a paused result keeps open: report it now,
        # not only at `finished`, so the client and the idle check see it.
        self._refresh_state(began_at=ex.req.started_at)
        self._send(ResultSetEndEvent(request_id=ex.request_id, index=ex.index, row_count=ex.sent, has_more=True))

    def _discard_open(self) -> None:
        if self._open_exec is not None:
            self._finish(self._open_exec)

    def _listen(self, request_id: str | None, listener: Callable[[ServerMessage], None] | None = None) -> None:
        """Route messages the connection delivers as they arrive to `request_id`'s Messages, or to `listener` when
        given (None: stop). Connections without live messages ignore it and keep using take_messages()."""
        if request_id is None:
            listener = None
        elif listener is None:
            listener = partial(self._send_live_message, request_id)
        try:
            self.connection.set_message_listener(listener)
        except Exception:  # a session whose connection is gone still ends its request
            logger.warning("Could not set the message listener on {}", self.name, exc_info=True)

    def _send_live_message(self, request_id: str, message: ServerMessage) -> None:
        """Send one server message to `request_id`'s Messages. A connection calls it, through its listener, on this
        worker thread from inside a driver call; `_send_messages` calls it for messages taken from the result."""
        self._send(MessageEvent(request_id=request_id, severity=message.severity, text=message.text))

    def _send_messages(self, ex: _OpenExecution) -> None:
        self._forward_messages(ex.request_id, ex.result)

    def _forward_messages(self, request_id: str, result: StatementResult) -> None:
        for message in result.take_messages():
            self._send_live_message(request_id, message)

    def _pump(self, ex: _OpenExecution, budget: int) -> None:
        result = ex.result
        while True:
            columns = result.columns()
            if columns is None:
                affected = result.affected_rows()
                if affected is not None:
                    ex.req.rows += affected
                    self._send(AffectedEvent(request_id=ex.request_id, index=ex.index, rows=affected))
            else:
                if not ex.columns_sent:
                    presentation = result.presentation()
                    plan = presentation == "plan"
                    ex.text_limit = PLAN_TEXT_LIMIT if plan else TEXT_LIMIT  # a plan is drawn whole
                    if plan:  # B45: never paused before its first row (a profile after a page the rows filled)
                        budget = max(budget, 1)
                    self._send(ResultSetEvent(
                        request_id=ex.request_id, index=ex.index, columns=columns, presentation=presentation,
                    ))
                    ex.columns_sent = True
                while budget > 0:
                    want = min(budget, _CHUNK)
                    rows, ex.peeked = ex.peeked[:want], ex.peeked[want:]
                    if len(rows) < want:
                        try:
                            rows += result.fetch(want - len(rows))
                        except StatementCancelled:
                            raise  # a cancel ends the result as it always did: nothing more is shown
                        except StatementError:
                            self._send_rows(ex, rows)  # the peeked rows in hand go out before the error (fix round 2)
                            raise
                    budget -= self._send_rows(ex, rows)
                    self._send_messages(ex)
                    if len(rows) < want:
                        break  # result set exhausted
                else:
                    if not ex.peeked:
                        ex.peeked = result.fetch(1)
                    if ex.peeked:  # page full and more rows exist
                        self._pause(ex)
                        return
                self._send(ResultSetEndEvent(request_id=ex.request_id, index=ex.index, row_count=ex.sent, has_more=False))
            self._send_messages(ex)
            if not result.next_result():
                break
            ex.index += 1
            ex.sent = 0
            ex.columns_sent = False
            ex.peeked = []
        self._finish(ex)

    def _send_rows(self, ex: _OpenExecution, rows: list) -> int:
        """Send `rows` of the current result set (none: nothing); how many."""
        if rows:
            cells = [encode_row(r, ex.text_limit) for r in rows]
            self._send(RowsEvent(request_id=ex.request_id, index=ex.index, rows=cells))
            ex.sent += len(rows)
            ex.req.rows += len(rows)
        return len(rows)

    def _export(self, cmd: ExportCommand) -> None:
        self._discard_open()  # one connection: a result the grid left paused ends before the export runs
        req = _Request(request_id=cmd.request_id, sql=cmd.sql, history=False)
        if not self._start_request(req):
            return
        try:
            _check_text(cmd.sql)
            options = replace(run_options(cmd, self._hooks.capabilities), retry_safe=True)  # an export is always a read
            target = self._check_export(cmd, req)
            logger.info("[{}] export {} as {} to {}", self.name, cmd.request_id, cmd.format, target)
            result = self._run_statement(req, "export", cmd.sql, options)
            written = self._write_export(cmd, target, req, result)
        except StatementError as exc:
            self._fail_export(req, cmd, exc)
        except OSError as exc:
            error = StatementError(f"Cannot write {cmd.path}: {exc.strerror or exc}", code="export_io")
            self._fail_export(req, cmd, error)
        except Exception as exc:
            logger.exception("Unexpected error exporting on {}", self.name)
            self._fail(req, StatementError(f"Internal error: {exc}"))
        else:
            logger.info("[{}] exported {:,} rows to {} in {} ms", self.name, written, target, req.elapsed_ms())
            text = f"Exported {written:,} rows to {target}"
            self._send(MessageEvent(request_id=cmd.request_id, severity=MessageSeverity.INFO, text=text))
            self._complete(req)
        finally:
            self._set_running(None)

    def _fail_export(self, req: _Request, cmd: ExportCommand, exc: StatementError) -> None:
        if isinstance(exc, StatementCancelled):
            logger.info("[{}] export {} to {} cancelled", self.name, cmd.request_id, cmd.path)
        else:  # the code above DEBUG: the server's text can echo the statement's literals (spec §5)
            logger.warning("[{}] export {} to {} failed ({})", self.name, cmd.request_id, cmd.path,
                           exc.code or type(exc).__name__)
            logger.debug("[{}] export {} failed: {}", self.name, cmd.request_id, exc.message)
        self._fail(req, exc)

    def _check_export(self, cmd: ExportCommand, req: _Request) -> Path:
        """Reject what an export cannot do, before anything runs or is written. Returns the file to write."""
        if (reason := unsupported_reason(cmd.format, self._hooks.dialect)) is not None:  # e.g. SQL++ INSERT
            raise StatementError(reason, code="unsupported_format")
        if cmd.format == FormatKind.IN_LIST:
            raise StatementError("An IN list cannot be exported; copy it from the grid", code="unsupported_format")
        target = Path(cmd.path).expanduser()
        if not target.is_absolute() or target.is_dir() or not target.parent.is_dir():
            raise StatementError(f"Choose a file in an existing folder to export into: {cmd.path}", code="invalid_path")
        if self._decide(req, False).action == GuardAction.BLOCK:
            raise StatementError(_read_only_message(req.kind), code="read_only")
        if req.kind != StatementKind.READ:
            raise StatementError(_EXPORT_SELECT_ONLY, code="export_requires_select")
        return target

    def _write_export(self, cmd: ExportCommand, target: Path, req: _Request, result: StatementResult) -> int:
        """Write the statement's whole result, raw values, into `target`, then close the result. Returns the rows
        written.

        A failed or cancelled export removes its partial file instead of finishing the document, so the writer's
        `close()` runs only when every row is in.
        """
        try:
            self._raise_if_cancelled()  # as in _execute (R-04a-6b1): an export is always a read
            columns = result.columns()
            if columns is None:
                raise StatementError(_EXPORT_SELECT_ONLY, code="export_requires_select")
            written = 0
            with atomic_text_file(target) as fp:
                writer: RowWriter | RawJsonWriter
                fetch: Callable[[int], list[Any]]
                if isinstance(result, RawJsonRows) and cmd.format in RAW_JSON_FORMATS:
                    # Couchbase JSON / JSONL: the server's row text, never re-encoded (R-04-15)
                    writer, fetch = open_raw_json_writer(cmd.format, fp), result.fetch_raw
                else:
                    writer = open_writer(
                        cmd.format, fp, [FormatColumn(name=c.name, category=c.category) for c in columns],
                        cmd.options, self._hooks.dialect,
                    )
                    fetch = result.fetch
                while True:
                    rows = fetch(_EXPORT_CHUNK)
                    self._raise_if_cancelled()
                    if not rows:
                        break
                    writer.write(rows)
                    before, written = written, written + len(rows)
                    req.rows = written
                    if written // _PROGRESS_EVERY > before // _PROGRESS_EVERY:
                        self._send(ProgressEvent(request_id=cmd.request_id, rows=written))
                writer.close()
            return written
        finally:
            # Before `finished` goes out and the result closes: from now on a cancel must not reach the connection.
            self._set_running(None)
            self._close_result(result)

    def _apply_edits(self, cmd: ApplyEditsCommand) -> None:
        """Apply a previewed plan (spec ⑤ §3.5): its texts and nothing else, in one explicit transaction on this
        session, every statement checked to match what it must (R-05-5), the outcome classified once (`EditProgress`)
        and never sent again. Events: started → affected × n → [error] → edit_outcome → session_state → finished;
        every apply ends with one edit_outcome, a refused or skipped one too (ruling E1)."""
        self._discard_open()  # the grid's paused result ends first (R-05-4); its own `finished` goes out as usual
        req = _Request(request_id=cmd.request_id, sql="", kind=StatementKind.WRITE)
        skipped = EditOutcomeEvent(request_id=req.request_id, outcome="rolled_back")
        if not self._start_request(req, before_finished=lambda _: skipped):
            return
        plan: EditPlan | None = None
        progress: EditProgress | None = None
        try:
            plan = self._take_plan(cmd.plan_id)
            req.sql = plan.script  # what History keeps (D-05-9)
            progress = EditProgress(plan.statements, plan.transactional, dialect=self._hooks.dialect)
            self._check_plan(plan)
            report = self._run_plan(req, plan, progress)
        except StatementError as exc:  # refused before anything was sent: _run_plan classifies every stop of its own
            report = refused(exc)
        except Exception as exc:
            logger.exception("Unexpected error applying edits on {}", self.name)
            if req.ran and not (progress is not None and progress.committing):
                # release what the BEGIN opened (a table that cannot roll back too); the outcome stays as broke() says
                self._rollback_quietly(req.request_id)
            report = (progress if progress is not None else EditProgress((), True)).broke(exc)
        finally:
            self._set_running(None)
        self._end_apply(req, plan, report)

    def _take_plan(self, plan_id: str) -> EditPlan:
        try:
            return self._hooks.take_edit_plan(plan_id)
        except PlanUnavailable as exc:
            raise StatementError(exc.message, code=exc.code) from None

    def _check_plan(self, plan: EditPlan) -> None:
        """Before anything is sent (spec §3.5 steps 3–4): every text can reach the driver whole, the policy passes every
        DML statement (a profile made read-only since the preview is refused here, and the server refuses again,
        R-05-7), and no transaction is open (R-05-4)."""
        for sql in (plan.begin, *(statement.sql for statement in plan.statements), plan.commit):
            _check_text(sql)
        for statement in plan.statements:
            decision = self._hooks.check(statement.sql, True)
            if decision.action == GuardAction.BLOCK:
                logger.info("[{}] blocked an apply of {} statement(s) (read-only)", self.name, len(plan.statements))
                raise StatementError(_read_only_message(decision.classification.kind), code="read_only")
        if self.connection.in_transaction():
            raise transaction_open_error()

    def _run_plan(self, req: _Request, plan: EditPlan, progress: EditProgress) -> EditReport:
        """BEGIN, each statement, the checks before COMMIT, COMMIT. Every stop from the BEGIN on is classified here
        (never by the caller's "refused"), so nothing that went out reads as if it had not."""
        rid = req.request_id
        try:
            self._edit_statement(req, plan.begin)
            self._check_literals(plan)
        except StatementError as exc:
            # A BEGIN that failed opened nothing; one whose answer was lost died with its link; one refused by the
            # check after it sent nothing else
            self._rollback_quietly(rid)
            return refused(exc)
        progress.sent = True
        for index, statement in enumerate(plan.statements):
            progress.index, progress.reached = index, False
            try:
                self._raise_if_cancelled()  # a Cancel between statements: this one was never sent
                progress.reached = True
                rows = self._edit_statement(req, statement.sql)
                if rows is not None and rows >= 0:
                    req.rows += rows
                    self._send(AffectedEvent(request_id=rid, index=index, rows=rows))
                if (mismatch := progress.matched(rows)) is not None:
                    logger.info("[{}] apply {}: statement {} ({}) matched {} row(s)", self.name, rid, index,
                                statement.kind, rows)
                    raise mismatch
            except StatementError as exc:
                self._rollback_quietly(rid)
                return progress.failed(exc)
        cancelled = self._set_running(None)  # from here a Cancel never reaches the driver (R-05-6)
        if plan.transactional:  # ruling E11: a table that cannot roll back has every statement applied already
            if (stopped := self._before_commit(rid, progress, cancelled)) is not None:
                return stopped
        progress.committing = True
        try:
            self._edit_statement(req, plan.commit)
        except StatementError as exc:
            if exc.code != CONNECTION_LOST:
                self._rollback_quietly(rid)  # SQLite keeps the transaction after a busy COMMIT (A14); a no-op elsewhere
            return progress.commit_failed(exc)
        return self._after_commit(rid, progress)

    def _check_literals(self, plan: EditPlan) -> None:
        """Right after BEGIN, before any DML: this session reads a backslash as the plan's literals were written for
        (D-05-13; 05a review M2). The plan has that from the metadata connection; BEGIN's own answer says this
        session's (MySQL's OK packet, no extra round trip). An engine whose answers do not say passes."""
        session = self.connection.backslash_escapes()
        if session is not None and session != plan.backslash_escapes:
            logger.info("[{}] refused an apply: the session's backslash escapes are {}, the plan's {}", self.name,
                        session, plan.backslash_escapes)
            raise StatementError(SQL_MODE_CHANGED_MESSAGE, code=SQL_MODE_CHANGED)

    def _before_commit(self, request_id: str, progress: EditProgress, cancelled: bool) -> EditReport | None:
        """Spec §3.5 step 7 for a transactional target (ruling E12): what stops the COMMIT, or None to send it. Nothing
        is committed yet, so every stop rolls back, but a transaction that vanished has an unknown fate. A state that
        cannot be read stops it too, rolled back (pre-flight L8)."""
        connection = self.connection
        try:
            if cancelled:
                report = progress.cancelled_before_commit()
            elif connection.transaction_failed():
                report = progress.aborted_before_commit()
            elif not connection.in_transaction():
                return progress.ended_before_commit()
            else:
                return None
        except StatementError as exc:
            report = refused(exc)
        self._rollback_quietly(request_id)
        return report

    def _after_commit(self, request_id: str, progress: EditProgress) -> EditReport:
        """COMMIT returned: it committed only if no transaction is left open (pre-flight L7). One still open is rolled
        back, so the tab is not left holding it; `commit_unconfirmed` says what that means for the outcome."""
        try:
            left_open = self.connection.in_transaction()
        except StatementError as exc:  # the connection cannot say
            return progress.commit_unconfirmed(exc)
        if not left_open:
            return progress.committed()
        self._rollback_quietly(request_id)
        return progress.commit_unconfirmed()

    def _edit_statement(self, req: _Request, sql: str) -> int | None:
        """Run one text of the plan: the rows it matched are read first, then the rest of its answer to the end, so
        SQL Server never has to stop it with an attention (ruling E9); its messages (a trigger's NOTICE or PRINT) go
        to the request."""
        result = self._run_statement(req, "edit", sql, _EDIT_OPTIONS)
        try:
            return read_matched(result, partial(self._forward_messages, req.request_id))
        finally:
            self._close_result(result)

    def _rollback_quietly(
        self, request_id: str, what: str = "apply", failed_text: str = ROLLBACK_FAILED,
    ) -> str | None:
        """End the apply's (or a broken job's) transaction once it stopped. No Cancel may reach the ROLLBACK, so the
        request stops running first. A connection that is gone cannot roll back: the server does it as the connection
        ends (R-05-6). One that is still there and still in the transaction gets `failed_text` as a warning: a
        table-data tab has no Rollback (pre-flight L8), and a job's dialog none either. Returns the warning sent."""
        self._set_running(None)
        try:
            self.connection.rollback()
            return None
        except Exception as exc:
            logger.info("[{}] the rollback after a stopped {} failed ({})", self.name, what,
                        getattr(exc, "code", None) or type(exc).__name__)
        try:
            still_open = self.connection.in_transaction()
        except Exception:
            return None  # it cannot tell: as for a connection that is gone
        if not still_open:
            return None
        logger.warning("[{}] {} {}: its transaction is still open after the rollback failed", self.name, what,
                       request_id)
        self._send(MessageEvent(request_id=request_id, severity=MessageSeverity.WARNING, text=failed_text))
        return failed_text

    def _end_apply(self, req: _Request, plan: EditPlan | None, report: EditReport) -> None:
        """The apply's last events: its error (for a commit that stood, a warning instead), the edit_outcome, then the
        History record, session_state and finished (`_complete`)."""
        rid, error = req.request_id, report.error
        if error is not None and report.outcome == "committed":
            text = f"{COMMIT_WARNING}: {error.message}"
            self._send(MessageEvent(request_id=rid, severity=MessageSeverity.WARNING, text=text))
        elif error is not None:
            self._send(ErrorEvent(request_id=rid, message=error.message, code=error.code, position=error.position))
        self._send(EditOutcomeEvent(
            request_id=rid, outcome=report.outcome, applied=report.applied, already_gone=report.already_gone,
            failed_change=report.failed_change, statement=report.statement,
        ))
        statements = plan.statements if plan is not None else ()
        kinds = Counter(statement.kind for statement in statements)  # counts only: never a key or a value (spec §5)
        warn = report.outcome in ("unknown", "partial") or (error is not None and error.code == ROW_COUNT_UNKNOWN)
        logger.log("WARNING" if warn else "INFO",
                   "[{}] apply {}: {} delete(s), {} update(s), {} insert(s) on {} → {} in {} ms{}", self.name, rid,
                   kinds["delete"], kinds["update"], kinds["insert"], plan.table if plan is not None else "-",
                   report.outcome, req.elapsed_ms(), f" ({error.code})" if error is not None and error.code else "")
        cancelled = report.outcome == "rolled_back" and isinstance(error, StatementCancelled)
        self._complete(req, error=history_error(report, len(statements)), cancelled=cancelled)

    def _run_job(self, cmd: JobCommand) -> None:
        """Every data-tools job (spec ⑥ D-06-2): the job object does its own work through a JobContext, this does
        the rest. Events: started → job_progress × n → [message] → [error] → job_outcome → session_state → finished;
        a job cancelled while queued, or whose session closed first, still gets its job_outcome (not_started). The
        session is busy until the job's last event went out, through its COMMIT too (pre-flight L1)."""
        with self._state_lock:
            self._job_active, self._job_request, self._held_cancel = True, cmd.request_id, False
        try:
            self._job(cmd)
        finally:
            with self._state_lock:
                self._job_active, self._job_request, self._held_cancel = False, None, False

    def _job(self, cmd: JobCommand) -> None:
        """`_run_job`'s request, from `started` to `finished`."""
        self._discard_open()  # one connection: a paused result ends first
        req = _Request(request_id=cmd.request_id, sql="", history=False)
        kind = cmd.job_kind
        with self._emit_lock:
            self._job_events = {}

        def not_started(why: str) -> None:  # right before the `finished` _start_request sends
            self._never_ran(cmd, StatementError(why, code="not_started"))
            self._job_event(JobOutcomeEvent(  # `why` is the warning sent live, kept for a later window too (F3)
                request_id=req.request_id, job=kind, outcome="not_started", cancelled=why != CLOSED_BEFORE_START,
                warnings=[why],
            ))

        if not self._start_request(req, before_finished=not_started):
            return
        job: Job | None = None
        ctx: JobContext | None = None
        try:
            job = self._hooks.jobs.build(cmd)
            ctx = JobContext(
                request_id=req.request_id, kind=kind, connection=self.connection, dialect=self._hooks.dialect,
                capabilities=self._hooks.capabilities, hooks=self._hooks.jobs, _run=partial(self._job_statement, req),
                _close=self._close_result, _raise_if_cancelled=self._raise_if_cancelled, _send=self._send,
                _publish=self._job_event,
                _stop_cancels=lambda: self._set_running(None), _sent=lambda: req.ran,
                _resume_cancels=lambda: self._resume(req.request_id),
            )
            if not job.helper:  # a copy's source is its copy's part: never listed, run in the session's transaction
                self._notify("job list", self._hooks.jobs.announce, req.request_id, kind, job.title,
                             job.source_profile_id)
                if self.connection.in_transaction():  # a job runs on a session of its own: never inside a user's
                    raise transaction_open_error()
            report = job.run(ctx)
        except Exception as exc:
            cancelled = isinstance(exc, StatementCancelled)  # a flag on whatever the outcome is (spec ⑥ §3.2)
            if job is None or ctx is None or not req.ran:
                if not isinstance(exc, StatementError):
                    logger.exception("Unexpected error starting a job on {}", self.name)
                error = exc if isinstance(exc, StatementError) else StatementError(f"Internal error: {exc}")
                report = JobReport(JobOutcomeEvent(
                    request_id=req.request_id, job=kind, outcome="not_started", cancelled=cancelled,
                ), error)
                # Refused before its run (no job, or the session is in a transaction), or its run raised before its
                # first statement: either way nothing was sent, and its maker hears it (06c Task 3 review M-5)
                self._never_ran(cmd, error)
            else:
                self._log_unclassified(req.request_id, exc)
                report = self._broke(job, ctx, exc)
                if cancelled and not report.outcome.cancelled:
                    report = replace(report, outcome=report.outcome.model_copy(update={"cancelled": True}))
                # A helper's transaction is its session's (a query tab's): never rolled back for it
                if not job.helper and (said := self._release_broken_job(req.request_id)) is not None:
                    report = report.warned(said)
        finally:
            self._set_running(None)
        self._end_job(req, ctx, report)

    def _never_ran(self, cmd: JobCommand, error: StatementError) -> None:
        """A job its maker built that ends without sending anything: its maker may be waiting on it (a copy's sink on
        its pipe, 06c pre-flight M1), so it hears why."""
        if isinstance(cmd, PreparedJob):
            self._notify("prepared job", cmd.job.not_run, error)

    def _resume(self, request_id: str) -> bool:
        """The job's statements can be cancelled again (a copy's next table, 06c): it is the running request once more,
        and a Cancel held since `stop_cancels()` is honoured at its next check. Whether one was held — or the session
        began closing meanwhile: its close interrupts only a running request, so the next table must not start
        either."""
        with self._state_lock:
            stop, self._held_cancel = self._held_cancel or self._closed, False
            # A job that never stopped its cancels is still the running request: a Cancel that reached it and found no
            # driver call to stop is kept, and so is the count that makes a second Cancel force (06c Task 3 review M-2)
            if self._running_request != request_id:
                self._cancels = 0
            self._running_request = request_id
            self._cancel_requested = self._cancel_requested or stop
            self._tracker.activity(busy=True)
        return stop

    def _log_unclassified(self, request_id: str, exc: Exception) -> None:
        """An exception a job's run() let through once something was sent. A StatementError's text may echo literals:
        above DEBUG only its code (D-06-12); anything else is a bug, with its traceback."""
        if isinstance(exc, StatementCancelled):
            logger.info("[{}] job {} was cancelled where it did not expect it", self.name, request_id)
        elif isinstance(exc, StatementError):
            logger.warning("[{}] job {} stopped on an error it did not classify ({})", self.name, request_id,
                           exc.code or type(exc).__name__)
            logger.debug("[{}] job {}: {}", self.name, request_id, exc.message)
        else:
            logger.exception("Unexpected error in job {} on {}", request_id, self.name)

    def _broke(self, job: Job, ctx: JobContext, exc: Exception) -> JobReport:
        """`job.broke()`; when an override of it fails too, the framework's own (the outcome is still exactly one)."""
        try:
            return job.broke(ctx, exc)
        except Exception:
            logger.exception("The broke() of job {} on {} failed: the framework's outcome instead", ctx.request_id,
                             self.name)
            return Job.broke(job, ctx, exc)

    def _release_broken_job(self, request_id: str) -> str | None:
        """A job that broke may have left its transaction open (an all-or-nothing run's BEGIN), holding its locks
        until the session closes: roll it back (pre-flight L2). The outcome stays what `broke()` said; the warning
        sent when the rollback failed is returned for its warnings too (F3)."""
        try:
            open_now = self.connection.in_transaction()
        except Exception:
            return None  # it cannot tell (the connection is gone): the server ends the transaction with it
        if open_now:
            logger.warning("[{}] job {} broke inside a transaction: rolling it back", self.name, request_id)
            return self._rollback_quietly(request_id, "job", JOB_ROLLBACK_FAILED)
        return None

    def _job_statement(
        self, req: _Request, sql: str, options: RunOptions, listener: Callable[[ServerMessage], None],
    ) -> StatementResult:
        """A job's statement (JobContext.run): refused text first (U+0000), then the worker's path."""
        _check_text(sql)
        return self._run_statement(req, "job", sql, options, listener=listener, log_chars=_JOB_LOG_CHARS)

    def _job_event(self, event: BaseModel) -> None:
        """A job's progress or outcome: kept for a client that attaches later, given to the job list, and sent; as one
        step under the job-send lock, so attach_emitter() finds it either kept and sent, or neither (pre-flight M2)."""
        with self._job_send_lock:
            self._keep_job_event(event)
            self._send(event)

    def _keep_job_event(self, event: BaseModel) -> None:
        with self._emit_lock:
            self._job_events[getattr(event, "type", "")] = event
        self._notify("job list", self._hooks.jobs.report, event)

    def _end_job(self, req: _Request, ctx: JobContext | None, report: JobReport) -> None:
        """The job's last events: its error, the job_outcome, then its History record, the schema cache, the
        session_state and `finished`."""
        rid, outcome, error = req.request_id, report.sent_outcome(), report.error
        if error is not None:
            self._send(ErrorEvent(request_id=rid, message=error.message, code=error.code, position=error.position))
        self._job_event(outcome)  # with the error, for a later window and the job list (F3)
        if ctx is not None and ctx.record_id is not None:
            status, text = history_status(outcome, error)
            self._notify("history", self._hooks.jobs.finish_record, ctx.record_id, status, req.elapsed_ms(),
                         outcome.rows, text)
        if report.schema_changed:
            self._notify("schema change", self._hooks.on_schema_change)
        warn = outcome.outcome in ("partial", "unknown")
        logger.log("WARNING" if warn else "INFO",
                   "[{}] job {} ({}) → {}{} in {} ms: {:,} statement(s), {:,} row(s), {:,} bytes{}", self.name, rid,
                   outcome.job, outcome.outcome, " (cancelled)" if outcome.cancelled else "", req.elapsed_ms(),
                   outcome.statements, outcome.rows, outcome.bytes,
                   f" ({error.code})" if error is not None and error.code else "")
        self._listen(None)
        self._send_finished(rid, req.elapsed_ms(), cancelled=outcome.cancelled)

    def _transaction_command(self, cmd: TransactionCommand) -> None:
        self._discard_open()
        started = time.monotonic()
        self._send(StartedEvent(request_id=cmd.request_id))
        if self._closed:
            text = "Session closed before the command ran"
            self._send(MessageEvent(request_id=cmd.request_id, severity=MessageSeverity.WARNING, text=text))
            self._send_finished(cmd.request_id, 0, cancelled=True)
            return
        self._tracker.activity(busy=True)  # a slow COMMIT is not an idle transaction
        try:
            self._run_transaction_command(cmd)
        except StatementError as exc:
            logger.info("[{}] {} failed ({})", self.name, cmd.type, exc.code)
            self._send(ErrorEvent(request_id=cmd.request_id, message=exc.message, code=exc.code, position=exc.position))
        except Exception as exc:
            logger.exception("Unexpected error in {} on {}", cmd.type, self.name)
            self._send(ErrorEvent(request_id=cmd.request_id, message=f"Internal error: {exc}"))
        finally:
            self._tracker.activity(busy=False)
        self._send_finished(cmd.request_id, int((time.monotonic() - started) * 1000))

    def _run_transaction_command(self, cmd: TransactionCommand) -> None:
        connection = self.connection
        match cmd:
            case CommitCommand():
                failed = connection.transaction_failed()  # PostgreSQL: COMMIT of an aborted transaction rolls back
                connection.commit()
                if failed:
                    logger.info("[{}] commit rolled back an aborted transaction", self.name)
                    self._send(MessageEvent(
                        request_id=cmd.request_id, severity=MessageSeverity.WARNING, text=FAILED_COMMIT_MESSAGE,
                    ))
                    return
                logger.info("[{}] commit", self.name)
            case RollbackCommand():
                connection.rollback()
                logger.info("[{}] rollback", self.name)
            case SetAutocommitCommand():
                connection.set_autocommit(cmd.enabled)
                logger.info("[{}] auto-commit {}", self.name, "on" if cmd.enabled else "off")

    def _idle_rollback(self, token: int, after_s: float) -> None:
        # Compare before discarding: finishing a paused result is activity too, but the rollback must still happen.
        if self._closed or not self._tracker.unchanged_since(token):
            return  # something ran meanwhile (or the session is closing): the transaction is not idle any more
        self._discard_open()  # a paused result holds the transaction open
        try:
            self.connection.rollback()
        except Exception as exc:  # e.g. the connection is gone
            if isinstance(exc, StatementError):
                logger.warning("Idle rollback failed on {} ({})", self.name, exc.code or type(exc).__name__)
                logger.debug("Idle rollback failed on {}: {}", self.name, exc.message)  # the server's text: DEBUG only
                reason = exc.message
            else:
                logger.exception("Idle rollback failed on {}", self.name)
                reason = f"Internal error: {exc}"
            # Report what the connection says now: a dead one has no transaction, so the idle check stops retrying.
            self._refresh_state()
            self._send(MessageEvent(
                request_id="", severity=MessageSeverity.WARNING,
                text=f"Could not roll back the idle transaction: {reason}",
            ))
            return
        logger.info("Rolled back an idle transaction on {}", self.name)
        self._refresh_state()
        self._send(MessageEvent(
            request_id="", severity=MessageSeverity.WARNING,
            text=f"Rolled back the transaction after {after_s / 60:g} min idle",
        ))

    def _close_idle_paused(self, mark: _PausedMark, limit_s: float) -> None:
        with self._state_lock:
            still = self._paused == mark
        execution = self._open_exec
        if self._closed or not still or execution is None or execution.request_id != mark.request_id:
            return  # a Load more, a new statement, a discard or closing came first: the result is not idle any more
        waited = self._clock() - mark.since if mark.since is not None else 0.0  # always set: check_paused_result
        logger.info("[{}] closed paused result {} {:.0f} s after the server was left waiting (limit {:g} s)",
                    self.name, mark.request_id, waited, limit_s)
        self._send(MessageEvent(
            request_id=mark.request_id, severity=MessageSeverity.WARNING, text=paused_result_closed_text(limit_s),
        ))
        self._finish(execution)

    @staticmethod
    def _cancel_text(req: _Request, default: str) -> str:
        return f"Timed out after {req.timeout_s:g} s" if req.timed_out else default

    def _connection_state(self) -> tuple[bool, bool, bool]:
        """(autocommit, in_transaction, transaction_failed); outside any transaction when the connection cannot tell
        (e.g. it is gone)."""
        try:
            connection = self.connection
            # bool(): building the state must never fail, or the request could not send its `finished`
            autocommit, in_transaction = bool(connection.autocommit()), bool(connection.in_transaction())
            return autocommit, in_transaction, bool(connection.transaction_failed())
        except Exception as exc:
            logger.debug("Session state of {} unavailable: {}", self.name, exc)
            last = self._tracker.snapshot()
            return (last.autocommit if last is not None else True), False, False

    def _refresh_state(self, began_at: datetime | None = None, ddl_done: bool = False) -> SessionStateEvent:
        """Recompute and send `session_state`; report DDL once other connections can see it (no open transaction)."""
        autocommit, in_transaction, failed = self._connection_state()
        self._ddl_pending = self._ddl_pending or ddl_done
        if self._ddl_pending and not in_transaction:
            self._ddl_pending = False
            self._notify("schema change", self._hooks.on_schema_change)
        with self._state_send_lock:
            state = self._tracker.update(autocommit, in_transaction, failed, began_at)
            self._send(state)
        return state

    def _send_finished(
        self, request_id: str, elapsed_ms: int, *, cancelled: bool = False, timed_out: bool = False,
        skipped: bool = False, began_at: datetime | None = None, ddl_done: bool = False,
    ) -> None:
        """The one `finished` every request ends with, right after the `session_state` it reports."""
        state = self._refresh_state(began_at, ddl_done)
        self.finished_at = self._clock()
        self._send(FinishedEvent(
            request_id=request_id,
            elapsed_ms=elapsed_ms,
            in_transaction=state.in_transaction,
            transaction_failed=state.transaction_failed,
            cancelled=cancelled,
            timed_out=timed_out,
            skipped=skipped,
        ))

    def _complete(self, req: _Request, *, error: StatementError | None = None, cancelled: bool = False) -> None:
        """End an execute: history record, then session state (+ schema invalidation), then `finished` — so whoever
        has seen `finished` can rely on all three."""
        self._listen(None)  # every execute and export ends here; a paused result keeps its listener until it does
        timed_out = cancelled and req.timed_out
        if req.history and req.ran:
            status = "error" if error else "timeout" if timed_out else "cancelled" if cancelled else "ok"
            self._notify("history", self._hooks.on_record, ExecutionRecord(
                request_id=req.request_id, sql=req.sql, started_at=req.started_at,
                elapsed_ms=req.first_page_ms if req.first_page_ms is not None else req.elapsed_ms(),
                rows=req.rows, status=status, error=error.message if error else None, query_context=req.query_context,
            ))
        ddl_done = error is None and not cancelled and req.kind == StatementKind.DDL
        self._send_finished(
            req.request_id, req.elapsed_ms(), cancelled=cancelled, timed_out=timed_out, began_at=req.started_at,
            ddl_done=ddl_done,
        )

    def _close_result(self, result: StatementResult) -> None:
        try:
            result.close()
        except Exception as exc:  # the code only above DEBUG: a server's text may echo literals (spec ⑤ §5, L9)
            code = getattr(exc, "code", None) or type(exc).__name__
            logger.warning("Error closing result on {} ({})", self.name, code)
            logger.debug("Error closing result on {}: {}", self.name, exc)

    def _finish(self, ex: _OpenExecution, cancelled: bool = False) -> None:
        self._clear_paused()
        self._set_running(None)  # before `finished` goes out: from then on a cancel must not reach the connection
        self._open_exec = None
        self._close_result(ex.result)
        self._complete(ex.req, cancelled=cancelled)

    def _fail(self, req: _Request, exc: StatementError) -> None:
        self._clear_paused()
        self._set_running(None)
        if self._open_exec is not None:
            self._close_result(self._open_exec.result)
            self._open_exec = None
        cancelled = isinstance(exc, StatementCancelled)
        if cancelled:
            text = self._cancel_text(req, exc.message)
            self._send(MessageEvent(request_id=req.request_id, severity=MessageSeverity.WARNING, text=text))
            self._complete(req, cancelled=True)
        else:
            self._send(ErrorEvent(request_id=req.request_id, message=exc.message, code=exc.code, position=exc.position))
            self._complete(req, error=exc)
