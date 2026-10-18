"""The query service's streamed response, parsed as it arrives (spec §3.4.2). Pure: no I/O, no logging.

The answer to `POST /query/service` is one JSON object sent in chunks:
`{"requestID":…, "signature":…, "results":[row, row, …], "errors":[…], "warnings":[…], "status":…, "metrics":{…}}`.
`ResponseParser` turns the bytes into events: a `FieldEvent` for each top-level field other than `results` (decoded as
usual: they are small; with the value's exact text too, as the execution profile is shown), `ResultsStart`, one
`RowEvent` per row, `ResultsEnd`, and `EndEvent` when the object closes. An error that comes after rows comes after
their events, so no row is lost to it.

Rows keep the server's exact text (`RawRow.text`, and each member's text for an object row): numbers are never turned
into Python numbers here (`1.10`, `-0`, `1E+3`, `9007199254740993`, `1e400` stay as sent), keys stay in order. A row's
`value` is decoded with numbers left as text, so it only means something for strings, true, false and null; the cell
decision (Task 8's `value_cell`) reads the text.

How bytes become events:
- A strict incremental UTF-8 decoder (a character cut between chunks waits for its other bytes).
- A state machine over the top-level object and the `results` array; the position only moves past a unit (a key, a
  colon, a field value, a row, a bracket, a comma) once the whole unit has arrived.
- A value is accepted only when something follows it (a number may go on in the next chunk; at the end of the body a
  value with nothing after it is a cut response, so `[1,23` cut to `[1,2` never shows a row `2`). A decode error whose
  bad token runs to the end of what has arrived (an open string, `tru`, `1e`, `{"a"`) means "wait for more"; at the end
  of the body it means the response was cut short (`ProtocolError.truncated`). A bad token followed by more JSON is
  malformed at once.
- A value that is not complete yet is parsed again only once the text buffered from its start has doubled, and new text
  is kept as a list of pieces joined only for that attempt: a 5 MB row costs about twice its size, never a parse per
  chunk.
- A failure is raised by the call that meets it when nothing parsed before it in that call; otherwise the events before
  it are returned and every later call raises it (rows before a bad spot are not lost)."""

import codecs
import json
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from json.decoder import scanstring
from typing import Any, Literal, NoReturn

JsonKind = Literal["string", "number", "boolean", "null", "object", "array"]


@dataclass(frozen=True, slots=True)
class RowField:
    """One member of an object row: its key, the value's exact text, and the value decoded with numbers left as text
    (meaningful only for strings, true, false and null)."""

    key: str
    text: str
    value: Any


@dataclass(frozen=True, slots=True)
class RawRow:
    """One row as sent. `fields`: the members of an object row in order (then `value` is None); None for any other row
    (then `value` as in RowField)."""

    text: str
    value: Any
    fields: tuple[RowField, ...] | None


@dataclass(frozen=True, slots=True)
class FieldEvent:
    """A top-level field other than `results` (or a `results` that is not an array), decoded as usual. `text`: the
    value's exact text as sent (the `profile` field is shown as the server wrote it); not compared (ruling B36)."""

    name: str
    value: Any
    text: str = field(default="", compare=False)


@dataclass(frozen=True, slots=True)
class ResultsStart:
    pass


@dataclass(frozen=True, slots=True)
class RowEvent:
    row: RawRow


@dataclass(frozen=True, slots=True)
class ResultsEnd:
    pass


@dataclass(frozen=True, slots=True)
class EndEvent:
    """The response object has closed."""


Event = FieldEvent | ResultsStart | RowEvent | ResultsEnd | EndEvent


class ProtocolError(Exception):
    """The response is not the query service's JSON object. `offset`: characters read before the unit that failed;
    `field`: the top-level field being read (a name, never content); `truncated`: the body ended before the object
    closed (a dropped connection) rather than holding malformed JSON."""

    def __init__(self, message: str, offset: int, field: str | None, *, truncated: bool = False) -> None:
        super().__init__(message)
        self.offset, self.field, self.truncated = offset, field, truncated


