"""Row rendering shared by copy and export, so both produce identical text.

- Copy (`/sql/format-rows`) formats rows the grid already holds: `source="encoded"` cells, as `encode_row` sent them.
- Export streams `source="raw"` driver values to a file through `open_writer`, so nothing is truncated.

Both go through the same normalisation step (`_NORMALISERS`) and the same writer classes. Couchbase rows that are
already JSON text (`RawJsonRows`) are exported by `open_raw_json_writer`, which never re-reads them.
"""

import csv
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from io import StringIO
from typing import Any, Literal, NoReturn, TextIO

from pydantic import BaseModel

from ..adapters.values import MISSING, JsonText, encode_value
from ..domain.column_category import ColumnCategory
from ..domain.errors import UnsupportedFormat, ValidationFailed
from ..domain.formats import FormatColumn, FormatKind, RowFormatOptions
from .dialects import (
    NUMERIC_LITERAL, Dialect, bool_literal, bytes_literal, datetime_literal, in_list, json_literal, qualified,
    quote_ident, quote_literal, zero_date_as_text,
)
from .jsontext import one_line

__all__ = [
    "FormatColumn", "FormatKind", "FormatResult", "RAW_JSON_FORMATS", "RawJsonWriter", "RowFormatOptions", "RowWriter",
    "format_rows", "open_raw_json_writer", "open_writer", "unsupported_reason", "value_condition",
]

Source = Literal["encoded", "raw"]

_ELLIPSIS = "…"
_DEFAULT_TABLE = "table_name"
# NUMERIC_LITERAL without the leading zeros that JSON forbids
_JSON_NUMBER = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_HEX_BYTES = re.compile(r"(?:[0-9a-fA-F]{2})*")
_NEWLINES = re.compile(r"\r\n|\r|\n")
_TSV_SPECIAL = frozenset('\t\n\r"')
_UNSUPPORTED: dict[tuple[FormatKind, Dialect], str] = {
    # SQL++ INSERT takes a key and a document, not columns (spec §3.2.4)
    (FormatKind.INSERT, Dialect.SQLPP): "INSERT statements are not offered for Couchbase",
}
RAW_JSON_FORMATS: frozenset[FormatKind] = frozenset({FormatKind.JSON, FormatKind.JSONL})


class FormatResult(BaseModel):
    text: str
    warnings: list[str] = []


# --- cells: one normalised shape for both sources ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Binary:
    hex: str  # complete value, or only the grid's preview when `truncated`
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class _Truncated:
    """Text the grid cut at its size limit; only the preview is known."""

    preview: str


@dataclass(frozen=True, slots=True)
class _Json:
    """A JSON value as its text (Couchbase objects, arrays, exact numbers): written as it is, never re-encoded."""

    text: str


class _MissingCell:
    """SQL++ MISSING: the row has no such key. Empty in text formats, a left-out key in JSON (R-04-8)."""

    __slots__ = ()


_MISSING_CELL = _MissingCell()
Cell = None | bool | int | float | str | _Binary | _Truncated | _Json | _MissingCell


def unsupported_reason(kind: FormatKind, dialect: Dialect) -> str | None:
    """Why `kind` cannot be produced for `dialect` (copy and export alike); None when it can."""
    return _UNSUPPORTED.get((kind, dialect))


def _normalise_raw(value: Any) -> Cell:
    if isinstance(value, JsonText):  # before encode_value, which would cut a long document for the grid
        return _Json(value.text)
    if value is MISSING:
        return _MISSING_CELL
    if isinstance(value, str):  # before encode_value, which would cut long text for the grid
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _Binary(bytes(value).hex())
    return encode_value(value)  # scalars unchanged; big ints, Decimal, dates, UUID, list/dict become text


