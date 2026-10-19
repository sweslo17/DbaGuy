"""Grid cells and SQL literals: one normalised shape for a cell, whether a raw driver value (export) or what
`encode_row` sent the grid (copy, Filter by this value, the grid's DML), and how a cell is written as a SQL literal or
matched in a WHERE (spec ⑤ D-05-1). Pure: no I/O; quoting and literal syntax come from `dialects` only."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from ..adapters.values import MISSING, JsonText, encode_value
from ..domain.column_category import ColumnCategory
from ..domain.errors import ValidationFailed
from .dialects import (
    NUMERIC_LITERAL, Dialect, bool_literal, bytes_literal, datetime_literal, json_literal, quote_ident, quote_literal,
    zero_date_as_text,
)

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


class CellLiterals:
    """Renders cells as SQL literals; a cell cut by the grid cannot be restored, so it becomes NULL and is counted."""

    def __init__(self, dialect: Dialect) -> None:
        self._dialect = dialect
        self.truncated = 0

    def __call__(self, cell: Cell, category: ColumnCategory) -> str:
        match cell:
            case None:
                return "NULL"
            case MissingCell():
                return "MISSING"
            case Json():
                return json_literal(self._dialect, cell.text)
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
                return datetime_literal(self._dialect, cell)
            case _:
                numeric = category == ColumnCategory.NUMBER and NUMERIC_LITERAL.fullmatch(cell)
                return cell if numeric else quote_literal(self._dialect, cell)


def value_condition(dialect: Dialect, column: str, value: Any, category: ColumnCategory) -> str | None:
    """The "Filter by this value" condition (spec §3.7) for one encoded grid cell, in the dialect's syntax.

    The database decides what matches, by the column's collation: MySQL's default collations ignore case, accents and
    trailing spaces, so the filter can return more rows than the one clicked. A JSON cell is compared as JSON where the
    dialect needs it said (MySQL), and a MySQL date with a zero part as text (R-03a-T9c). None when the cell is only a
    preview (cut text or bytes): no condition can be built from it."""
    name = quote_ident(dialect, column)
    if value is None:
        return f"{name} IS NULL"
    try:
        cell = normalise_encoded(value)
    except MalformedCell as malformed:
        raise ValidationFailed(f"Not a grid cell: {malformed}") from None
    if cell is MISSING_CELL:
        return f"{name} IS MISSING"
    if isinstance(cell, Truncated) or (isinstance(cell, Binary) and cell.truncated):
        return None
    if isinstance(cell, Json):
        return f"{name} = {json_literal(dialect, cell.text)}"
    if category == ColumnCategory.JSON and isinstance(cell, str):
        return f"{name} = {json_literal(dialect, cell)}"
    if category == ColumnCategory.DATETIME and isinstance(cell, str) and (text := zero_date_as_text(dialect, name, cell)):
        return f"{text} = {quote_literal(dialect, cell)}"
    return f"{name} = {CellLiterals(dialect)(cell, category)}"