_WHITESPACE = " \t\n\r"
_WHITESPACE_RUN = re.compile(r"[ \t\n\r]+")
_FOLLOWERS = frozenset(_WHITESPACE + ",]}")  # what may follow a value
_DELIMITER = re.compile(r"[ \t\n\r,\]}]")  # one of _FOLLOWERS, searched for
_KIND_BY_FIRST: dict[str, JsonKind] = {
    '"': "string", "{": "object", "[": "array", "t": "boolean", "f": "boolean", "n": "null",
}


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not JSON")


# Rows: numbers stay text (no float, no int() digit limit); values are decoded only to find their end and read strings.
_ROW_DECODER = json.JSONDecoder(parse_int=str, parse_float=str, parse_constant=_reject_constant)
# Top-level metadata (signature, errors, warnings, status, metrics, profile): small, decoded as usual.
_META_DECODER = json.JSONDecoder(parse_constant=_reject_constant)


class _Incomplete(Exception):
    """The buffer ends inside the value being read: wait for more."""


class _EndedEarly(ValueError):
    """The body ended inside a value: a cut response (a dropped connection), not a malformed one."""


def json_kind(text: str) -> JsonKind:
    """The kind of the JSON value `text`, from its first character."""
    return _KIND_BY_FIRST.get(text[:1], "number")


def _skip(s: str, i: int) -> int:
    if i < len(s) and s[i] in _WHITESPACE:
        return _WHITESPACE_RUN.match(s, i).end()  # type: ignore[union-attr]  # s[i] is whitespace: always a match
    return i


def _short(eof: bool, problem: str) -> Exception:
    """Running out of text: more may come, or (at the end of the body) the response was cut there."""
    return _EndedEarly(problem) if eof else _Incomplete()


def _failure(exc: json.JSONDecodeError, s: str, eof: bool) -> Exception:
    """A decode error means the text stops inside a value (an open string; `tru`, `-`, `{"a"` at the end of what has
    arrived) or is malformed (the bad token is followed by more JSON)."""
    if exc.msg.startswith("Unterminated") or not _DELIMITER.search(s, exc.pos):
        return _short(eof, "the text ends inside a value")
    return ValueError(f"malformed JSON ({exc.msg})")


def _decode(decoder: json.JSONDecoder, s: str, i: int, eof: bool) -> tuple[Any, int]:
    try:
        value, end = decoder.raw_decode(s, i)
    except json.JSONDecodeError as exc:
        raise _failure(exc, s, eof) from None
    if end < len(s) and s[end] not in _FOLLOWERS:
        if _DELIMITER.search(s, end):
            raise ValueError("malformed JSON after a value")
        raise _short(eof, "the text ends inside a number")  # `1e` of `1e400`, cut between two chunks
    if end >= len(s):
        # A number may go on in the next chunk; and a valid response never ends on a value, so at the end of the body
        # `[1,2` is `[1,23` cut short, not a row `2`.
        raise _short(eof, "the text ends after a value")
    return value, end


def _scan_object(s: str, start: int, eof: bool) -> tuple[tuple[RowField, ...], int]:
    """The members of the object at s[start] ("{") with their exact texts, and the index after its "}"."""
    fields: list[RowField] = []
    i = _skip(s, start + 1)
    if i < len(s) and s[i] == "}":
        return (), i + 1
    while True:
        if i >= len(s):
            raise _short(eof, "an object ends early")
        if s[i] != '"':
            raise ValueError("expected a key")
        try:
            key, i = scanstring(s, i + 1)
        except json.JSONDecodeError as exc:
            raise _failure(exc, s, eof) from None
        i = _skip(s, i)
        if i >= len(s):
            raise _short(eof, "an object ends early")
        if s[i] != ":":
            raise ValueError("expected a colon")
        i = _skip(s, i + 1)
        value, end = _decode(_ROW_DECODER, s, i, eof)
        fields.append(RowField(key, s[i:end], value))
        i = _skip(s, end)
        if i >= len(s):
            raise _short(eof, "an object ends early")
        if s[i] == "}":
            return tuple(fields), i + 1
        if s[i] != ",":
            raise ValueError("expected a comma")
        i = _skip(s, i + 1)