class _MalformedCell(Exception):
    """An encoded cell that is not shaped like `encode_value`'s output. The reason never contains the cell."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _encoded_binary(cell: dict[str, Any]) -> _Binary:
    preview, size = cell.get("preview"), cell.get("size")
    if not (isinstance(preview, str) and _HEX_BYTES.fullmatch(preview) and _is_int(size) and size >= 0):
        raise _MalformedCell("a bytes cell needs a preview of whole hex bytes and a size of at least 0")
    return _Binary(preview, truncated=len(preview) < 2 * size)  # preview is hex: 2 chars a byte


def _encoded_text(cell: dict[str, Any]) -> str | _Truncated:
    preview, size, truncated = cell.get("preview"), cell.get("size"), cell.get("truncated", False)
    if not (isinstance(preview, str) and _is_int(size) and isinstance(truncated, bool)):
        raise _MalformedCell("a text cell needs a text preview, a size and, optionally, a true/false truncated flag")
    return _Truncated(preview) if truncated else preview


def _encoded_json(cell: dict[str, Any]) -> _Json:
    text = cell.get("text")
    if not isinstance(text, str):
        raise _MalformedCell("a json cell needs its text")
    return _Json(text)


def _normalise_encoded(value: Any) -> Cell:
    """Raises `_MalformedCell`: the only objects an encoded cell can be are `encode_value`'s bytes, text, json and
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
                return _MISSING_CELL
            case _:
                raise _MalformedCell("an object cell must be a bytes, text, json or missing cell")
    return _normalise_raw(value)


_NORMALISERS: dict[Source, Callable[[Any], Cell]] = {"encoded": _normalise_encoded, "raw": _normalise_raw}


def _normalise_row(row: Sequence[Any], row_index: int, normalise: Callable[[Any], Cell]) -> list[Cell]:
    cells = []
    for column_index, value in enumerate(row):
        try:
            cells.append(normalise(value))
        except _MalformedCell as malformed:
            raise ValidationFailed(
                f"Row index {row_index}, column index {column_index} is not a valid cell: {malformed}",
                detail={"row": row_index, "column": column_index},
            ) from None
    return cells


def _cells(rows: Iterable[Sequence[Any]], width: int, normalise: Callable[[Any], Cell]) -> Iterator[list[Cell]]:
    for row_index, row in enumerate(rows):
        if len(row) != width:
            raise ValidationFailed(f"A row has {len(row)} cells but there are {width} columns")
        yield _normalise_row(row, row_index, normalise)


# --- cell renderers -----------------------------------------------------------------------------------------------


def _display(cell: Cell) -> str:
    """Text of a non-null cell."""
    match cell:
        case bool():
            return "true" if cell else "false"
        case _Binary():
            return f"0x{cell.hex}{_ELLIPSIS if cell.truncated else ''}"
        case _Truncated():
            return cell.preview + _ELLIPSIS
        case _Json():
            return cell.text
        case _MissingCell():
            return ""
        case _:
            return str(cell)


def _text_cell(cell: Cell, null_text: str) -> str:
    return null_text if cell is None else _display(cell)


def _json_cell(cell: Cell) -> Any:
    return cell if cell is None or isinstance(cell, (bool, int, float, str)) else _display(cell)


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not JSON")


def _dump_json(node: Any) -> str:
    """`json.dumps` for a tree parsed with `parse_float=Decimal`: decimals are written as they were read."""
    match node:
        case dict():
            fields = (f"{json.dumps(key, ensure_ascii=False)}: {_dump_json(value)}" for key, value in node.items())
            return "{" + ", ".join(fields) + "}"
        case list():
            return "[" + ", ".join(map(_dump_json, node)) + "]"
        case Decimal():
            return str(node)
        case _:
            return json.dumps(node, ensure_ascii=False)


def _embedded_json(text: str) -> str | None:
    """`text` as compact JSON with its numbers kept exactly as written; None when it is not JSON."""
    try:
        return _dump_json(json.loads(text, parse_float=Decimal, parse_constant=_reject_constant))
    except (ValueError, RecursionError):
        return None


def _single_line(text: str) -> str:
    """JSON text on one line: a line break (whitespace a document was stored with) is squeezed out between tokens,
    anything else is exactly as it is."""
    return one_line(text) if "\n" in text or "\r" in text else text


