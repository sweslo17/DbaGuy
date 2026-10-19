"""Grid cells and SQL literals: one normalised shape for a cell, whether a raw driver value (export) or what
`encode_row` sent the grid (copy, Filter by this value, the grid's DML), and how a cell is written as a SQL literal or
matched in a WHERE (spec ⑤ D-05-1). Pure: no I/O; quoting and literal syntax come from `dialects` only."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import JsonValue

from ..adapters.positions import offset_to_line_col
from ..adapters.values import MAX_SAFE_INT, MISSING, JsonText, encode_value
from ..domain.column_category import ColumnCategory
from ..domain.edits import MAX_VALUE_CHARS, CellInput, EditColumn, EditKind, EditProblemCode, kind_category
from ..domain.errors import ValidationFailed
from .dialects import (
    NUMERIC_LITERAL, Dialect, bool_literal, bytes_literal, datetime_literal, json_literal, quote_ident, quote_literal,
    zero_date_as_text,
)
from .jsontext import LONE_SURROGATE, json_problem

Source = Literal["encoded", "raw"]
HEX_BYTES = re.compile(r"(?:[0-9a-fA-F]{2})*")


@dataclass(frozen=True, slots=True)
class Binary:
    hex: str  # complete value, or only the grid's preview when `truncated`
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class Truncated:
    """Text the grid cut at its size limit; only the preview is known."""

    preview: str


@dataclass(frozen=True, slots=True)
class Json:
    """A JSON value as its text (Couchbase objects, arrays, exact numbers): written as it is, never re-encoded."""

    text: str


class MissingCell:
    """SQL++ MISSING: the row has no such key. Empty in text formats, a left-out key in JSON (R-04-8)."""

    __slots__ = ()


MISSING_CELL = MissingCell()
Cell = None | bool | int | float | str | Binary | Truncated | Json | MissingCell


def normalise_raw(value: Any) -> Cell:
    if isinstance(value, JsonText):  # before encode_value, which would cut a long document for the grid
        return Json(value.text)
    if value is MISSING:
        return MISSING_CELL
    if isinstance(value, str):  # before encode_value, which would cut long text for the grid
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return Binary(bytes(value).hex())
    return encode_value(value)  # scalars unchanged; big ints, Decimal, dates, UUID, list/dict become text


class MalformedCell(Exception):
    """An encoded cell that is not shaped like `encode_value`'s output. The reason never contains the cell."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _encoded_binary(cell: dict[str, Any]) -> Binary:
    preview, size = cell.get("preview"), cell.get("size")
    if not (isinstance(preview, str) and HEX_BYTES.fullmatch(preview) and _is_int(size) and size >= 0):
        raise MalformedCell("a bytes cell needs a preview of whole hex bytes and a size of at least 0")
    return Binary(preview, truncated=len(preview) < 2 * size)  # preview is hex: 2 chars a byte


def _encoded_text(cell: dict[str, Any]) -> str | Truncated:
    preview, size, truncated = cell.get("preview"), cell.get("size"), cell.get("truncated", False)
    if not (isinstance(preview, str) and _is_int(size) and isinstance(truncated, bool)):
        raise MalformedCell("a text cell needs a text preview, a size and, optionally, a true/false truncated flag")
    return Truncated(preview) if truncated else preview


def _encoded_json(cell: dict[str, Any]) -> Json:
    text = cell.get("text")
    if not isinstance(text, str):
        raise MalformedCell("a json cell needs its text")
    return Json(text)


def normalise_encoded(value: Any) -> Cell:
    """Raises `MalformedCell`: the only objects an encoded cell can be are `encode_value`'s bytes, text, json and
    missing cells."""
    if isinstance(value, dict):
        match value.get("$type"):
            case "bytes":
                return _encoded_binary(value)
            case "text":
                return _encoded_text(value)
            case "json":
                return _encoded_json(value)
            case "missing":
                return MISSING_CELL
            case _:
                raise MalformedCell("an object cell must be a bytes, text, json or missing cell")
    return normalise_raw(value)