def split_object(text: str) -> tuple[RowField, ...]:
    """The members of one complete JSON object text, with their exact texts (alias expansion reads a document this
    way, Task 8). ValueError for anything else."""
    start = _skip(text, 0)
    if start >= len(text) or text[start] != "{":
        raise ValueError("not a JSON object")
    fields, end = _scan_object(text, start, eof=True)
    if _skip(text, end) != len(text):
        raise ValueError("text after the object")
    return fields


def parse_row(text: str) -> RawRow:
    """One complete row text, rebuilt exactly as ResponseParser made it (`parse_row(row.text) == row`): a paused
    result's read-ahead holds rows as their text only (result.py). ValueError for anything else."""
    if text[:1] == "{":
        fields, end = _scan_object(text, 0, eof=True)
        if _skip(text, end) != len(text):
            raise ValueError("text after the row")
        return RawRow(text, None, fields)
    # _decode refuses a value at the very end of its input (a number may go on): give it one delimiter after the text
    value, end = _decode(_ROW_DECODER, text + ",", 0, eof=True)
    if end != len(text):
        raise ValueError("text after the row")
    return RawRow(text, value, None)


class _State(Enum):
    """What may come next."""

    START = auto()  # "{"
    FIRST_KEY = auto()  # a key or "}"
    KEY = auto()  # a key (after ",")
    COLON = auto()
    VALUE = auto()  # a field's value
    AFTER_VALUE = auto()  # "," or "}"
    FIRST_ROW = auto()  # a row or "]"
    ROW = auto()  # a row (after ",")
    AFTER_ROW = auto()  # "," or "]"
    DONE = auto()  # nothing but whitespace


