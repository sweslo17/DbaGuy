"""One MCP read (spec ⑧ §3.5's run_query steps 3–7, D-08-3, CR-08-7): lease the agent session, send the dialect's
ReadFrame opening with `call()` (never through the agent gate: DbaGuy's own text), the statement as an ExecuteCommand
(the gate again as the worker's check, describe off, no read-ahead), page by page as `ResultBudget` sizes them, discard
once nothing more is wanted, then ROLLBACK and the frame's end with `call()`. The call's deadline covers the lock, the
session's opening, the frame's opening and every page (P12, ruling T6-I2). A frame that does not end cleanly, or a read
that breaks, closes the session: the next call never meets a transaction or a lock this one left. The profile's login is
read after the read, best effort."""

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel

from ..adapters.base import DatabaseConnection, RunOptions, StatementCancelled
from ..domain.agent import ToolFailure
from ..domain.data_jobs import ENGINE_NAMES
from ..domain.errors import DomainError
from ..domain.execution import (
    DiscardCommand, ErrorEvent, ExecuteCommand, FetchMoreCommand, FinishedEvent, MessageEvent, QueryContext,
    ResultSetEndEvent, ResultSetEvent, RowsEvent,
)
from ..domain.profiles import AnyProfile, Engine
from ..sessions.worker import CLOSED_BEFORE_START
from ..sql.dialects import SERVER_TIMEOUT_MARGIN_S, Dialect, SessionAssumption, explain_head, quote_literal, read_frame
from .sessions import TICK_S, AgentSessions, Lease, WaitCancelled
from .shape import FIRST_PAGE, MAX_CELL_DEFAULT, MAX_ROWS_DEFAULT, NOTES_MAX, ResultBudget, note, size

FORCE_AFTER_S = 1.5  # after a timeout's cancel, a second (SQL Server's that ends a statement stuck in a transaction)
CANCEL_GRACE_S = 10.0  # then the statement has this long to stop before its session is closed
FRAME_END_S = 15.0  # ROLLBACK and the frame's end
_LEAST_S = 0.1  # the least time the frame's opening gets, its deadline past
_INTERNAL = RunOptions(describe=False)
RESET_NOTE = ("DbaGuy closed this connection after the call (it could not end the call cleanly); the next call opens a "
              "new one")
_STUCK = "The statement did not stop when it was cancelled: DbaGuy closed the connection"
CONNECTION_CLOSED = "connection_closed"  # the outcome's error_code when the session closed under the call
_CLOSED_UNDER = ("DbaGuy closed the connection during the call (its settings changed, or the MCP server stopped): try "
                 "again")
_ASSUMED = ("This {engine} login has {setting} {found}, which DbaGuy's read-only checks assume is {wanted}: give the "
            "AI a login (or database) with it {wanted}, or duplicate this connection")
_OPEN_LATE = "The connection did not open within the call's timeout"
# What the worker itself says when a statement ends cancelled: never a server message, so never a note
_CANCEL_TEXTS = frozenset({StatementCancelled().message, CLOSED_BEFORE_START, "Query cancelled before it started"})
ReadStatus = Literal["ok", "error", "timeout", "cancelled"]
_Stage = Literal["running", "cancelled", "forced"]


@dataclass(frozen=True)
class ReadRequest:
    profile: AnyProfile
    dialect: Dialect
    sql: str  # what the server gets: the agent's statement, or the explain tool's text
    database: str | None = None
    query_context: QueryContext | None = None  # Couchbase; None: the profile's default context
    max_rows: int = MAX_ROWS_DEFAULT
    max_cell_chars: int = MAX_CELL_DEFAULT
    timeout_s: float = 30.0
    explain: bool = False  # every result set's rows (T-SQL's SHOWPLAN plan is its second), SHOWPLAN around it
    # Called once the call holds its session's lock, before anything opens or is sent: raises when the call may no
    # longer read (the profile changed while it waited, review I1)
    admit: Callable[[], object] | None = None
    stops: int | None = None  # AgentSessions.stops() as the tool call began (final review M2); None: when read begins


@dataclass
class ReadOutcome:
    status: ReadStatus = "ok"
    columns: list[dict[str, str | None]] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    more_rows: bool = False  # the server had more than the result holds
    rows_dropped: int = 0  # rows read that the response had no room for
    columns_dropped: int = 0  # the last columns, which the response had no room for (with their values, review I5)
    notes: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    rows_read: int = 0
    read_capped: bool = False  # it stopped at READ_RAW_CHARS of values
    elapsed_ms: int = 0
    reset: bool = False  # the session was closed after the call

    def failed(self, code: str | None, message: str | None) -> None:
        self.status, self.error_code, self.error_message = "error", code, message