NORMALISERS: dict[Source, Callable[[Any], Cell]] = {"encoded": normalise_encoded, "raw": normalise_raw}


@dataclass(frozen=True, slots=True)
class LiteralStyle:
    """A column's literal options beyond its dialect (⑤ D-05-2, D-05-13). `national` False: a non-Unicode SQL Server
    column, whose ASCII text goes without the N prefix so a varchar key keeps its index seek (A8; non-ASCII text keeps
    N'…', correctness first, ruling E17). `backslash_escapes` False: MySQL with NO_BACKSLASH_ESCAPES; None: the
    dialect's own rule. The default style writes what copy and Filter by this value always wrote."""

    national: bool = True
    backslash_escapes: bool | None = None

    def quote(self, dialect: Dialect, text: str) -> str:
        return quote_literal(dialect, text, national=self.national or not text.isascii(),
                             backslash_escapes=self.backslash_escapes)

    def json(self, dialect: Dialect, text: str) -> str:
        return json_literal(dialect, text, national=self.national or not text.isascii(),
                            backslash_escapes=self.backslash_escapes)

    def datetime(self, dialect: Dialect, text: str) -> str:
        return datetime_literal(dialect, text, backslash_escapes=self.backslash_escapes)


DEFAULT_STYLE = LiteralStyle()


class CellLiterals:
    """Renders cells as SQL literals; a cell cut by the grid cannot be restored, so it becomes NULL and is counted."""

    def __init__(self, dialect: Dialect, style: LiteralStyle = DEFAULT_STYLE) -> None:
        self._dialect = dialect
        self._style = style
        self.truncated = 0

    def __call__(self, cell: Cell, category: ColumnCategory) -> str:
        match cell:
            case None:
                return "NULL"
            case MissingCell():
                return "MISSING"
            case Json():
                return self._style.json(self._dialect, cell.text)
            case bool():
                return bool_literal(self._dialect, cell)
            case Truncated() | Binary(truncated=True):
                self.truncated += 1
                return "NULL"
            case Binary():
                return bytes_literal(self._dialect, cell.hex)
            case int() | float():
                return str(cell)
            case _ if category == ColumnCategory.DATETIME:
                return self._style.datetime(self._dialect, cell)
            case _:
                numeric = category == ColumnCategory.NUMBER and NUMERIC_LITERAL.fullmatch(cell)
                return cell if numeric else self._style.quote(self._dialect, cell)


def _cell(value: Any) -> Cell:
    try:
        return normalise_encoded(value)
    except MalformedCell as malformed:
        raise ValidationFailed(f"Not a grid cell: {malformed}") from None


def _partial(cell: Cell) -> bool:
    """Only a preview of the value is known (text cut at TEXT_LIMIT, bytes beyond BYTES_PREVIEW)."""
    return isinstance(cell, Truncated) or (isinstance(cell, Binary) and cell.truncated)


def _equality(dialect: Dialect, name: str, cell: Cell, category: ColumnCategory, style: LiteralStyle) -> str:
    if isinstance(cell, Json):
        return f"{name} = {style.json(dialect, cell.text)}"
    if category == ColumnCategory.JSON and isinstance(cell, str):
        return f"{name} = {style.json(dialect, cell)}"
    if category == ColumnCategory.DATETIME and isinstance(cell, str) and (text := zero_date_as_text(dialect, name, cell)):
        return f"{text} = {style.quote(dialect, cell)}"
    return f"{name} = {CellLiterals(dialect, style)(cell, category)}"