def _json_text(cell: Cell, column: FormatColumn) -> str:
    """A cell as JSON. PostgreSQL loads bigint, numeric and json as text, so number and JSON columns are written as
    what they are: a bare number with its exact digits, an embedded JSON value (a string that is not JSON stays one).
    A `_Json` cell is already JSON text and goes in unchanged; any other string in a mixed column is a JSON string."""
    if isinstance(cell, _Json):
        return cell.text
    if isinstance(cell, str):
        if column.category == ColumnCategory.NUMBER and _JSON_NUMBER.fullmatch(cell):
            return cell
        if column.category == ColumnCategory.JSON and (embedded := _embedded_json(cell)) is not None:
            return embedded
    return json.dumps(_json_cell(cell), ensure_ascii=False)


def _truncation_warnings(count: int) -> list[str]:
    if not count:
        return []
    return [f"{count} value(s) were truncated in the grid and written as NULL; export the full result for complete values"]


class _SqlLiterals:
    """Renders cells as SQL literals; a cell cut by the grid cannot be restored, so it becomes NULL and is counted."""

    def __init__(self, dialect: Dialect) -> None:
        self._dialect = dialect
        self.truncated = 0

    def __call__(self, cell: Cell, column: FormatColumn) -> str:
        match cell:
            case None:
                return "NULL"
            case _MissingCell():
                return "MISSING"
            case _Json():
                return json_literal(self._dialect, cell.text)
            case bool():
                return bool_literal(self._dialect, cell)
            case _Truncated() | _Binary(truncated=True):
                self.truncated += 1
                return "NULL"
            case _Binary():
                return bytes_literal(self._dialect, cell.hex)
            case int() | float():
                return str(cell)
            case _ if column.category == ColumnCategory.DATETIME:
                return datetime_literal(self._dialect, cell)
            case _:
                numeric = column.category == ColumnCategory.NUMBER and NUMERIC_LITERAL.fullmatch(cell)
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
        cell = _normalise_encoded(value)
    except _MalformedCell as malformed:
        raise ValidationFailed(f"Not a grid cell: {malformed}") from None
    if cell is _MISSING_CELL:
        return f"{name} IS MISSING"
    if isinstance(cell, _Truncated) or (isinstance(cell, _Binary) and cell.truncated):
        return None
    if isinstance(cell, _Json):
        return f"{name} = {json_literal(dialect, cell.text)}"
    if category == ColumnCategory.JSON and isinstance(cell, str):
        return f"{name} = {json_literal(dialect, cell)}"
    if category == ColumnCategory.DATETIME and isinstance(cell, str) and (text := zero_date_as_text(dialect, name, cell)):
        return f"{text} = {quote_literal(dialect, cell)}"
    return f"{name} = {_SqlLiterals(dialect)(cell, FormatColumn(name=column, category=category))}"


def _tsv_field(text: str) -> str:
    if _TSV_SPECIAL.isdisjoint(text):
        return text
    return '"' + text.replace('"', '""') + '"'


def _markdown_field(text: str) -> str:
    return _NEWLINES.sub(" ", text).replace("|", "\\|")


def _unique_names(names: Sequence[str]) -> list[str]:
    """Repeated names get `_2`, `_3`, ... (skipping real names) so JSON objects keep every column."""
    taken, seen, result = set(names), set(), []
    for name in names:
        unique = name
        if name in seen:
            suffix = 2
            while (unique := f"{name}_{suffix}") in taken:
                suffix += 1
            taken.add(unique)
        seen.add(name)
        result.append(unique)
    return result


# --- writers ---------------------------------------------------------------------------------------------------------