class CallControl:
    """A tool call's cancel from the MCP layer's thread (a client's cancel, A4): cancels its statement, once there is
    one; a cancel before that is kept."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel: Callable[[], None] | None = None
        self.cancelled = False

    def attach(self, cancel: Callable[[], None]) -> None:
        with self._lock:
            self._cancel = cancel
            fire = self.cancelled
        if fire:
            cancel()

    def detach(self) -> None:
        with self._lock:
            self._cancel = None

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            cancel = self._cancel
        if cancel is not None:
            cancel()


class _Events:
    """The session's emitter during a call: the events of its request, in order."""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self._queue: queue.Queue[BaseModel] = queue.Queue()

    def __call__(self, event: BaseModel) -> None:
        if getattr(event, "request_id", None) == self.request_id:
            self._queue.put(event)

    def next(self, timeout: float) -> BaseModel | None:
        try:
            return self._queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None


def _send(statements: list[str], connection: DatabaseConnection) -> None:
    """DbaGuy's own statements, on the worker thread; any rows read and dropped."""
    for sql in statements:
        result = connection.run(sql, _INTERNAL)
        try:
            while result.fetch(100):
                pass
        finally:
            result.close()


def _broken(assumed: SessionAssumption | None, connection: DatabaseConnection) -> str | None:
    """The setting's value when it is not what the lexer assumes, as the server last reported it (no round trip)."""
    if assumed is not None and (found := connection.reported_setting(assumed.setting)) != assumed.value:
        return found or "unknown"
    return None


def _begin(assumed: SessionAssumption | None, statements: list[str], connection: DatabaseConnection) -> str | None:
    """The frame's opening, unless the session's setting is not what the lexer assumes: then nothing is sent, and the
    setting's value is returned. Checked again once the opening is answered: a server reload reaches the session's
    parameter status only with the answer to its next command (re-review R-1); the caller then closes the session,
    which ends the transaction the opening began, before the statement."""
    if (found := _broken(assumed, connection)) is not None:
        return found
    _send(statements, connection)
    return _broken(assumed, connection)


def _from_profile(names: tuple[str, ...], profile: AnyProfile, dialect: Dialect) -> str:
    """ReadFrame.from_profile as `, <name> = <value>`: the profile's option of that name, as the adapter applied it at
    open (a value that is only blank was not applied), else DEFAULT."""
    given = {key.lower(): value for key, value in getattr(profile, "options", {}).items()}
    return "".join(f", {name} = {quote_literal(dialect, given[name]) if given.get(name, '').strip() else 'DEFAULT'}"
                   for name in names)


def _end(before: list[str], rolls_back: bool, after: list[str], connection: DatabaseConnection) -> None:
    _send(before, connection)
    if rolls_back:
        connection.rollback()
    _send(after, connection)


def _code(exc: BaseException) -> str:
    return getattr(exc, "code", None) or type(exc).__name__


