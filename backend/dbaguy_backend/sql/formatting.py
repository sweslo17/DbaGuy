"""Row rendering shared by copy and export, so both produce identical text.

- Copy (`/sql/format-rows`) formats rows the grid already holds: `source="encoded"` cells, as `encode_row` sent them.
- Export streams `source="raw"` driver values to a file through `open_writer`, so nothing is truncated.

Both go through the same normalisation step (`NORMALISERS`) and the same writer classes. Couchbase rows that are
already JSON text (`RawJsonRows`) are exported by `open_raw_json_writer`, which never re-reads them.
"""

import csv
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Sequence
from decimal import Decimal
from io import StringIO
from typing import Any, TextIO

from pydantic import BaseModel

from ..domain.column_category import ColumnCategory
from ..domain.errors import UnsupportedFormat, ValidationFailed
from ..domain.formats import FormatColumn, FormatKind, RowFormatOptions
from .dialects import Dialect, in_list, qualified, quote_ident
from .jsontext import one_line, reject_constant
from .literals import (
    MISSING_CELL, NORMALISERS, Binary, Cell, CellLiterals, Json, MalformedCell, MissingCell, Source, Truncated,
    value_condition,
)

__all__ = [
    "FormatColumn", "FormatKind", "FormatResult", "RAW_JSON_FORMATS", "RawJsonWriter", "RowFormatOptions", "RowWriter",
    "format_rows", "open_raw_json_writer", "open_writer", "unsupported_reason", "value_condition",
]

_ELLIPSIS = "…"
_DEFAULT_TABLE = "table_name"
# NUMERIC_LITERAL without the leading zeros that JSON forbids
_JSON_NUMBER = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")
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


def unsupported_reason(kind: FormatKind, dialect: Dialect) -> str | None:
    """Why `kind` cannot be produced for `dialect` (copy and export alike); None when it can."""
    return _UNSUPPORTED.get((kind, dialect))


def _normalise_row(row: Sequence[Any], row_index: int, normalise: Callable[[Any], Cell]) -> list[Cell]:
    cells = []
    for column_index, value in enumerate(row):
        try:
            cells.append(normalise(value))
        except MalformedCell as malformed:
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
        case Binary():
            return f"0x{cell.hex}{_ELLIPSIS if cell.truncated else ''}"
        case Truncated():
            return cell.preview + _ELLIPSIS
        case Json():
            return cell.text
        case MissingCell():
            return ""
        case _:
            return str(cell)


def _text_cell(cell: Cell, null_text: str) -> str:
    return null_text if cell is None else _display(cell)


def _json_cell(cell: Cell) -> Any:
    return cell if cell is None or isinstance(cell, (bool, int, float, str)) else _display(cell)


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
        return _dump_json(json.loads(text, parse_float=Decimal, parse_constant=reject_constant))
    except (ValueError, RecursionError):
        return None


def _single_line(text: str) -> str:
    """JSON text on one line: a line break (whitespace a document was stored with) is squeezed out between tokens,
    anything else is exactly as it is."""
    return one_line(text) if "\n" in text or "\r" in text else text


def _json_text(cell: Cell, column: FormatColumn) -> str:
    """A cell as JSON. PostgreSQL loads bigint, numeric and json as text, so number and JSON columns are written as
    what they are: a bare number with its exact digits, an embedded JSON value (a string that is not JSON stays one).
    A `Json` cell is already JSON text and goes in unchanged; any other string in a mixed column is a JSON string."""
    if isinstance(cell, Json):
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
        self._normalise = NORMALISERS[source]
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
            if cell is not MISSING_CELL
        )
        return "{" + ", ".join(fields) + "}"

    def _value(self, cell: Cell, column: FormatColumn) -> str:
        return _json_text(cell, column)


class _JsonlWriter(_ObjectWriter):
    def _value(self, cell: Cell, column: FormatColumn) -> str:
        text = _json_text(cell, column)
        return _single_line(text) if isinstance(cell, Json) else text  # a JSON Lines row is one line

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
        self._literals = CellLiterals(self._dialect)
        table = qualified(self._dialect, self._options.table_schema, self._options.table_name or _DEFAULT_TABLE)
        columns = ", ".join(quote_ident(self._dialect, name) for name in self._names)
        self._prefix = f"INSERT INTO {table} ({columns}) VALUES ("

    @property
    def warnings(self) -> list[str]:
        return _truncation_warnings(self._literals.truncated)

    def _write_row(self, cells: list[Cell]) -> None:
        values = ", ".join(self._literals(cell, column.category) for cell, column in zip(cells, self._columns))
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
    literals = CellLiterals(dialect)
    distinct = dict.fromkeys(literals(cells[0], columns[0].category) for cells in _cells(rows, 1, NORMALISERS[source]))
    return FormatResult(text=in_list(dialect, distinct), warnings=_truncation_warnings(literals.truncated))
