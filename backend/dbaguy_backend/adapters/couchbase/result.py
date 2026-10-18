"""One Couchbase response as a StatementResult (spec §3.5, §3.7): rows read on demand from the wire, the `*` peek
inside columns(), messages from warnings and metrics, an error that follows rows raised where the worker will see it,
each row's server text for raw JSON exports, and for a run with profile (R-04-13, A16) the `profile` field that
follows `metrics` as a second result set: one `plan` cell holding the field's exact text.

Threads: everything here runs on the session's worker thread. A cancel from another thread only shuts the connection
down (adapter.py); the read that fails because of it is reported as a cancel, since `cancelled()` says so.

Read-ahead (R-04a-T11a): while the result is paused, the worker steps `read_ahead()`, which takes the rows the server
has already sent into memory so the server is not held back, up to the budget (`READ_AHEAD_BYTES`, counted by
`row_cost`). A step never waits: it reads only when the response is ready (`QueryResponse.ready()`: bytes on the
socket), at most READ_AHEAD_STEP_CHUNKS times. Held rows keep their text only and are parsed again when delivered
(`parse_row`): a parsed row with its fields costs 4–5 times its text (ruling B5). They come out after the `*` peek's
rows and before the stream's, and a failure met while reading ahead is kept until they are out."""

import sys
from collections import deque
from collections.abc import Callable, Sequence
from enum import Enum, auto
from typing import Any, Protocol

from loguru import logger

from ...domain.execution import ColumnMeta, MessageSeverity, Presentation, ServerMessage
from ..base import (
    RawJsonRows, ReadAhead, ReadAheadState, StatementCancelled, StatementError, StatementResult,
)
from .http import ChannelError
from .messages import PROTOCOL_ERROR, metrics_message, statement_error, warning_messages
from .rows import STAR_PEEK_BYTES, STAR_PEEK_ROWS, RowShape, build_shape, needs_first_page
from .stream import (
    EndEvent, Event, FieldEvent, ProtocolError, RawRow, ResponseParser, ResultsEnd, ResultsStart, RowEvent, parse_row,
)

STOPPED_ON_THE_SERVER = "Query was stopped on the server"
NO_PROFILE = "The server sent no execution profile (Community Edition does not give one)"
# One read-ahead step makes at most this many reads of up to READ_CHUNK (ruling B35: 16 reads took 0.42 s on 6-byte
# rows; with 2 the longest step was about 70 ms and filling 8 MiB took as long): a command waits for one step at most.
READ_AHEAD_STEP_CHUNKS = 2


def row_cost(text: str) -> int:
    """What a held row takes in memory: its text object and its slot in the deque."""
    return sys.getsizeof(text) + 8


class NotReady(Enum):
    """`EventStream.next_if_ready()`: no event is complete, and reading on would wait or the step's reads are used up."""

    NOT_READY = auto()


NOT_READY = NotReady.NOT_READY