class AgentReader:
    def __init__(self, sessions: AgentSessions, clock: Callable[[], float] = time.monotonic) -> None:
        self._sessions = sessions
        self._clock = clock

    def stops(self) -> int:
        """How many times the MCP server stopped (`AgentSessions.stops`): a tool call reads it as it begins."""
        return self._sessions.stops()

    def read(self, request: ReadRequest, control: CallControl | None = None) -> ReadOutcome:
        """The statement's first result (every result for an explain), within its limits. A password the profile
        lacks, a session that cannot open, a busy session, a session refused for a setting: DomainError / ToolFailure
        from the lease."""
        started = self._clock()
        deadline = started + request.timeout_s
        outcome = ReadOutcome()
        control = control or CallControl()
        reached = False  # the server answered: a session opened (only then is the login worth reading)
        try:
            with self._sessions.lease(request.profile, request.database, deadline, admit=request.admit,
                                      cancelled=lambda: control.cancelled, stops=request.stops) as lease:
                reached = True
                self._framed(lease, request, deadline, control, outcome)
        except TimeoutError:  # still opening at the deadline: the open stays in its slot for the next call (R-3)
            outcome.status, outcome.error_message = "timeout", _OPEN_LATE
        except WaitCancelled:  # the client gave up, or the MCP server stopped, while the call waited (review I2, M2)
            outcome.status = "cancelled"
        outcome.elapsed_ms = int((self._clock() - started) * 1000)
        if reached:
            self._sessions.read_login(request.profile, control.cancelled)  # on a thread of its own (re-review R-2)
        return outcome

    def _framed(self, lease: Lease, request: ReadRequest, deadline: float, control: CallControl,
                outcome: ReadOutcome) -> None:
        frame = read_frame(request.dialect)
        around = frame.explain_around if request.explain else None
        timeout_ms = max(1, int((request.timeout_s + SERVER_TIMEOUT_MARGIN_S) * 1000))  # 0 would mean no limit
        restored = _from_profile(frame.from_profile, request.profile, request.dialect)
        opening = [sql.format(timeout_ms=timeout_ms, from_profile=restored) for sql in frame.begin] + (
            [around[0]] if around else [])
        closing = [around[1]] if around else []
        if self._clock() >= deadline:  # the lock or the session's opening took it all: nothing is sent
            outcome.status = "timeout"
            return
        if control.cancelled:  # the client gave up while the call waited: nothing is sent
            outcome.status = "cancelled"
            return
        events = _Events(uuid4().hex)
        worker = lease.worker
        worker.set_emitter(events)
        try:
            if opening or frame.assumes is not None:
                try:
                    found = worker.call(partial(_begin, frame.assumes, opening),
                                        timeout=max(_LEAST_S, deadline - self._clock()))
                except TimeoutError:
                    self._reset(lease, outcome)
                    outcome.status = "timeout"
                    return
                except Exception as exc:  # the opening failed: whatever it began is closed with the session
                    self._reset(lease, outcome)
                    outcome.failed(_code(exc), getattr(exc, "message", None) or "The read could not start")
                    return
                if found is not None and frame.assumes is not None:
                    self._refuse(lease, request.profile, frame.assumes, found)
            if self._clock() >= deadline:
                # T6 re-review 2 observation 1: the opening answered past the deadline (it gets at least _LEAST_S): the
                # statement is not sent only to be cancelled at once (a slow server's cancel cost seconds and a
                # connection); the frame's end below rolls back what the opening began
                outcome.status = "timeout"
            else:
                try:
                    if not self._statement(lease, events, request, deadline, control, outcome):
                        return  # the session was closed under it
                except BaseException:  # review M-1: whatever broke, never a transaction or paused result left behind
                    self._reset(lease, outcome)
                    raise
        finally:
            worker.clear_emitter(events)
        if not (closing or frame.rolls_back or frame.end):
            return  # SQLite, Couchbase: nothing was begun
        try:
            # and the session's state read again: the idle-transaction check never sees the read's transaction
            worker.call(partial(_end, closing, frame.rolls_back, list(frame.end)), timeout=FRAME_END_S,
                        refresh_state=True)
        except Exception as exc:  # ROLLBACK or the lock release failed: never reuse this session (D-08-3)
            logger.warning("MCP: the frame's end failed on {} ({})", request.profile.id, _code(exc))
            self._reset(lease, outcome)

    def _statement(self, lease: Lease, events: _Events, request: ReadRequest, deadline: float, control: CallControl,
                   outcome: ReadOutcome) -> bool:
        """Run it and read its pages; False when the session had to be closed (or was closed under it)."""
        rid, worker = events.request_id, lease.worker
        context = request.query_context or lease.default_context
        sql = request.sql
        if request.explain and (head := explain_head(request.dialect, lease.server_version)) is not None:
            sql = f"{head} {sql}"  # D-08-10; the worker's check gates it again, as an explain's text
        if not self._submit(lease, ExecuteCommand(request_id=rid, sql=sql, page_size=FIRST_PAGE, history=False,
                                                  query_context=context), outcome):
            return False
        control.attach(lambda: worker.cancel(rid))
        budget: ResultBudget | None = None
        stage: _Stage = "running"
        since = 0.0  # when the stage began
        timed_out = False
        awaiting = False  # a page was asked for and nothing of it has come: a `finished` now means the result closed
        closed_before_start = False
        try:
            while True:
                now = self._clock()
                if stage == "running" and (control.cancelled or now >= deadline):
                    if not control.cancelled:  # the call's deadline (a client's cancel reached the worker already)
                        timed_out = True
                        worker.cancel(rid)
                    stage, since = "cancelled", now
                elif stage == "cancelled" and now >= since + FORCE_AFTER_S:
                    worker.cancel(rid)  # again: SQL Server's second Cancel ends a statement stuck in a transaction
                    stage, since = "forced", now
                elif stage == "forced" and now >= since + CANCEL_GRACE_S:
                    self._reset(lease, outcome)
                    outcome.status = "cancelled" if control.cancelled else "timeout"
                    outcome.error_message = _STUCK
                    return False
                if stage == "running":
                    wait = min(deadline - now, TICK_S)  # a client's cancel is looked for this often
                else:
                    wait = since + (FORCE_AFTER_S if stage == "cancelled" else CANCEL_GRACE_S) - now
                event = events.next(wait)
                if event is None:
                    continue
                match event:
                    case ResultSetEvent():
                        awaiting = False
                        if budget is None:
                            # null where the engine only knows a type from the values (SQLite without its peek, P9)
                            outcome.columns = [{"name": c.name, "type": c.type_name or None} for c in event.columns]
                            budget = ResultBudget(outcome.columns, request.max_rows, request.max_cell_chars)
                            outcome.columns, outcome.columns_dropped = budget.columns, budget.columns_dropped
                            budget.spend(sum(size(text) + 1 for text in outcome.notes))
                    case RowsEvent():
                        awaiting = False
                        if budget is not None and (event.index == 0 or request.explain):
                            outcome.rows_read += len(event.rows)
                            budget.take(event.rows)
                    case ResultSetEndEvent(has_more=True):
                        # only the first result set is read (every one for an explain)
                        wanted = event.index == 0 or request.explain
                        if budget is not None and wanted and not budget.full and stage == "running":
                            awaiting = True
                            command: BaseModel = FetchMoreCommand(request_id=rid, count=budget.next_page())
                        else:
                            awaiting = False
                            outcome.more_rows = outcome.more_rows or wanted
                            command = DiscardCommand(request_id=rid)
                        if not self._submit(lease, command, outcome):
                            return False
                    case ResultSetEndEvent():
                        awaiting = False
                    case MessageEvent():
                        closed_before_start = closed_before_start or event.text == CLOSED_BEFORE_START
                        if stage == "running" and len(outcome.notes) < NOTES_MAX:
                            text = note(event.text)
                            outcome.notes.append(text)
                            if budget is not None:
                                budget.spend(size(text) + 1)
                    case ErrorEvent():
                        awaiting = False
                        outcome.failed(event.code, event.message)
                    case FinishedEvent():
                        # the session closed under the call: before it started, while it waited for a page, or the
                        # close's own cancel of it (the profile changed, the MCP server stopped)
                        if closed_before_start or (awaiting and not event.cancelled) or (
                                event.cancelled and stage == "running" and worker.closed):
                            outcome.failed(CONNECTION_CLOSED, _CLOSED_UNDER)
                            outcome.more_rows = awaiting
                        elif event.cancelled:
                            # cancelled with no cancel of ours or the client's: the server's own limit (PG's
                            # statement_timeout, reported as cancelled)
                            timed_out = timed_out or not control.cancelled
                        timed_out = timed_out or event.timed_out
                        if event.cancelled:
                            outcome.notes = [text for text in outcome.notes if text not in _CANCEL_TEXTS]
                        break
        finally:
            control.detach()
        if budget is not None:
            outcome.rows, outcome.rows_dropped, outcome.read_capped = budget.rows, budget.dropped, budget.read_capped
            outcome.more_rows = outcome.more_rows or budget.dropped > 0
        if outcome.error_code == CONNECTION_CLOSED:
            lease.drop()  # whatever state it is in, it is never used again
            return False
        if control.cancelled:
            outcome.status = "cancelled"
        elif timed_out:
            outcome.status = "timeout"
        return True

    @staticmethod
    def _submit(lease: Lease, command: BaseModel, outcome: ReadOutcome) -> bool:
        """Send a command to the session; False when it is closed (its profile changed, the MCP server stopped)."""
        try:
            lease.worker.submit(command)
        except DomainError:
            outcome.failed(CONNECTION_CLOSED, _CLOSED_UNDER)
            lease.drop()
            return False
        return True

    @staticmethod
    def _refuse(lease: Lease, profile: AnyProfile, assumed: SessionAssumption, found: str) -> None:
        """Pre-flight M1, review M-2: the lexer would read this session's statements unlike the server. The statement
        was not sent (the frame's opening may have been: re-review R-1 checks again after it); the session is closed,
        which ends whatever the opening began, and the call refused (the next one opens a new session and checks it
        again)."""
        logger.warning("MCP: refused a read on {}: {} is {}", profile.id, assumed.setting, found)
        lease.drop()
        raise ToolFailure("refused", _ASSUMED.format(engine=ENGINE_NAMES[Engine(profile.engine)],
                                                     setting=assumed.setting, found=found, wanted=assumed.value))

    @staticmethod
    def _reset(lease: Lease, outcome: ReadOutcome) -> None:
        lease.drop()
        outcome.reset = True
        outcome.notes.append(RESET_NOTE)
