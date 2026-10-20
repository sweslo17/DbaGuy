"""Multi-row INSERTs (spec ⑥ D-06-5): a dump's data, and later a copy's. Rows go in as the driver read them (raw
values); each cell is written as its column's literal (`sql/literals.py`: the column's kind and its literal style), so a
value reads back as itself. A statement holds at most MAX_INSERT_ROWS rows (SQL Server: TSQL_INSERT_ROWS — one VALUES
list takes at most 1,000 rows, 10738, and 100 rows load three times faster, X4) and at most MAX_INSERT_CHARS of row
text; a row longer than that is a statement of its own. Pure."""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from ..adapters.columns import TableColumn
from ..domain.column_category import ColumnCategory
from ..domain.edits import kind_category
from .dialects import Dialect, overriding_identity, quote_ident
from .literals import Cell, CellLiterals, LiteralStyle, normalise_raw
from .statement_stream import MAX_STATEMENT_CHARS

MAX_INSERT_ROWS = 1000
TSQL_INSERT_ROWS = 100
MAX_INSERT_CHARS = 1_048_576
MYSQL_PACKET_BYTES = 64 * 1_048_576  # MySQL's default max_allowed_packet: the longest statement its server takes
_MEBIBYTE = 1_048_576
_UTF8_MOST = 4  # bytes a character takes at most in UTF-8
_PLAIN_EXPONENT = 1000  # a Decimal is written out in plain digits up to this many places (SQL Server's hold 38)
_TERMINATOR = 4  # what Run SQL file counts after a statement: its `;`, or a T-SQL batch's `\nGO`


def _cell(value: Any) -> Cell:
    """A driver's value as CellLiterals writes it. Numbers stay numbers: `normalise_raw` would make an integer past
    2^53 or an infinity text (for the grid's JSON), and a column without a number kind (SQLite's untyped columns) would
    then store it as text. A Decimal goes as its plain digits (`str` writes 1E-7, which MySQL and SQL Server read as a
    double)."""
    if isinstance(value, int | float):
        return value
    if isinstance(value, Decimal) and value.is_finite() and abs(value.as_tuple().exponent) <= _PLAIN_EXPONENT:
        return format(value, "f")
    return normalise_raw(value)


class InsertBatcher:
    """`add()` a row at a time, then `flush()`; each returns a finished statement (no terminator: the file adds it,
    `dump_script.insert_text`) or None. `row_chars`: the characters of every row's text so far (a dump sizes its next
    fetch by it, pre-flight M12)."""

    def __init__(
        self, dialect: Dialect, table_sql: str, columns: Sequence[TableColumn], *, backslash_escapes: bool | None = None,
        max_rows: int | None = None, max_chars: int = MAX_INSERT_CHARS,
    ) -> None:
        names = ", ".join(quote_ident(dialect, column.name) for column in columns)
        overriding = overriding_identity(dialect) if any(c.identity == "always" for c in columns) else None
        self._head = f"INSERT INTO {table_sql} ({names}){f' {overriding}' if overriding else ''} VALUES\n"
        writers = {unicode: CellLiterals(dialect, LiteralStyle(national=unicode, backslash_escapes=backslash_escapes))
                   for unicode in (True, False)}
        self._cells: list[tuple[CellLiterals, ColumnCategory]] = [
            (writers[column.unicode], kind_category(column.value_kind)) for column in columns]
        self._max_rows = max_rows or (TSQL_INSERT_ROWS if dialect is Dialect.TSQL else MAX_INSERT_ROWS)
        self._max_chars = max_chars
        self._rows: list[str] = []
        self._chars = 0
        self.row_chars = 0

    def row_text(self, row: Sequence[Any]) -> str:
        return "(" + ", ".join(write(_cell(value), category)
                               for value, (write, category) in zip(row, self._cells, strict=True)) + ")"

    def add(self, row: Sequence[Any]) -> str | None:
        text = self.row_text(row)
        self.row_chars += len(text)
        done = None
        if self._rows and (len(self._rows) >= self._max_rows or self._chars + len(text) > self._max_chars):
            done = self.flush()
        self._rows.append(text)
        self._chars += len(text) + 2  # and its ",\n"
        return done

    def flush(self) -> str | None:
        if not self._rows:
            return None
        statement = self._head + ",\n".join(self._rows)
        self._rows, self._chars = [], 0
        return statement


def size_warning(dialect: Dialect, name: str, statement: str) -> str | None:
    """What a dump says about a statement too large to restore (R-06-11, pre-flight M11), or None: past Run SQL file's
    limit (characters, as it measures), any dialect; past MySQL's default max_allowed_packet (UTF-8 bytes, measured
    only when the characters could reach it). Only a single row makes one: a statement of several holds at most
    MAX_INSERT_CHARS. `name`: the table as the file names it."""
    chars = len(statement)
    if chars + _TERMINATOR > MAX_STATEMENT_CHARS:
        return f"{name} has a row over {MAX_STATEMENT_CHARS // _MEBIBYTE} MiB: Run SQL file cannot restore it"
    if dialect is Dialect.MYSQL and chars * _UTF8_MOST > MYSQL_PACKET_BYTES:
        size = chars if statement.isascii() else len(statement.encode("utf-8", "surrogatepass"))
        if size > MYSQL_PACKET_BYTES:
            return (f"{name} has a row of {size / _MEBIBYTE:.0f} MiB: MySQL refuses statements over its "
                    "max_allowed_packet (64 MiB by default) when the file is run")
    return None