class RowWriter(ABC):
    """Writes rows into `fp` in one format. The caller owns `fp`; `close()` only finishes the document.

    `write` takes rows in the writer's `source` (raw driver values for `open_writer`).
    """

    def __init__(
        self, fp: TextIO, columns: Sequence[FormatColumn], options: RowFormatOptions, dialect: Dialect,
        source: Source = "raw",
    ) -> None:
        self._fp = fp
        self._columns = columns
        self._options = options
        self._dialect = dialect
        self._normalise = _NORMALISERS[source]
        self._begin()

    @property
    def warnings(self) -> list[str]:
        return []

    def write(self, rows: Iterable[Sequence[Any]]) -> None:
        for cells in _cells(rows, len(self._columns), self._normalise):
            self._write_row(cells)

    def close(self) -> None:
        self._end()

    @property
    def _names(self) -> list[str]:
        return [column.name for column in self._columns]

    def _begin(self) -> None:
        """Header; runs at the end of construction."""

    @abstractmethod
    def _write_row(self, cells: list[Cell]) -> None: ...

    def _end(self) -> None:
        """Footer."""


class _TableWriter(RowWriter, ABC):
    """Formats with one text field per cell."""

    def _begin(self) -> None:
        if self._options.header:
            self._emit(self._names)

    def _write_row(self, cells: list[Cell]) -> None:
        self._emit([_text_cell(cell, self._options.null_text) for cell in cells])

    @abstractmethod
    def _emit(self, fields: list[str]) -> None: ...


class _TsvWriter(_TableWriter):
    def _emit(self, fields: list[str]) -> None:
        self._fp.write("\t".join(_tsv_field(field) for field in fields) + "\n")