class ResponseParser:
    """See the module docstring. Not thread-safe: one per response, fed by the reading thread."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._buf, self._pos = "", 0
        self._pieces: list[str] = []  # text received since the last attempt, joined only for the next one
        self._pieces_len = 0
        self._need = 0  # characters from `_pos` that must be buffered before the next attempt (the doubling rule)
        self._dropped = 0  # characters already cut from the front of the buffer
        self._state = _State.START
        self._key = ""  # the top-level field being read ("" before the first one)
        self._failed: ProtocolError | None = None  # raised by every later call
        self.attempts = 0  # value parses started (tests read it)

    @property
    def offset(self) -> int:
        """Characters read before the unit being parsed."""
        return self._dropped + self._pos

    def feed(self, data: bytes) -> list[Event]:
        """The events `data` completes."""
        self._raise_failure()
        text, bad_byte = self._text(data, final=False)
        events = self._take(text, eof=False, now=bad_byte)  # what came before a bad byte is parsed now, not lost
        if bad_byte:
            self._fail("the response is not UTF-8", truncated=False)
        return self._deliver(events)

    def finish(self) -> list[Event]:
        """The body has ended: the last events, or ProtocolError(truncated=True) when the object never closed."""
        self._raise_failure()
        text, cut_character = self._text(b"", final=True)  # only a character the body cut short can fail here
        events = self._take(text, eof=True)
        if self._state is not _State.DONE:
            self._fail("the response ended early", truncated=True)
        elif cut_character:
            self._fail("data after the end of the response", truncated=False)
        return self._deliver(events)

    def _fail(self, problem: str, *, truncated: bool) -> None:
        if self._failed is None:  # the first failure stays
            self._failed = ProtocolError(problem, self.offset, self._key or None, truncated=truncated)

    def _deliver(self, events: list[Event]) -> list[Event]:
        if not events:
            self._raise_failure()
        return events  # a failure after these events is raised by the next call

    def _raise_failure(self) -> None:
        if self._failed is not None:
            raise self._failed

    def _text(self, data: bytes, final: bool) -> tuple[str, bool]:
        """The text `data` completes, and whether a byte that is not UTF-8 stopped it (the text before that byte is
        still returned)."""
        try:
            return self._decoder.decode(data, final=final), False
        except UnicodeDecodeError as exc:
            return exc.object[:exc.start].decode("utf-8"), True

    def _take(self, text: str, eof: bool, now: bool = False) -> list[Event]:
        """The events of the buffered text plus `text`. `now`: parse even when the doubling rule says to wait."""
        if text:
            self._pieces.append(text)
            self._pieces_len += len(text)
        if not (eof or now) and len(self._buf) - self._pos + self._pieces_len < self._need:
            return []  # still short of what the incomplete value needs: do not parse it again yet
        self._dropped += self._pos
        self._buf = self._buf[self._pos:] + "".join(self._pieces)
        self._pos, self._pieces, self._pieces_len, self._need = 0, [], 0, 0
        events: list[Event] = []
        try:
            while (i := _skip(self._buf, self._pos)) < len(self._buf):
                self._pos = i
                self._step(events, eof)
            self._pos = i  # whitespace at the end is consumed, not scanned again by the next call
        except _Incomplete:
            self._need = 2 * (len(self._buf) - self._pos)
        except (ValueError, RecursionError) as exc:  # cut short (_EndedEarly), malformed JSON, NaN, absurd nesting
            self._fail(str(exc) or type(exc).__name__, truncated=isinstance(exc, _EndedEarly))
        return events

    def _step(self, events: list[Event], eof: bool) -> None:
        """One unit at `_pos` (never whitespace); `_pos` moves past it only once it is complete."""
        s, i = self._buf, self._pos
        char = s[i]
        match self._state:
            case _State.START:
                if char != "{":
                    raise ValueError("not a JSON object")
                self._pos, self._state = i + 1, _State.FIRST_KEY
            case _State.FIRST_KEY | _State.KEY:
                if char == "}" and self._state is _State.FIRST_KEY:
                    self._end_object(events, i)
                    return
                if char != '"':
                    raise ValueError("expected a key")
                try:
                    key, end = scanstring(s, i + 1)
                except json.JSONDecodeError as exc:
                    raise _failure(exc, s, eof) from None
                self._key, self._pos, self._state = key, end, _State.COLON
            case _State.COLON:
                if char != ":":
                    raise ValueError("expected a colon")
                self._pos, self._state = i + 1, _State.VALUE
            case _State.VALUE:
                if self._key == "results" and char == "[":
                    events.append(ResultsStart())
                    self._pos, self._state = i + 1, _State.FIRST_ROW
                    return
                self.attempts += 1
                value, end = _decode(_META_DECODER, s, i, eof)
                events.append(FieldEvent(self._key, value, s[i:end]))
                self._pos, self._state = end, _State.AFTER_VALUE
            case _State.AFTER_VALUE:
                if char == "}":
                    self._end_object(events, i)
                elif char == ",":
                    self._pos, self._state = i + 1, _State.KEY
                else:
                    raise ValueError("expected a comma")
            case _State.FIRST_ROW | _State.ROW:
                if char == "]" and self._state is _State.FIRST_ROW:
                    self._end_results(events, i)
                    return
                events.append(RowEvent(self._row(s, i, eof)))
                self._state = _State.AFTER_ROW
            case _State.AFTER_ROW:
                if char == "]":
                    self._end_results(events, i)
                elif char == ",":
                    self._pos, self._state = i + 1, _State.ROW
                else:
                    raise ValueError("expected a comma between rows")
            case _State.DONE:
                raise ValueError("data after the end of the response")

    def _row(self, s: str, i: int, eof: bool) -> RawRow:
        """The row at s[i]; `_pos` moves past it."""
        self.attempts += 1
        if s[i] == "{":
            fields, end = _scan_object(s, i, eof)
            row = RawRow(s[i:end], None, fields)
        else:
            value, end = _decode(_ROW_DECODER, s, i, eof)
            row = RawRow(s[i:end], value, None)
        self._pos = end
        return row

    def _end_results(self, events: list[Event], i: int) -> None:
        events.append(ResultsEnd())
        self._pos, self._state = i + 1, _State.AFTER_VALUE

    def _end_object(self, events: list[Event], i: int) -> None:
        events.append(EndEvent())
        self._pos, self._state = i + 1, _State.DONE
