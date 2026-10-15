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
    peeked: list[list[Any]] = field(default_factory=list)


class SessionWorker:
    """Owns one connection and one thread; every driver call happens on that thread."""

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
        self._state_lock = threading.Lock()
        self._running_request: str | None = None
        self._open_exec: _OpenExecution | None = None
        self._closed = False
        self.last_activity = time.monotonic()

    # ---- lifecycle (any thread) ----

    def start(self, timeout: float = 30.0) -> None:
        self._thread.start()
        if not self._opened.wait(timeout):
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
                self._put(lambda: self._execute(command))
            case FetchMoreCommand():
                self._put(lambda: self._fetch_more(command))
            case DiscardCommand():
                self._put(lambda: self._discard(command.request_id))
            case CancelCommand():
                self.cancel(command.request_id)

    def cancel(self, request_id: str) -> None:
        with self._state_lock:
            running = self._running_request == request_id
        if running:
            logger.info("Cancelling request {} on {}", request_id, self.name)
            self.connection.cancel()
        else:
            self._put(lambda: self._discard(request_id))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._state_lock:
            running = self._running_request is not None
        if running and self._lease is not None:
            self.connection.cancel()
        self._queue.put(_STOP)
        self._thread.join(timeout=10)

    def _put(self, task: Callable[[], None]) -> None:
        if self._closed:
            raise SessionBusy("Session is closed")
        self.last_activity = time.monotonic()
        self._queue.put(task)

    # ---- worker thread ----

    def _run(self) -> None:
        try:
            self._lease = self._opener()
        except BaseException as exc:
            self._open_error = exc
            self._opened.set()
            return
        self._opened.set()
        while True:
            task = self._queue.get()
            if task is _STOP:
                break
            try:
                task()
            except Exception:
                logger.exception("Session task failed on {}", self.name)
        self._discard_open()
        self._lease.close()
        logger.info("Session {} closed", self.name)

    def _send(self, event: BaseModel) -> None:
        with self._emit_lock:
            emit = self._emit
        try:
            emit(event)
        except Exception:
            logger.exception("Failed to deliver event on {}", self.name)

    def _set_running(self, request_id: str | None) -> None:
        with self._state_lock:
            self._running_request = request_id

    def _in_transaction(self) -> bool:
        try:
            return self.connection.in_transaction()
        except Exception:
            return False

    def _execute(self, cmd: ExecuteCommand) -> None:
        self._discard_open()
        started = time.monotonic()
        self._set_running(cmd.request_id)
        self._send(StartedEvent(request_id=cmd.request_id))
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
        if execution is None or execution.request_id != cmd.request_id:
            self._send(ErrorEvent(request_id=cmd.request_id, message="No open result to fetch from", code="no_open_result"))
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

    def _discard(self, request_id: str) -> None:
        if self._open_exec is not None and self._open_exec.request_id == request_id:
            self._discard_open()

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
                        self._send(RowsEvent(request_id=ex.request_id, index=ex.index, rows=rows))
                        ex.sent += len(rows)
                        budget -= len(rows)
                    self._send_messages(ex)
                    if len(rows) < want:
                        break  # result set exhausted
                else:
                    if not ex.peeked:
                        ex.peeked = result.fetch(1)
                    if ex.peeked:  # page full and more rows exist: pause
                        self._send(ResultSetEndEvent(request_id=ex.request_id, index=ex.index, row_count=ex.sent, has_more=True))
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

    def _finish(self, ex: _OpenExecution, cancelled: bool = False) -> None:
        self._open_exec = None
        try:
            ex.result.close()
        except Exception as exc:
            logger.warning("Error closing result on {}: {}", self.name, exc)
        self._send(FinishedEvent(
            request_id=ex.request_id,
            elapsed_ms=int((time.monotonic() - ex.started) * 1000),
            in_transaction=self._in_transaction(),
            cancelled=cancelled,
        ))

    def _fail(self, request_id: str, started: float, exc: StatementError) -> None:
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
        self._send(FinishedEvent(
            request_id=request_id,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            in_transaction=self._in_transaction(),
            cancelled=cancelled,
        ))