class Response(Protocol):
    """What EventStream reads: http.QueryResponse (or a test's canned FakeResponse)."""

    done: bool  # the body has been read to its end

    def ready(self) -> bool: ...

    def read_chunk(self, wait: bool = True) -> bytes: ...

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
        self.received = 0  # body bytes read so far, by every path (a read-ahead step, fetch(), the trailer; B44)
        self.reads = 0  # reads made so far (a read-ahead step counts its own)

    def next(self) -> Event | None:
        """The next event; None once the response object has closed. Waits for the server."""
        event = self._next(None)
        assert event is not NOT_READY
        return event

    def queued(self) -> Event | None:
        """The next event if one is already parsed (no I/O), without taking it."""
        return self._events[0] if self._events else None

    def next_if_ready(self, max_reads: int) -> Event | None | NotReady:
        """As next(), but never waits (ruling B34): while no event is complete it reads one chunk at a time, only when
        the response is ready and at most `max_reads` times, and then gives NOT_READY. The bytes after the object are
        read only while ready, within the same `max_reads`."""
        return self._next(max_reads)

    def _next(self, max_reads: int | None) -> Event | None | NotReady:
        """`max_reads` None: wait for the server (next())."""
        reads = 0
        while not self._events:
            if self._ended:
                return None
            if max_reads is None:
                self._read()
            elif reads < max_reads and self._response.ready():
                reads += 1
                self._read(wait=False)
            else:
                return NOT_READY
        event = self._events.popleft()
        if isinstance(event, EndEvent):
            self._ended = True
            self._read_trailer(None if max_reads is None else max_reads - reads)
        return event

    def end_if_ready(self, max_reads: int) -> None:
        """The object has closed and the bytes after it were left (a step's reads ran out, or they had not arrived yet;
        on TLS the end of the chunked body comes in a record of its own): read them while ready, at most `max_reads`
        times, so the connection can carry the next request. Nothing to do otherwise."""
        if self._ended and not self.done:
            self._read_trailer(max_reads)

    def _read_trailer(self, max_reads: int | None) -> None:
        """The last bytes after the object (a newline, the end of the chunked body): usually already here. The answer is
        complete, so a failure here costs the connection only, never the statement. `max_reads` (a read-ahead step):
        read only while ready and at most that often; what is left is dropped by close() (the request is over, only
        the kept-alive connection goes)."""
        try:
            while not self.done:
                if max_reads is None:
                    self._read()
                elif max_reads > 0 and self._response.ready():
                    max_reads -= 1
                    self._read(wait=False)
                else:
                    return
        except StatementError as exc:
            logger.debug("Couchbase response from {} ended badly after its last field: {}", self._node, exc.message)
            self.close()

    def _read(self, wait: bool = True) -> None:
        """One read and the events it completes. `wait=False` (a read-ahead, only when the response is ready): a read
        that brings chunk framing only completes nothing and is not the end either (B33)."""
        self.reads += 1
        try:
            chunk = self._response.read_chunk(wait)
            self.received += len(chunk)
            events = self._parser.feed(chunk) if chunk else []
            if self._response.done:
                try:
                    events += self._parser.finish()
                except ProtocolError:
                    if not events:
                        raise
                    # the events before the failure go out first: the parser raises it again on the next call
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
        self.done = self._response.done
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
    `fetch_raw()` (R-04a-C14), then the rows read ahead while paused (`held`), then the stream. An error or a stop that
    follows rows is kept until those rows are out, then raised by the next `fetch()` that has no rows, `next_result()`
    or `affected_rows()` (Review Focus 2).

    `profile` (a run with profile): once the rows are over and no error is pending, `next_result()` turns the result
    into a second set, the `profile` field as one `plan` cell; an answer without that field (Community Edition) has no
    second set and says so once. The field's text is kept outside the read-ahead budget, which counts rows only: it
    comes after the rows, when a step reads on to the end whatever it holds. `messages` (the parameter warnings) are
    delivered by the first `take_messages()`."""

    def __init__(self, stream: EventStream, sql: str, *, plan: bool, bare_star: bool, dml: bool,
                 on_done: Callable[[], None], profile: bool = False, messages: Sequence[ServerMessage] = ()) -> None:
        self._stream = stream
        self._sql = sql
        self._plan, self._bare_star, self._dml = plan, bare_star, dml
        self._on_done: Callable[[], None] | None = on_done  # called once: the response is over (read or abandoned)
        self._signature: Any = None
        self._errors: list[Any] = []
        self._status: Any = None
        self._metrics: dict[str, Any] = {}
        self._messages: list[ServerMessage] = list(messages)
        self._profile_wanted = profile
        self._profile: str | None = None  # the `profile` field's exact text, kept until its set starts
        self._in_profile = False  # the second set: the profile's one row
        self._peeked: deque[RawRow] = deque()  # the first page, read to build the columns, not delivered yet
        self._held: deque[str] = deque()  # rows read ahead while paused, their text only (parsed again when delivered)
        self.held = 0  # the bytes those rows take (row_cost)
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
            case "profile" if self._profile_wanted:  # unasked (a node-wide profile setting), it is not shown
                self._profile = event.text
                logger.debug("Couchbase execution profile: {} characters", len(event.text))  # never its content
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
        elif self._profile_wanted and self._profile is None:
            # said here, not in next_result(): the worker takes the messages before it asks for the next set
            self._messages.append(ServerMessage(severity=MessageSeverity.INFO, text=NO_PROFILE))
        if self._pending is not None:
            self._profile = None  # an answer that failed shows its error, never a profile
        logger.debug("Couchbase response ended: status {}", self._status)
        self._release()

    def _next_row(self) -> RawRow | None:
        if self._peeked:
            return self._peeked.popleft()
        if self._held:
            text = self._held.popleft()
            self.held -= row_cost(text)
            return parse_row(text)
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
            if not self._peeked and not self._held and self._pending is not None:
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
        """`metrics.mutationCount`; 0 for DML without it (the server leaves out a zero), else None (spec §3.5.5). None
        for the profile set."""
        if self._in_profile:
            return None
        self._read_to_end()
        if self._pending is not None:
            raise self._take_pending()
        count = self._metrics.get("mutationCount")
        return count if type(count) is int else (0 if self._dml else None)

    def next_result(self) -> bool:
        """The profile set once the rows are over (see the class docstring), else False; an error or a stop that came
        with the answer is raised instead. The profile set's own next_result() is False: its text is taken."""
        self._read_to_end()
        if self._pending is not None:
            raise self._take_pending()
        if self._profile is None:
            return False
        self._start_profile_set(self._profile)
        return True

    def _start_profile_set(self, text: str) -> None:
        """The second set: one row, the profile's text in the `plan` shape. Rows of the first set nobody fetched are
        dropped, held ones as those still on the wire (`_read_to_end`); the worker only asks once the rows are out."""
        self._profile, self._in_profile = None, True
        self._held.clear()
        self.held = 0
        self._peeked = deque([RawRow(text, None, None)])
        self._shape = build_shape(None, plan=True, bare_star=False, first_page=[])
        self._shaped = True
        logger.debug("Couchbase execution profile delivered as a plan result set")

    def take_messages(self) -> list[ServerMessage]:
        messages, self._messages = self._messages, []
        return messages

    def presentation(self) -> Presentation:
        return "plan" if self._plan or self._in_profile else "grid"

    def read_ahead(self, budget: int) -> ReadAhead:
        """One step (see the module docstring): the events the stream already holds, then reads while the response is
        ready, at most READ_AHEAD_STEP_CHUNKS of them, until the rows held take `budget` (FULL; checked after each row,
        so a row is never split and the rows after it stay queued; the end of the rows already parsed after the last
        row is still taken, without reading, and after it the step reads on), the answer ends (DONE: `_end()` releases
        the connection), or nothing more is ready (WAITING when this step read nothing and took nothing, else READING:
        a chunk inside a long row completes no event and is still progress, B34). A failure is kept for after the held
        rows, and the step says DONE."""
        if self._ended or self._pending is not None:
            return ReadAhead(ReadAheadState.DONE, self._stream.received)
        reads, took = self._stream.reads, False
        while True:
            if self.held < budget or self._rows_done:  # after the rows only the end's small fields come: read on
                max_reads = READ_AHEAD_STEP_CHUNKS - (self._stream.reads - reads)
            elif isinstance(self._stream.queued(), RowEvent | None):
                return ReadAhead(ReadAheadState.FULL, self._stream.received)
            else:  # full, but the end of the rows is already parsed: take it without reading (an answer in memory
                max_reads = 0  # must not look like a stall, or the idle close would drop it)
            try:
                event = self._stream.next_if_ready(max_reads)
            except StatementError as exc:  # a cut connection, a protocol error, a cancel
                self._pending = exc  # raised once the rows held before it are out (Review Focus 2)
                return self._ahead_done()
            if event is NOT_READY:
                idle = not took and self._stream.reads == reads
                return ReadAhead(ReadAheadState.WAITING if idle else ReadAheadState.READING, self._stream.received)
            took = True
            match event:
                case RowEvent(row=row):
                    if not self._rows_done:  # as _read_to_end(): nothing after the rows is a row
                        self._held.append(row.text)
                        self.held += row_cost(row.text)
                case FieldEvent():
                    self._field(event)
                case ResultsEnd():
                    self._rows_done = True
                case EndEvent() | None:
                    self._end()  # an error or a stop in the answer is kept for after the held rows
                    return self._ahead_done()

    def _ahead_done(self) -> ReadAhead:
        logger.debug("Couchbase read-ahead reached the end of the answer: {} rows held ({} bytes){}", len(self._held),
                     self.held, ", then an error" if self._pending is not None else "")
        return ReadAhead(ReadAheadState.DONE, self._stream.received)

    def close(self) -> None:
        """Idempotent. A response read to its end leaves the connection for the next request; any other is abandoned,
        which stops the request on the server at once (no drain, spec §3.7.2). The rows held are dropped. An answer
        whose object has closed but whose last bytes were left by the read-ahead reads them first if they are there
        (a step's worth of reads, never a wait), so its connection is kept."""
        self._ended = self._rows_done = True
        self._peeked.clear()
        self._held.clear()
        self.held = 0
        self._profile = None
        try:
            self._stream.end_if_ready(READ_AHEAD_STEP_CHUNKS)
            self._stream.close()
        finally:
            self._release()


def _row_text(row: RawRow) -> str:
    return row.text


def start_result(stream: EventStream, sql: str, *, plan: bool, bare_star: bool, dml: bool,
                 on_done: Callable[[], None], profile: bool = False,
                 messages: Sequence[ServerMessage] = ()) -> CouchbaseResult:
    """Reads the head of the response (spec §3.7.1 step 3): signature, early errors and warnings, up to the start of
    `results` or the end. `run()` returns quickly because the server sends the signature first. `profile` and
    `messages`: see CouchbaseResult."""
    result = CouchbaseResult(stream, sql, plan=plan, bare_star=bare_star, dml=dml, on_done=on_done, profile=profile,
                             messages=messages)
    try:
        result.read_head()
    except BaseException:
        result.close()
        raise
    return result
