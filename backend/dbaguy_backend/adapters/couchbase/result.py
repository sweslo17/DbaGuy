"""One Couchbase response as a StatementResult (spec §3.5, §3.7): rows read on demand from the wire, the `*` peek
inside columns(), messages from warnings and metrics, an error that follows rows raised where the worker will see it,
and each row's server text for raw JSON exports.

Threads: everything here runs on the session's worker thread. A cancel from another thread only shuts the connection
down (adapter.py); the read that fails because of it is reported as a cancel, since `cancelled()` says so."""

from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

from loguru import logger

from ...domain.execution import ColumnMeta, MessageSeverity, Presentation, ServerMessage
from ..base import RawJsonRows, StatementCancelled, StatementError, StatementResult
from .http import ChannelError
from .messages import PROTOCOL_ERROR, metrics_message, statement_error, warning_messages
from .rows import STAR_PEEK_BYTES, STAR_PEEK_ROWS, RowShape, build_shape, needs_first_page
from .stream import EndEvent, Event, FieldEvent, ProtocolError, RawRow, ResponseParser, ResultsEnd, ResultsStart, RowEvent

STOPPED_ON_THE_SERVER = "Query was stopped on the server"


class Response(Protocol):
    """What EventStream reads: http.QueryResponse (or a test's canned FakeResponse)."""

    def read_chunk(self) -> bytes: ...

    def abandon(self) -> None: ...


class EventStream:
    """The events of one response, read from the wire as they are asked for. Read failures become StatementCancelled
    when a cancel was asked for, else `connection_lost` (also for a response cut short) or `protocol_error`."""

    def __init__(self, response: Response, *, cancelled: Callable[[], bool], node: str) -> None:
        self._response, self._cancelled, self._node = response, cancelled, node
        self._parser = ResponseParser()
        self._events: deque[Event] = deque()
        self._ended = False  # the response object has closed (EndEvent seen), or the stream was closed
        self.done = False  # …and the body has been read to its end: the connection may carry the next request

    def next(self) -> Event | None:
        """The next event; None once the response object has closed."""
        while not self._events:
            if self._ended:
                return None
            self._read()
        event = self._events.popleft()
        if isinstance(event, EndEvent):
            self._ended = True
            self._read_trailer()
        return event

    def _read_trailer(self) -> None:
        """The last bytes after the object (a newline, the end of the chunked body): usually already here. The answer is
        complete, so a failure here costs the connection only, never the statement."""
        try:
            while not self.done:
                self._read()
        except StatementError as exc:
            logger.debug("Couchbase response from {} ended badly after its last field: {}", self._node, exc.message)
            self.close()

    def _read(self) -> None:
        try:
            chunk = self._response.read_chunk()
            events = self._parser.feed(chunk) if chunk else self._parser.finish()
        except ChannelError as exc:
            raise self._lost(str(exc)) from exc
        except ProtocolError as exc:
            if self._cancelled():
                raise StatementCancelled() from exc
            if exc.truncated:
                raise self._lost("the query node closed the connection mid-response") from exc
            logger.warning("Malformed response from the query node {} at offset {} (field {})", self._node, exc.offset,
                           exc.field)
            raise StatementError(PROTOCOL_ERROR, code="protocol_error") from exc
        self.done = not chunk
        self._events.extend(events)

    def _lost(self, reason: str) -> StatementError:
        if self._cancelled():
            return StatementCancelled()
        logger.warning("Connection to the query node {} was lost: {}", self._node, reason)
        return StatementError(f"Connection to the query node was lost: {reason}", code="connection_lost")

    def close(self) -> None:
        """Abandon an unfinished response: the server stops the request at once (`closed`) and the connection goes."""
        self._ended = True
        self._events.clear()
        if not self.done:
            self.done = True
            self._response.abandon()


