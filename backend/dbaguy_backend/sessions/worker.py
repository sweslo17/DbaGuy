import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, TypeVar

from loguru import logger
from pydantic import BaseModel

from ..adapters.base import DatabaseConnection, StatementCancelled, StatementError, StatementResult
from ..adapters.values import encode_row
from ..domain.errors import ConnectionFailed, SessionBusy
from ..domain.execution import (
    AffectedEvent,
    CancelCommand,
    DiscardCommand,
    ErrorEvent,
    ExecuteCommand,
    FetchMoreCommand,
    FinishedEvent,
    MessageEvent,
    MessageSeverity,
    ResultSetEndEvent,
    ResultSetEvent,
    RowsEvent,
    StartedEvent,
)

T = TypeVar("T")
Emitter = Callable[[BaseModel], None]
_CHUNK = 200
_STOP = object()


def _discard_event(_: BaseModel) -> None:
    return None


@dataclass
class _OpenExecution:
    request_id: str
    result: StatementResult
    started: float
    index: int = 0
    sent: int = 0
    columns_sent: bool = False
    peeked: list[list[Any]] = field(default_factory=list)  # raw rows read ahead to detect a full page


class SessionWorker:
    """Owns one connection and one thread; every driver call happens on that thread.

    Locking: `_queue_lock` guards `_closed` and every enqueue, so nothing can land behind the stop marker.
    `_state_lock` guards `_running_request`, `_cancel_requested` and `_queued`; it is held across
    `connection.cancel()`, so a cancel can never hit a statement other than the one it was meant for (the worker
    changes the running request under it).
    """

    def __init__(self, name: str, opener: Callable[[], Any]) -> None:
        self.name = name
        self._opener = opener
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=f"session-{name}", daemon=True)
        self._lease: Any = None
        self._open_error: BaseException | None = None
        self._opened = threading.Event()
        self._emit: Emitter = _discard_event
        self._emit_lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._running_request: str | None = None
        # A cancel reached the running request; honoured at the next page boundary in case no driver call was in
        # flight to interrupt (the driver then has nothing to cancel and the request would pause instead).
        self._cancel_requested = False
        self._queued: dict[str, bool] = {}  # execute requests waiting in the queue -> cancelled before they started
        self._open_exec: _OpenExecution | None = None
        self._closed = False
        self.last_activity = time.monotonic()

    # ---- lifecycle (any thread) ----

    def start(self, timeout: float = 30.0) -> None:
        self._thread.start()
        if not self._opened.wait(timeout):
            self._begin_close()  # the thread closes the connection itself as soon as the opener returns
            raise ConnectionFailed(f"Timed out opening a connection for {self.name}")
        if self._open_error is not None:
            raise self._open_error

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

    def call(self, fn: Callable[[DatabaseConnection], T], timeout: float | None = 120.0) -> T:
        """Run fn(connection) on the worker thread and wait for its result."""
        future: Future[T] = Future()

        def task() -> None:
            if not future.set_running_or_notify_cancel():
                return
            if self._closed:
                future.set_exception(SessionBusy("Session is closed"))
                return
            try:
                self._discard_open()
                future.set_result(fn(self.connection))
            except BaseException as exc:
                future.set_exception(exc)

        self._put(task)
        return future.result(timeout)

    def submit(self, command: BaseModel) -> None:
        match command:
            case ExecuteCommand():
                self._submit_execute(command)
            case FetchMoreCommand():
                self._put(lambda: self._fetch_more(command))
            case DiscardCommand():
                self._put(lambda: self._discard(command.request_id))
            case CancelCommand():
                self.cancel(command.request_id)

    def cancel(self, request_id: str) -> None:
        """Cancel a request: stop it if it is running, skip it if it is still queued, close it if it is paused."""
        with self._state_lock:
            if self._running_request == request_id:
                logger.info("Cancelling request {} on {}", request_id, self.name)
                self._cancel_requested = True
                self._cancel_connection()
                return
            if request_id in self._queued:
                logger.info("Request {} on {} cancelled before it started", request_id, self.name)
                self._queued[request_id] = True
                return
        try:
            self._put(lambda: self._discard(request_id, cancelled=True))  # a paused result, if it is the open one
        except SessionBusy:
            pass  # closing: the worker finishes the open result itself

    def close(self) -> None:
        if not self._begin_close():
            return
        with self._state_lock:
            if self._running_request is not None and self._lease is not None:
                self._cancel_connection()
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

    def _cancel_connection(self) -> None:
        try:
            self.connection.cancel()
        except Exception:
            logger.warning("Cancel failed on {}", self.name, exc_info=True)

    def _put(self, task: Callable[[], None]) -> None:
        with self._queue_lock:
            if self._closed:
                raise SessionBusy("Session is closed")
            self.last_activity = time.monotonic()
            self._queue.put(task)

    def _submit_execute(self, cmd: ExecuteCommand) -> None:
        with self._state_lock:
            self._queued[cmd.request_id] = False
        try:
            self._put(lambda: self._execute(cmd))
        except BaseException:
            with self._state_lock:
                self._queued.pop(cmd.request_id, None)
            raise

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
        self._opened.set()
        self._run_queued()
        self._discard_open()
        try:
            self._lease.close()
        except Exception:
            logger.exception("Error closing the connection of {}", self.name)
        logger.info("Session {} closed", self.name)

    def _run_queued(self) -> None:
        """Run queued tasks until the stop marker; once closing, tasks only fail fast (see _execute / call)."""
        while True:
            try:
                task = self._queue.get(block=self._lease is not None)
            except queue.Empty:
                return  # the opener failed: only what was queued before the failure is left
            if task is _STOP:
                return
            try:
                task()
            except Exception:
                logger.exception("Session task failed on {}", self.name)

    def _send(self, event: BaseModel) -> None:
        with self._emit_lock:
            emit = self._emit
        try:
            emit(event)
        except Exception:
            logger.exception("Failed to deliver event on {}", self.name)

    def _set_running(self, request_id: str | None) -> bool:
        """Change the running request. Returns whether a cancel had been requested for the previous one."""
        with self._state_lock:
            cancel_requested = self._cancel_requested
            self._running_request = request_id
            self._cancel_requested = False
        return cancel_requested

    def _transaction_state(self) -> tuple[bool, bool]:
        """(in_transaction, transaction_failed); both False when the connection cannot tell (e.g. it is gone)."""
        try:
            connection = self.connection
            return connection.in_transaction(), connection.transaction_failed()
        except Exception:
            return False, False

    def _begin_request(self, request_id: str) -> str | None:
        """Atomically leave the queue and become the running request. Returns why it must not run, if so."""
        with self._state_lock:
            cancelled = self._queued.pop(request_id, False)
            if cancelled:
                return "Query cancelled before it started"
            if self._closed:
                return "Session closed before the query started"
            self._running_request = request_id
            self._cancel_requested = False
        return None

    def _execute(self, cmd: ExecuteCommand) -> None:
        self._discard_open()
        started = time.monotonic()
        skipped = self._begin_request(cmd.request_id)
        self._send(StartedEvent(request_id=cmd.request_id))
        if skipped is not None:
            self._send(MessageEvent(request_id=cmd.request_id, severity=MessageSeverity.WARNING, text=skipped))
            self._send_finished(cmd.request_id, started, cancelled=True)
            return
        logger.debug("[{}] execute {}: {}", self.name, cmd.request_id, cmd.sql[:2000])
        try:
            result = self.connection.run(cmd.sql)
            self._open_exec = _OpenExecution(request_id=cmd.request_id, result=result, started=started)
            self._pump(self._open_exec, cmd.page_size)
        except StatementError as exc:
            self._fail(cmd.request_id, started, exc)
        except Exception as exc:
            logger.exception("Unexpected error executing on {}", self.name)
            self._fail(cmd.request_id, started, StatementError(f"Internal error: {exc}"))
        finally:
            self._set_running(None)

    def _fetch_more(self, cmd: FetchMoreCommand) -> None:
        execution = self._open_exec
        if self._closed:
            return  # the open result is finished when the thread stops
        if execution is None or execution.request_id != cmd.request_id:
            self._send(ErrorEvent(request_id=cmd.request_id, message="No open result to fetch from", code="no_open_result"))
            # Every request ends with exactly one `finished`; without it a client waiting on this request would hang.
            self._send_finished(cmd.request_id, None, cancelled=False)
            return
        self._set_running(cmd.request_id)
        try:
            self._pump(execution, cmd.count)
        except StatementError as exc:
            self._fail(cmd.request_id, execution.started, exc)
        except Exception as exc:
            logger.exception("Unexpected error fetching on {}", self.name)
            self._fail(cmd.request_id, execution.started, StatementError(f"Internal error: {exc}"))
        finally:
            self._set_running(None)

    def _discard(self, request_id: str, cancelled: bool = False) -> None:
        execution = self._open_exec
        if execution is None or execution.request_id != request_id:
            return
        if cancelled:
            self._send(MessageEvent(request_id=request_id, severity=MessageSeverity.WARNING, text="Query cancelled"))
        self._finish(execution, cancelled=cancelled)

    def _pause(self, ex: _OpenExecution) -> None:
        """A page is full and more rows exist: pause, unless a cancel arrived that the driver had nothing to stop."""
        # Leave the running state before any event: a cancel from now on discards instead of interrupting.
        if self._set_running(None):
            self._discard(ex.request_id, cancelled=True)
            return
        self._send(ResultSetEndEvent(request_id=ex.request_id, index=ex.index, row_count=ex.sent, has_more=True))

    def _discard_open(self) -> None:
        if self._open_exec is not None:
            self._finish(self._open_exec)

    def _send_messages(self, ex: _OpenExecution) -> None:
        for message in ex.result.take_messages():
            self._send(MessageEvent(request_id=ex.request_id, severity=message.severity, text=message.text))

    def _pump(self, ex: _OpenExecution, budget: int) -> None:
        result = ex.result
        while True:
            columns = result.columns()
            if columns is None:
                affected = result.affected_rows()
                if affected is not None:
                    self._send(AffectedEvent(request_id=ex.request_id, index=ex.index, rows=affected))
            else:
                if not ex.columns_sent:
                    self._send(ResultSetEvent(request_id=ex.request_id, index=ex.index, columns=columns))
                    ex.columns_sent = True
                while budget > 0:
                    want = min(budget, _CHUNK)
                    rows, ex.peeked = ex.peeked[:want], ex.peeked[want:]
                    if len(rows) < want:
                        rows += result.fetch(want - len(rows))
                    if rows:
                        cells = [encode_row(r) for r in rows]
                        self._send(RowsEvent(request_id=ex.request_id, index=ex.index, rows=cells))
                        ex.sent += len(rows)
                        budget -= len(rows)
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

    def _send_finished(self, request_id: str, started: float | None, cancelled: bool) -> None:
        """The one `finished` every request ends with; `started=None` for requests that never ran."""
        in_transaction, transaction_failed = self._transaction_state()
        self._send(FinishedEvent(
            request_id=request_id,
            elapsed_ms=0 if started is None else int((time.monotonic() - started) * 1000),
            in_transaction=in_transaction,
            transaction_failed=transaction_failed,
            cancelled=cancelled,
        ))

    def _finish(self, ex: _OpenExecution, cancelled: bool = False) -> None:
        self._set_running(None)  # before `finished` goes out: from then on a cancel must not reach the connection
        self._open_exec = None
        try:
            ex.result.close()
        except Exception as exc:
            logger.warning("Error closing result on {}: {}", self.name, exc)
        self._send_finished(ex.request_id, ex.started, cancelled)

    def _fail(self, request_id: str, started: float, exc: StatementError) -> None:
        self._set_running(None)
        if self._open_exec is not None:
            try:
                self._open_exec.result.close()
            except Exception:
                pass
            self._open_exec = None
        cancelled = isinstance(exc, StatementCancelled)
        if cancelled:
            self._send(MessageEvent(request_id=request_id, severity=MessageSeverity.WARNING, text=exc.message))
        else:
            self._send(ErrorEvent(request_id=request_id, message=exc.message, code=exc.code, position=exc.position))
        self._send_finished(request_id, started, cancelled)