class _CsvWriter(_TableWriter):
    def _begin(self) -> None:
        self._csv = csv.writer(self._fp, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        super()._begin()

    def _emit(self, fields: list[str]) -> None:
        self._csv.writerow(fields)


class _MarkdownWriter(_TableWriter):
    def _begin(self) -> None:  # a Markdown table cannot do without its header row
        self._emit(self._names)
        self._emit(["---"] * len(self._columns))

    def _emit(self, fields: list[str]) -> None:
        self._fp.write("| " + " | ".join(_markdown_field(field) for field in fields) + " |\n")


class _ObjectWriter(RowWriter, ABC):
    """Formats with one JSON object per row, keyed by column name."""

    def _begin(self) -> None:
        self._keys = [json.dumps(key, ensure_ascii=False) for key in _unique_names(self._names)]

    def _object(self, cells: list[Cell]) -> str:
        fields = (  # a MISSING cell has no key in the object (R-04-8)
            f"{key}: {self._value(cell, column)}"
            for key, cell, column in zip(self._keys, cells, self._columns)
            if cell is not _MISSING_CELL
        )
        return "{" + ", ".join(fields) + "}"

    def _value(self, cell: Cell, column: FormatColumn) -> str:
        return _json_text(cell, column)


class _JsonlWriter(_ObjectWriter):
    def _value(self, cell: Cell, column: FormatColumn) -> str:
        text = _json_text(cell, column)
        return _single_line(text) if isinstance(cell, _Json) else text  # a JSON Lines row is one line

    def _write_row(self, cells: list[Cell]) -> None:
        self._fp.write(self._object(cells) + "\n")


class _JsonWriter(_ObjectWriter):
    """A JSON array with one object per line, written as rows arrive."""

    def _begin(self) -> None:
        super()._begin()
        self._fp.write("[\n")
        self._first = True

    def _write_row(self, cells: list[Cell]) -> None:
        self._fp.write(("" if self._first else ",\n") + "  " + self._object(cells))
        self._first = False

    def _end(self) -> None:
        self._fp.write("]\n" if self._first else "\n]\n")


class _InsertWriter(RowWriter):
    def _begin(self) -> None:
        self._literals = _SqlLiterals(self._dialect)
        table = qualified(self._dialect, self._options.table_schema, self._options.table_name or _DEFAULT_TABLE)
        columns = ", ".join(quote_ident(self._dialect, name) for name in self._names)
        self._prefix = f"INSERT INTO {table} ({columns}) VALUES ("

    @property
    def warnings(self) -> list[str]:
        return _truncation_warnings(self._literals.truncated)

    def _write_row(self, cells: list[Cell]) -> None:
        values = ", ".join(self._literals(cell, column) for cell, column in zip(cells, self._columns))
        self._fp.write(f"{self._prefix}{values});\n")


_WRITERS: dict[FormatKind, type[RowWriter]] = {
    FormatKind.TSV: _TsvWriter,
    FormatKind.CSV: _CsvWriter,
    FormatKind.JSON: _JsonWriter,
    FormatKind.JSONL: _JsonlWriter,
    FormatKind.MARKDOWN: _MarkdownWriter,
    FormatKind.INSERT: _InsertWriter,
}
_KEEPS_FINAL_NEWLINE = frozenset({FormatKind.CSV})  # the csv module's row terminator is part of its output


def _make_writer(
    kind: FormatKind, fp: TextIO, columns: Sequence[FormatColumn], options: RowFormatOptions, dialect: Dialect,
    source: Source,
) -> RowWriter:
    if (reason := unsupported_reason(kind, dialect)) is not None:  # copy and export alike
        raise UnsupportedFormat(reason)
    writer = _WRITERS.get(kind)
    if writer is None:
        raise ValueError(f"{kind.value} cannot be written as a stream")
    return writer(fp, columns, options, dialect, source)


class RawJsonWriter:
    """JSON / JSONL export of rows that are already JSON text (`RawJsonRows`): each row exactly as the server sent it
    (R-04-15). A JSON Lines row must be one line, so a row holding a line break (whitespace a document was stored with)
    is squeezed outside its strings, tokens unchanged; a JSON array keeps every row's bytes."""

    def __init__(self, kind: FormatKind, fp: TextIO) -> None:
        if kind not in RAW_JSON_FORMATS:
            raise ValueError(f"{kind.value} is not a raw JSON format")
        self._lines = kind == FormatKind.JSONL
        self._fp = fp
        self._first = True
        if not self._lines:
            fp.write("[")

    def write(self, rows: Iterable[str]) -> None:
        for text in rows:
            if self._lines:
                self._fp.write(_single_line(text) + "\n")
            else:
                self._fp.write(("\n  " if self._first else ",\n  ") + text)
            self._first = False

    def close(self) -> None:
        if not self._lines:
            self._fp.write("]\n" if self._first else "\n]\n")


# --- public entry points ---------------------------------------------------------------------------------------------


def open_raw_json_writer(kind: FormatKind, fp: TextIO) -> RawJsonWriter:
    """Streaming JSON / JSONL writer for `RawJsonRows` text (ValueError for any other kind)."""
    return RawJsonWriter(kind, fp)


def open_writer(
    kind: FormatKind, fp: TextIO, columns: Sequence[FormatColumn], options: RowFormatOptions, dialect: Dialect
) -> RowWriter:
    """Streaming writer for export: raw driver values in, nothing truncated. `in_list` is not a stream (ValueError)."""
    return _make_writer(kind, fp, columns, options, dialect, "raw")


def format_rows(
    kind: FormatKind, columns: Sequence[FormatColumn], rows: Iterable[Sequence[Any]], options: RowFormatOptions,
    dialect: Dialect, source: Source,
) -> FormatResult:
    if kind == FormatKind.IN_LIST:
        return _in_list(columns, rows, dialect, source)
    buffer = StringIO()
    writer = _make_writer(kind, buffer, columns, options, dialect, source)
    writer.write(rows)
    writer.close()
    text = buffer.getvalue()
    return FormatResult(text=text if kind in _KEEPS_FINAL_NEWLINE else text.removesuffix("\n"), warnings=writer.warnings)


def _in_list(columns: Sequence[FormatColumn], rows: Iterable[Sequence[Any]], dialect: Dialect, source: Source) -> FormatResult:
    if len(columns) != 1:
        raise ValidationFailed("An IN list needs exactly one column", detail={"columns": len(columns)})
    literals = _SqlLiterals(dialect)
    distinct = dict.fromkeys(literals(cells[0], columns[0]) for cells in _cells(rows, 1, _NORMALISERS[source]))
    return FormatResult(text=in_list(dialect, distinct), warnings=_truncation_warnings(literals.truncated))