class CouchbaseResult(StatementResult, RawJsonRows):
    """The rows of one statement, read as asked for. The columns come from the signature, or for a `*` from the first
    page, which is read inside `columns()` (the worker's timeout covers it) and delivered first by `fetch()` and
    `fetch_raw()` (R-04a-C14). An error or a stop that follows rows is kept until those rows are out, then raised by the
    next `fetch()` that has no rows, `next_result()` or `affected_rows()` (Review Focus 2)."""

    def __init__(self, stream: EventStream, sql: str, *, plan: bool, bare_star: bool, dml: bool,
                 on_done: Callable[[], None]) -> None:
        self._stream = stream
        self._sql = sql
        self._plan, self._bare_star, self._dml = plan, bare_star, dml
        self._on_done: Callable[[], None] | None = on_done  # called once: the response is over (read or abandoned)
        self._signature: Any = None
        self._errors: list[Any] = []
        self._status: Any = None
        self._metrics: dict[str, Any] = {}
        self._messages: list[ServerMessage] = []
        self._peeked: deque[RawRow] = deque()  # the first page, read to build the columns, not delivered yet
        self._rows_done = False  # no more row will come
        self._ended = False  # the response object has closed (or the result was closed)
        self._pending: StatementError | None = None  # a failure kept while rows read before it go out
        self._shape: RowShape | None = None
        self._shaped = False

    # ---- reading the response ----

    def read_head(self) -> None:
        """Up to the start of `results` (signature, early warnings), or the end of a response without rows, whose error
        or stop is raised here (spec §3.7.1 step 3)."""
        while True:
            event = self._stream.next()
            if isinstance(event, FieldEvent):
                self._field(event)
            elif isinstance(event, ResultsStart):
                return
            elif event is None or isinstance(event, EndEvent):
                self._end()
                if self._pending is not None:
                    raise self._take_pending()
                return

    def _field(self, event: FieldEvent) -> None:
        value = event.value
        match event.name:
            case "signature":
                self._signature = value
            case "errors":
                self._errors = value if isinstance(value, list) else [] if value is None else [value]
            case "warnings":
                if value is not None:
                    self._messages.extend(warning_messages(value if isinstance(value, list) else [value]))
            case "status":
                self._status = value
            case "metrics":
                self._metrics = value if isinstance(value, dict) else {}
                logger.debug("Couchbase metrics: {}", self._metrics)
            case "requestID" | "clientContextID":
                logger.debug("Couchbase {}: {}", event.name, value)

    def _end(self) -> None:
        """The response object closed: the metrics line, and what the statement ends with. Once."""
        if self._ended:
            return
        self._ended = self._rows_done = True
        if (line := metrics_message(self._metrics)) is not None:
            self._messages.append(line)
        if self._errors:
            self._pending = statement_error(self._errors, self._sql)
        elif self._status == "stopped":  # someone else stopped it (DELETE FROM system:active_requests)
            self._pending = StatementCancelled(STOPPED_ON_THE_SERVER)
        logger.debug("Couchbase response ended: status {}", self._status)
        self._release()

    def _next_row(self) -> RawRow | None:
        if self._peeked:
            return self._peeked.popleft()
        while not self._rows_done:
            event = self._stream.next()
            match event:
                case RowEvent(row=row):
                    return row
                case FieldEvent():
                    self._field(event)
                case ResultsEnd():
                    self._rows_done = True
                case EndEvent() | None:
                    self._end()
        return None

    def _read_to_end(self) -> None:
        """The fields after the rows (errors, status, metrics). Rows nobody asked for any more are dropped: the worker
        only gets here once it has read every row, or for a statement without rows."""
        while not self._ended:
            event = self._stream.next()
            if isinstance(event, FieldEvent):
                self._field(event)
            elif isinstance(event, ResultsEnd):
                self._rows_done = True
            elif event is None or isinstance(event, EndEvent):
                self._end()

    def _keep(self, exc: StatementError, delivered: bool) -> None:
        """A failure met while reading rows: raised now when nothing goes out with it, else kept for the next call."""
        if not delivered:
            raise exc
        self._pending = exc

    def _rows(self, n: int, convert: Callable[[RawRow], Any]) -> list[Any]:
        if n <= 0 or self.columns() is None:
            return []
        out: list[Any] = []
        while len(out) < n:
            if not self._peeked and self._pending is not None:
                break  # a failure kept while earlier rows went out: those rows first, then it
            try:
                row = self._next_row()
            except StatementError as exc:
                self._keep(exc, bool(out))  # the rows read so far go out first (Review Focus 2)
                break
            if row is None:
                break
            out.append(convert(row))
        self._shape_warning()
        if len(out) < n and self._pending is None:
            try:
                self._read_to_end()  # the rows are exhausted: errors, status and metrics follow them
            except StatementError as exc:
                self._keep(exc, bool(out))
        if not out and self._pending is not None:
            raise self._take_pending()
        return out

    def _shape_warning(self) -> None:
        if self._shape is not None and (warning := self._shape.take_warning()):
            logger.warning("A Couchbase result has rows with keys outside its columns")  # never the keys themselves
            self._messages.append(ServerMessage(severity=MessageSeverity.WARNING, text=warning))

    def _take_pending(self) -> StatementError:
        pending, self._pending = self._pending, None
        assert pending is not None
        return pending

    def _release(self) -> None:
        done, self._on_done = self._on_done, None
        if done is not None:
            done()

    # ---- StatementResult ----

    def columns(self) -> list[ColumnMeta] | None:
        if not self._shaped:
            first_page: list[RawRow] = []
            if needs_first_page(self._signature, self._plan):  # inside the worker's timeout (spec §3.5.1)
                size = 0
                while len(first_page) < STAR_PEEK_ROWS and (not first_page or size < STAR_PEEK_BYTES):
                    try:
                        row = self._next_row()
                    except StatementError as exc:
                        self._keep(exc, bool(first_page))  # the rows peeked so far still go out first
                        break
                    if row is None:
                        break
                    first_page.append(row)
                    size += len(row.text)  # characters, close enough to bytes for a limit (ruling P12)
                self._peeked.extend(first_page)
            self._shape = build_shape(self._signature, plan=self._plan, bare_star=self._bare_star,
                                      first_page=first_page)
            self._shaped = True
        return None if self._shape is None else self._shape.columns

    def fetch(self, n: int) -> list[list[Any]]:
        """Cells (spec §3.2.4); the shape sees every row once, in order (its "from row N" warning counts them)."""
        if self.columns() is None:
            return []
        assert self._shape is not None
        return self._rows(n, self._shape.cells)

    def fetch_raw(self, n: int) -> list[str]:
        """Each row's text as the server sent it (JSON / JSONL exports, R-04-15)."""
        return self._rows(n, _row_text)

    def affected_rows(self) -> int | None:
        """`metrics.mutationCount`; 0 for DML without it (the server leaves out a zero), else None (spec §3.5.5)."""
        self._read_to_end()
        if self._pending is not None:
            raise self._take_pending()
        count = self._metrics.get("mutationCount")
        return count if type(count) is int else (0 if self._dml else None)

    def next_result(self) -> bool:  # 04b adds the profile result set here
        self._read_to_end()
        if self._pending is not None:
            raise self._take_pending()
        return False

    def take_messages(self) -> list[ServerMessage]:
        messages, self._messages = self._messages, []
        return messages

    def presentation(self) -> Presentation:
        return "plan" if self._plan else "grid"

    def close(self) -> None:
        """Idempotent. A response read to its end leaves the connection for the next request; any other is abandoned,
        which stops the request on the server at once (no drain, spec §3.7.2)."""
        self._ended = self._rows_done = True
        self._peeked.clear()
        try:
            self._stream.close()
        finally:
            self._release()


def _row_text(row: RawRow) -> str:
    return row.text


def start_result(stream: EventStream, sql: str, *, plan: bool, bare_star: bool, dml: bool,
                 on_done: Callable[[], None]) -> CouchbaseResult:
    """Reads the head of the response (spec §3.7.1 step 3): signature, early errors and warnings, up to the start of
    `results` or the end. `run()` returns quickly because the server sends the signature first."""
    result = CouchbaseResult(stream, sql, plan=plan, bare_star=bare_star, dml=dml, on_done=on_done)
    try:
        result.read_head()
    except BaseException:
        result.close()
        raise
    return result