def value_condition(
    dialect: Dialect, column: str, value: Any, category: ColumnCategory, style: LiteralStyle = DEFAULT_STYLE
) -> str | None:
    """The "Filter by this value" condition (spec §3.7) for one encoded grid cell, in the dialect's syntax.

    The database decides what matches, by the column's collation: MySQL's default collations ignore case, accents and
    trailing spaces, so the filter can return more rows than the one clicked. A JSON cell is compared as JSON where the
    dialect needs it said (MySQL), and a MySQL date with a zero part as text (R-03a-T9c). None when the cell is only a
    preview (cut text or bytes): no condition can be built from it."""
    name = quote_ident(dialect, column)
    if value is None:
        return f"{name} IS NULL"
    cell = _cell(value)
    if cell is MISSING_CELL:
        return f"{name} IS MISSING"
    if _partial(cell):
        return None
    return _equality(dialect, name, cell, category, style)


# --- grid edits (spec ⑤ §3.3.3) -----------------------------------------------------------------------------------


class ValueProblem(Exception):
    """A typed value that cannot become a literal: its problem code and a fixed message that never quotes it."""

    def __init__(self, code: EditProblemCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_TRUE = frozenset({"true", "t", "1", "yes"})
_FALSE = frozenset({"false", "f", "0", "no"})
_PG_SPECIAL_NUMBERS = {"nan": "NaN", "infinity": "Infinity", "-infinity": "-Infinity"}
# A SQLite integer is 64-bit (at most 19 digits): longer digits are text, and int() of them could exceed Python's
# 4,300-digit conversion limit (ValueError)
_INTEGER = re.compile(r"-?[0-9]{1,19}")
_SHOWN = 60  # characters of a key value a message shows


def _sendable(dialect: Dialect, text: str, style: LiteralStyle) -> None:
    """Text that can go into a quoted literal (`invalid_text`, spec §3.3.3, ruling E16)."""
    if not text.isascii() and LONE_SURROGATE.search(text):
        raise ValueProblem("invalid_text", "The value holds a character that is not valid text (half of an emoji?)")
    if "\x00" in text and (dialect is Dialect.POSTGRES or (dialect is Dialect.MYSQL and style.backslash_escapes is False)):
        raise ValueProblem("invalid_text", "The value holds a NUL character (U+0000), which cannot be written here")


def _number(dialect: Dialect, text: str) -> str:
    value = text.strip()
    if NUMERIC_LITERAL.fullmatch(value):
        return value
    if dialect is Dialect.POSTGRES and (special := _PG_SPECIAL_NUMBERS.get(value.lower())):
        return quote_literal(dialect, special)
    raise ValueProblem("invalid_number", "Not a number")


def _bool(dialect: Dialect, text: str) -> str:
    word = text.strip().lower()
    if word in _TRUE or word in _FALSE:
        return bool_literal(dialect, word in _TRUE)
    raise ValueProblem("invalid_bool", "Not true or false (true, false, t, f, 1, 0, yes, no)")


def _hex(dialect: Dialect, text: str) -> str:
    digits = text.strip()
    if digits[:2].lower() == "0x":
        digits = digits[2:]
    if not HEX_BYTES.fullmatch(digits):
        raise ValueProblem("invalid_hex", "Not hex digits (two per byte, an optional 0x in front)")
    return bytes_literal(dialect, digits.lower())


def _json(text: str) -> None:
    problem = json_problem(text)
    if problem is not None:
        line, column = offset_to_line_col(text, problem.offset + 1)
        raise ValueProblem("invalid_json", f"Not valid JSON at line {line}, column {column}: {problem.reason}")


def input_literal(dialect: Dialect, column: EditColumn, cell: CellInput, style: LiteralStyle) -> str:
    """The literal a typed value is sent as (spec §3.3.3, R-05-8): NULL, or the text read by the column's kind.
    Raises ValueProblem for a value the kind refuses. DEFAULT is never a literal: the plan leaves the column out."""
    if cell.kind == "null":
        return "NULL"
    if cell.kind == "default" or cell.text is None:
        raise ValueError("DEFAULT has no literal")
    text = cell.text
    if len(text) > MAX_VALUE_CHARS:
        raise ValueProblem("value_too_large", f"A value is limited to {MAX_VALUE_CHARS:,} characters")
    match column.kind:
        case "number":
            return _number(dialect, text)
        case "bool":
            return _bool(dialect, text)
        case "binary":
            return _hex(dialect, text)
        case "json":
            _json(text)
            _sendable(dialect, text, style)
            return style.quote(dialect, text)
        case "datetime":
            _sendable(dialect, text, style)
            return style.datetime(dialect, text)
        case "other" if dialect is Dialect.SQLITE and NUMERIC_LITERAL.fullmatch(text.strip()):
            return text.strip()  # no affinity: a number is stored as a number (spec §3.3.3)
        case _:
            _sendable(dialect, text, style)
            return style.quote(dialect, text)


def _server_text(cell: Cell) -> str:
    """A cell as the server's text output wrote it (PostgreSQL's `as_text` check, A4)."""
    match cell:
        case bool():
            return "true" if cell else "false"
        case Binary():
            return "\\x" + cell.hex
        case Json():
            return cell.text
        case _:
            return str(cell)


def match_condition(
    dialect: Dialect, column: EditColumn, value: JsonValue, style: LiteralStyle, *, key: bool
) -> str | None:
    """The WHERE term that pins `column` to `value`, an encoded grid cell (spec §3.3.3, D-05-2): `c = <literal>` for a
    key (always `=`, so an index can be used) and for an `equals` original; `c::text = '<text>'` for an `as_text` one
    (PostgreSQL: exactly the text the session loaded, A4); `c IS NULL` for NULL whatever the compare (ruling E5). None
    when no exact term exists: the cell is only a preview, or the column's compare is `none`."""
    name = quote_ident(dialect, column.name)
    if value is None:
        return f"{name} IS NULL"
    cell = _cell(value)
    if cell is MISSING_CELL or _partial(cell):
        return None
    if isinstance(cell, str) and ((not cell.isascii() and LONE_SURROGATE.search(cell)) or
                                  ("\x00" in cell and dialect is Dialect.MYSQL and style.backslash_escapes is False)):
        # no literal can carry it: no driver sends a lone surrogate (ruling E19), and MySQL writes U+0000 only with
        # backslash escapes (ruling E16, pre-flight L6). A key is then incomplete, an original unchecked
        return None
    compare = "equals" if key else column.compare
    if compare == "none":
        return None
    if compare == "as_text":
        return f"{name}::text = {quote_literal(dialect, _server_text(cell))}"
    if (dialect is Dialect.SQLITE and column.kind == "other" and isinstance(cell, str) and _INTEGER.fullmatch(cell)
            and abs(int(cell)) > MAX_SAFE_INT):
        # ruling E7: no affinity, and encode_value sent this integer as text; it may be stored as either
        return f"{name} IN ({cell}, {quote_literal(dialect, cell)})"
    return _equality(dialect, name, cell, kind_category(column.kind), style)


def _cut(text: str) -> str:
    return text if len(text) <= _SHOWN else text[:_SHOWN] + "…"


def cell_text(value: JsonValue, kind: EditKind) -> str:
    """A key cell as the user's messages show it (`42`, `'abc'`, `NULL`, `0x00ff`): never logged."""
    if value is None:
        return "NULL"
    try:
        cell = normalise_encoded(value)
    except MalformedCell:
        return "?"
    match cell:
        case bool():
            return "true" if cell else "false"
        case int() | float():
            return str(cell)
        case Binary():
            return "0x" + _cut(cell.hex) + ("…" if cell.truncated else "")
        case Truncated():
            return "'" + _cut(cell.preview) + "…'"
        case Json():
            return _cut(cell.text)
        case str() if kind == "number" and NUMERIC_LITERAL.fullmatch(cell):
            return cell
        case str():
            return "'" + _cut(cell) + "'"
        case _:
            return "?"
