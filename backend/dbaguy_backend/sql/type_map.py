"""A copy's types and values across engines (spec ⑥ R-06-10, R-06-11, §3.5.2–3.5.3). `target_type`: the type a new
table's column gets on the target engine, with what the preview says of it (⚠, `note`) or why it has none (a
problem: a type to choose, or the column left out). Same-engine copies keep the declared type, unless it is the source
database's own (`TableColumn.custom_type`), which the target database may lack. `incompatible`: text and binary data do
not go into each other's columns. `conversion`: how each value goes from a source column into a target column — `None`
when it goes as the driver read it (the target column's literal writes it, `sql/literals.py`), else one function per
column pair, chosen once per table, that raises `RowFailure` for a value the target cannot hold (that row fails: the
table is rolled back and the message names the row and the column, never NULL in its place, Q3). Pure."""

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..adapters.columns import TableColumn, TypeFamily
from ..adapters.families import NamedType, of_name
from ..domain.data_jobs import ENGINE_NAMES
from ..domain.dialect import Dialect
from ..domain.edits import EditKind
from ..domain.profiles import Engine
from .lexer import TokenKind, tokenize

ENGINES: dict[Dialect, Engine] = {
    Dialect.POSTGRES: Engine.POSTGRES, Dialect.MYSQL: Engine.MYSQL, Dialect.TSQL: Engine.MSSQL,
    Dialect.SQLITE: Engine.SQLITE,
}
_TEXTUAL: frozenset[TypeFamily] = frozenset({"char", "varchar", "text", "json", "xml", "enum", "set", "uuid",
                                            "interval"})
_DATED: frozenset[TypeFamily] = frozenset({"date", "timestamp", "timestamp_tz"})
_NUMERIC: frozenset[TypeFamily] = frozenset({"int16", "int32", "int64", "uint64", "decimal", "money", "float32",
                                            "float64", "year"})
# Where NaN or ±infinity can come from: PostgreSQL's floats and numerics hold all three, SQLite's REAL ±infinity
_NON_FINITE: frozenset[TypeFamily] = frozenset({"float32", "float64", "decimal", "other"})
_UNMAPPABLE: frozenset[TypeFamily] = frozenset({"spatial", "variant"})
_MYSQL_DECIMAL = (65, 30)  # MySQL's widest DECIMAL: digits, digits after the point
_MSSQL_DECIMAL = 38
_MYSQL_CHAR = 255  # CHAR(n) up to 255; wider is VARCHAR
_MYSQL_VARCHAR = 16_383  # VARCHAR(n) of utf8mb4 that fits MySQL's 65,535-byte row (4 bytes a character)
_NVARCHAR = 4_000  # nvarchar(n) / nchar(n) up to 4,000 UTF-16 code units; wider is (max)
_VARCHAR = 8_000  # varchar(n) / char(n) up to 8,000 bytes; wider is (max)
_BINARY = 8_000  # varbinary(n) up to 8,000 bytes; wider is (max) / LONGBLOB
DIGITS_AS_TEXT = "stored as text, so that every digit is kept (SQLite would make it a number of 15 digits)"
# What PostgreSQL and MySQL do with the 7th digit of a SQL Server time, datetime2 or datetimeoffset: round it, with no
# warning of their own (I1); the copy keeps all seven as text up to the server, which then rounds
FRACTION_NOTE = "Fractions beyond 6 digits are rounded (the source keeps 7)"
_FRACTION_DIGITS = 6  # PostgreSQL's and MySQL's widest fraction of a second
_FRACTIONAL: frozenset[TypeFamily] = frozenset({"time", "timestamp", "timestamp_tz"})


def engine_name(dialect: Dialect) -> str:
    return ENGINE_NAMES[ENGINES[dialect]]


@dataclass(frozen=True, slots=True)
class Mapped:
    """A new table's column type on the target, or none (`problem`)."""

    type_sql: str | None
    note: str | None = None  # ⚠: what the preview says of this mapping
    problem: str | None = None


def unmappable(column: TableColumn, target: Dialect) -> str:
    """Spec §4 `unmappable_type`."""
    return f"{column.name} ({column.type_name}) has no {engine_name(target)} type: choose one, or leave the column out"


def _decimal(column: TableColumn, target: Dialect) -> Mapped:
    precision, scale = column.precision, column.scale or 0
    if target is Dialect.SQLITE:
        return Mapped("TEXT", DIGITS_AS_TEXT)
    if precision is None:  # PostgreSQL's numeric without a precision, MySQL's DECIMAL of a query's column
        return {
            Dialect.POSTGRES: Mapped("numeric"),
            Dialect.MYSQL: Mapped("decimal(65,30)", "more than 30 digits after the point are rounded, more than 35 "
                                                    "before it fail"),
            Dialect.TSQL: Mapped("decimal(38,10)", "more than 10 digits after the point are rounded, more than 28 "
                                                   "before it fail"),
        }[target]
    if target is not Dialect.POSTGRES and not 0 <= scale <= precision:  # numeric(5,-2), numeric(3,5): PostgreSQL 15
        return Mapped(None, problem=unmappable(column, target))
    if target is Dialect.MYSQL and (precision > _MYSQL_DECIMAL[0] or scale > _MYSQL_DECIMAL[1]):
        return Mapped(None, problem=unmappable(column, target))
    if target is Dialect.TSQL and precision > _MSSQL_DECIMAL:
        return Mapped(None, problem=unmappable(column, target))
    return Mapped(f"{'numeric' if target is Dialect.POSTGRES else 'decimal'}({precision},{scale})")


def _text(target: Dialect) -> str:
    return {Dialect.POSTGRES: "text", Dialect.MYSQL: "longtext", Dialect.TSQL: "nvarchar(max)",
            Dialect.SQLITE: "TEXT"}[target]


def _char(column: TableColumn, source: Dialect, target: Dialect, varying: bool) -> Mapped:
    n = column.length
    if n is None or target is Dialect.SQLITE:
        return Mapped(_text(target))
    if target is Dialect.POSTGRES:
        return Mapped(f"{'varchar' if varying else 'char'}({n})")
    if target is Dialect.MYSQL:
        if not varying and n <= _MYSQL_CHAR:
            return Mapped(f"char({n})")
        return Mapped(f"varchar({n})" if n <= _MYSQL_VARCHAR else "longtext")
    if source is Dialect.TSQL:  # an alias type of the same engine: n is already SQL Server's own unit, kept as it is
        unit, widest = ("n", _NVARCHAR) if column.unicode else ("", _VARCHAR)  # UTF-16 units of an nvarchar, bytes
        if not varying:
            return Mapped(f"{unit}char({n})")
        return Mapped(f"{unit}varchar({n})" if n <= widest else f"{unit}varchar(max)")
    if not varying:
        return Mapped(f"nchar({n})" if n <= _NVARCHAR else "nvarchar(max)")
    units = 2 * n  # nvarchar counts UTF-16 code units: a character beyond the BMP (an emoji) takes two
    shown = f"nvarchar({units})" if units <= _NVARCHAR else "nvarchar(max)"
    return Mapped(shown, f"nvarchar counts UTF-16 units: {shown} holds {n:,} characters even when each takes two")


def _binary(column: TableColumn, target: Dialect) -> Mapped:
    n = column.length
    if target is Dialect.POSTGRES:
        return Mapped("bytea")
    if target is Dialect.SQLITE:
        return Mapped("BLOB")
    if target is Dialect.MYSQL:
        return Mapped(f"varbinary({n})" if n is not None and n <= _BINARY else "longblob")
    return Mapped(f"varbinary({n})" if n is not None and n <= _BINARY else "varbinary(max)")


_PLAIN: dict[TypeFamily, dict[Dialect, str]] = {
    "bool": {Dialect.POSTGRES: "boolean", Dialect.MYSQL: "tinyint(1)", Dialect.TSQL: "bit", Dialect.SQLITE: "INTEGER"},
    "int16": {Dialect.POSTGRES: "smallint", Dialect.MYSQL: "smallint", Dialect.TSQL: "smallint",
              Dialect.SQLITE: "INTEGER"},
    "int32": {Dialect.POSTGRES: "integer", Dialect.MYSQL: "int", Dialect.TSQL: "int", Dialect.SQLITE: "INTEGER"},
    "int64": {Dialect.POSTGRES: "bigint", Dialect.MYSQL: "bigint", Dialect.TSQL: "bigint", Dialect.SQLITE: "INTEGER"},
    "uint64": {Dialect.POSTGRES: "numeric(20)", Dialect.MYSQL: "bigint unsigned", Dialect.TSQL: "decimal(20,0)",
               Dialect.SQLITE: "TEXT"},
    "money": {Dialect.POSTGRES: "numeric(19,4)", Dialect.MYSQL: "decimal(19,4)", Dialect.TSQL: "money",
              Dialect.SQLITE: "TEXT"},
    "float32": {Dialect.POSTGRES: "real", Dialect.MYSQL: "float", Dialect.TSQL: "real", Dialect.SQLITE: "REAL"},
    "float64": {Dialect.POSTGRES: "double precision", Dialect.MYSQL: "double", Dialect.TSQL: "float",
                Dialect.SQLITE: "REAL"},
    "date": {Dialect.POSTGRES: "date", Dialect.MYSQL: "date", Dialect.TSQL: "date", Dialect.SQLITE: "TEXT"},
    "time": {Dialect.POSTGRES: "time", Dialect.MYSQL: "time(6)", Dialect.TSQL: "time(7)", Dialect.SQLITE: "TEXT"},
    "duration": {Dialect.POSTGRES: "interval", Dialect.MYSQL: "time(6)", Dialect.TSQL: "time(7)",
                 Dialect.SQLITE: "TEXT"},
    "year": {Dialect.POSTGRES: "smallint", Dialect.MYSQL: "year", Dialect.TSQL: "smallint", Dialect.SQLITE: "INTEGER"},
    "timestamp": {Dialect.POSTGRES: "timestamp", Dialect.MYSQL: "datetime(6)", Dialect.TSQL: "datetime2(7)",
                  Dialect.SQLITE: "TEXT"},
    "timestamp_tz": {Dialect.POSTGRES: "timestamptz", Dialect.MYSQL: "datetime(6)", Dialect.TSQL: "datetimeoffset(7)",
                     Dialect.SQLITE: "TEXT"},
    "time_tz": {Dialect.POSTGRES: "timetz", Dialect.MYSQL: "time(6)", Dialect.TSQL: "time(7)", Dialect.SQLITE: "TEXT"},
    "interval": {Dialect.POSTGRES: "interval", Dialect.MYSQL: "varchar(64)", Dialect.TSQL: "nvarchar(64)",
                 Dialect.SQLITE: "TEXT"},
    "uuid": {Dialect.POSTGRES: "uuid", Dialect.MYSQL: "char(36)", Dialect.TSQL: "uniqueidentifier",
             Dialect.SQLITE: "TEXT"},
    "xml": {Dialect.POSTGRES: "xml", Dialect.MYSQL: "longtext", Dialect.TSQL: "xml", Dialect.SQLITE: "TEXT"},
}
_NOTES: dict[tuple[TypeFamily, Dialect], str] = {
    ("uint64", Dialect.SQLITE): DIGITS_AS_TEXT,
    ("duration", Dialect.TSQL): "a value of 24 hours or more, or a negative one, fails its row",
    ("timestamp_tz", Dialect.MYSQL): "converted to UTC: the offset is not kept",
    ("time_tz", Dialect.MYSQL): "the offset is dropped",
    ("time_tz", Dialect.TSQL): "the offset is dropped",
    ("interval", Dialect.MYSQL): "PostgreSQL's text of the interval",
    ("interval", Dialect.TSQL): "PostgreSQL's text of the interval",
}
_AS_TEXT_NOTE = "PostgreSQL's text of the value"  # an array, a range, an inet … on another engine
_JSONB_NOTE = "jsonb orders the keys and keeps the last of duplicate keys"
# Probed (06c Task 1 re-review N2): integers up to uint64 stay exact; a decimal is rewritten (1.10 as 1.1), and so is an
# integer beyond 64 bits, as a double
MYSQL_JSON_NOTE = ("MySQL's JSON orders the keys, keeps one of duplicate keys, and rewrites decimals (1.10 becomes "
                   "1.1) and integers beyond 64 bits as doubles: choose longtext to keep the text")


def _json(column: TableColumn, source: Dialect, target: Dialect) -> Mapped:
    if target is Dialect.POSTGRES:
        if source is Dialect.POSTGRES and column.type_name in ("json", "jsonb"):
            return Mapped(column.type_name)
        return Mapped("jsonb", _JSONB_NOTE)  # another engine's JSON, or a domain over json (its base is not kept)
    if target is Dialect.MYSQL:
        return Mapped("json", MYSQL_JSON_NOTE if source is not Dialect.MYSQL else None)
    return Mapped({Dialect.TSQL: "nvarchar(max)", Dialect.SQLITE: "TEXT"}[target])


def _normalised_json(target: TableColumn, dialect: Dialect) -> str | None:
    """What a JSON column the server normalises does to another engine's value (MySQL's JSON, PostgreSQL's jsonb), the
    note `_json` gives a new table's column, for an existing column or a type the user chose too; None for any other."""
    if dialect is Dialect.MYSQL and target.family == "json":
        return MYSQL_JSON_NOTE
    if dialect is Dialect.POSTGRES and target.type_name.strip().lower() == "jsonb":
        return _JSONB_NOTE
    return None


def _same_engine_custom(column: TableColumn, target: Dialect) -> Mapped:
    """A type of the source database's own, on the same engine: its family's type (a domain's base), or text."""
    mapped = _cross(column, target, target)
    if mapped.type_sql is None:
        return mapped
    why = f"{column.type_name} is the source database's own type: the new column is {mapped.type_sql}"
    return Mapped(mapped.type_sql, "; ".join(filter(None, (why, mapped.note))), mapped.problem)


def _cross(column: TableColumn, source: Dialect, target: Dialect) -> Mapped:
    family = column.family
    if family in _UNMAPPABLE:
        return Mapped(None, problem=unmappable(column, target))
    if family == "decimal":
        return _decimal(column, target)
    if family in ("char", "varchar"):
        return _char(column, source, target, varying=family == "varchar")
    if family == "binary":
        return _binary(column, target)
    if family == "json":
        return _json(column, source, target)
    if family in ("enum", "set"):
        return Mapped({Dialect.POSTGRES: "text", Dialect.MYSQL: "varchar(255)", Dialect.TSQL: "nvarchar(255)",
                       Dialect.SQLITE: "TEXT"}[target])
    if family in _PLAIN:
        return Mapped(_PLAIN[family][target], _NOTES.get((family, target)))
    # text, a PostgreSQL row type, an array, a range, SQLite's untyped column …: the engine's text
    note = _AS_TEXT_NOTE if source is Dialect.POSTGRES and family == "other" and target is not Dialect.SQLITE else None
    return Mapped(_text(target), note)


def target_type(column: TableColumn, source: Dialect, target: Dialect) -> Mapped:
    """The type a new table's column gets for `column` (spec §3.5.2). Same engine: the declared type, unless it is the
    database's own (a domain, an enum, an extension's or an alias type: its family's type then, with a note); spatial
    types keep theirs. Another engine: by family. PostgreSQL's money stays money, a domain over it too."""
    if column.family == "money" and source is Dialect.POSTGRES and target is Dialect.POSTGRES:
        return Mapped("money")
    if source is target:
        if column.custom_type and column.family != "spatial":
            return _same_engine_custom(column, target)
        return Mapped(column.type_name)  # SQLite's untyped column too: none ("")
    return _cross(column, source, target)


_KINDS: dict[TypeFamily, EditKind] = {
    **dict.fromkeys(("int16", "int32", "int64", "uint64", "decimal", "money", "float32", "float64"), "number"),
    **dict.fromkeys(("date", "time", "timestamp", "timestamp_tz", "time_tz", "duration", "year"), "datetime"),
    "bool": "bool", "binary": "binary", "json": "json",
}


def planned_column(name: str, type_sql: str, dialect: Dialect) -> TableColumn:
    """A new table's column as its catalog will read it — its family and kind from the type's text (MySQL's
    tinyint(1) is read as a number) — what the preview computes its conversions against. The copy itself reads the
    real column after its CREATE TABLE."""
    named = of_name(dialect, type_sql)
    kind = _KINDS.get(named.family, "text")
    if named.family == "bool" and dialect is Dialect.MYSQL:
        kind = "number"
    return TableColumn(name=name, type_name=type_sql, family=named.family, nullable=True, has_default=False,
                       kind=kind, compare="none", writable=True, precision=named.precision, scale=named.scale,
                       length=named.length)


# What is not bytes: a binary column takes only bytes (a server would store a number's or a date's own bytes, silently:
# SQL Server's int 5 into varbinary is 0x00000005)
_NOT_BYTES: frozenset[TypeFamily] = _TEXTUAL | _NUMERIC | _DATED | frozenset({"time", "time_tz", "duration", "bool",
                                                                              "composite", "other"})


def incompatible(source: TableColumn, target: TableColumn) -> str | None:
    """Why a source column's values cannot go into an existing target column at all (spec §3.5.3: text and binary
    data are not converted into each other, and nothing else is read as bytes), or None."""
    if source.family == "binary" and target.value_kind not in ("binary", "other"):
        return f"{source.name} holds binary data, which does not go into {target.name} ({target.type_name})"
    if target.value_kind == "binary" and source.family in _TEXTUAL:
        return f"{source.name} holds text, which does not go into {target.name} ({target.type_name}), a binary column"
    if target.value_kind == "binary" and source.family in _NOT_BYTES:
        return (f"{source.name} ({source.type_name}) does not go into {target.name} ({target.type_name}), a binary "
                f"column: only binary data does")
    return None


TYPE_CHARS = 200  # spec §3.5.1: the longest type a column of a new table may be given
# Words that make a column definition more than its type (a constraint, a default, a generated value): refused outside
# parentheses, so a typed type cannot add to the CREATE TABLE what its preview does not say it is
_NOT_A_TYPE = frozenset({"NOT", "NULL", "DEFAULT", "PRIMARY", "KEY", "REFERENCES", "CHECK", "CONSTRAINT", "UNIQUE",
                         "GENERATED", "AS", "IDENTITY", "AUTO_INCREMENT", "AUTOINCREMENT", "ON", "COMMENT"})


def type_problem(dialect: Dialect, text: str) -> str | None:
    """Why `text` is not one type for a new table's column (spec §4 `invalid_type`: `Not a single type: <reason>`),
    or None. The lexer reads it as the filter's check does (`select_builder._single_expression`): every quote and
    comment closed, no `;`, no comment at all (a MySQL executable one would run), parentheses balanced, and no comma or
    constraint word outside them."""
    stripped = text.strip()
    if not stripped:
        return "it is empty"
    if len(stripped) > TYPE_CHARS:
        return f"it is longer than {TYPE_CHARS} characters"
    depth = 0
    words = 0
    for token in tokenize(stripped, dialect):
        if not token.terminated:
            return "it has an unterminated string, quoted name or comment"
        if token.kind in (TokenKind.COMMENT, TokenKind.EXEC_COMMENT):
            return "it has a comment"
        if token.kind in (TokenKind.SEMI, TokenKind.DELIMITER):
            return "it has a ;"
        if token.kind is TokenKind.LPAREN:
            depth += 1
        elif token.kind is TokenKind.RPAREN:
            depth -= 1
            if depth < 0:
                return "it closes a parenthesis it did not open"
        elif depth == 0 and token.kind is TokenKind.COMMA:
            return "it has a comma outside parentheses"
        elif depth == 0 and token.kind is TokenKind.WORD:
            if token.upper in _NOT_A_TYPE:
                return f"{token.text} is not part of a type"
            words += 1
        elif depth == 0 and token.kind is TokenKind.QUOTED:  # "Mood", [dbo].[Alias], `t`: a type's own name
            words += 1
    if depth:
        return "it leaves a parenthesis open"
    return None if words else "it does not name a type"


# ---- a new table's primary key ----

# The bytes a primary key's columns take together (06c Task 2 fix I2, fix round 2). MySQL's InnoDB index is a hard
# limit (utf8mb4: 4 bytes a character): a wider CREATE TABLE is refused (1071). SQL Server's clustered index keys 900
# bytes of each row: a key declared wider is created (with a warning) and only a row whose key is longer fails (N2), so
# there only a type it cannot key at all is replaced. PostgreSQL and SQLite key every type a copy gives a key column.
KEY_BYTES: dict[Dialect, int] = {Dialect.MYSQL: 3_072, Dialect.TSQL: 900}
_MSSQL_KEY_TYPES = ("nvarchar(450)", "varchar(900)", "varbinary(900)")  # Unicode text, other text, binary: 900 bytes
# The bytes MySQL's key takes of a fixed-size type's column, at most (decimal by its digits): only MySQL's limit is a sum
_KEY_WIDTHS: dict[TypeFamily, int] = {
    "bool": 1, "int16": 2, "int32": 4, "int64": 8, "uint64": 8, "year": 2, "money": 8, "float32": 4, "float64": 8,
    "date": 3, "time": 6, "duration": 6, "time_tz": 6, "timestamp": 8, "timestamp_tz": 10, "uuid": 16, "enum": 2,
    "set": 8,
}
_DECIMAL_LEFTOVER = (0, 1, 1, 2, 2, 3, 3, 4, 4)  # MySQL: the bytes of 0–8 digits beyond each 9 (which take 4)
_STRING = frozenset({"char", "varchar", "text"})
_MYSQL_CHAR_BYTES = 4  # utf8mb4


def _decimal_bytes(precision: int | None, scale: int | None) -> int:
    if precision is None:
        return 30  # decimal(65,30), MySQL's widest
    whole, fraction = precision - (scale or 0), scale or 0
    return sum(part // 9 * 4 + _DECIMAL_LEFTOVER[part % 9] for part in (whole, fraction))


def _unkeyable(dialect: Dialect, named: NamedType) -> bool:
    """A type of a family the engine never keys (fix round 2, N1): MySQL's text, blob and json; SQL Server's max types,
    text, ntext, image and xml. A type no rule reads (`other`: a COLLATE, a CHARACTER SET, brackets, zerofill) is not
    one: the server decides at CREATE."""
    if named.family == "text" or (named.family == "binary" and named.length is None):
        return True
    return named.family == ("json" if dialect is Dialect.MYSQL else "xml")


def _mysql_key_width(named: NamedType) -> int:
    """The bytes MySQL's key takes of a keyable column: 0 for a type no rule reads (the server decides)."""
    if named.family in ("char", "varchar"):
        return _MYSQL_CHAR_BYTES * (named.length or 0)
    if named.family == "binary":
        return named.length or 0
    if named.family == "decimal":
        return _decimal_bytes(named.precision, named.scale)
    return _KEY_WIDTHS.get(named.family, 0)


@dataclass(frozen=True, slots=True)
class KeyPlan:
    """A new table's primary key on the target (06c fix I2): each key column's type in the key's order — its own, or a
    bounded one where the engine cannot key its own (`notes` says so) — or, with `reason`, none at all."""

    types: tuple[str, ...] = ()
    notes: tuple[str | None, ...] = ()
    reason: str | None = None


def _bounded_note(column: TableColumn, type_sql: str, dialect: Dialect) -> str:
    return (f"Key column {column.name} becomes {type_sql}: {engine_name(dialect)} keys at most "
            f"{KEY_BYTES[dialect]:,} bytes; longer values fail their row")


def primary_key(columns: Sequence[tuple[TableColumn, str, bool]], dialect: Dialect) -> KeyPlan:
    """The source's primary key as a new table on `dialect` can have it: `columns` are the key's source columns, each
    with the type the new table gives it and whether the user chose that type, which is never replaced.

    - SQL Server: a text column of a type it cannot key (nvarchar(max), varchar(max)) becomes nvarchar(450) or
      varchar(900), a binary one varbinary(900), each with a note; every other type stays as it is (N2).
    - MySQL: its text columns (char, varchar, text) share the bytes the other key columns leave (N3): each keeps its
      declared length when it fits in floor((3,072 − the others' bytes) / (4 × their number)) characters, else becomes
      a varchar of that many, with a note; an unbounded binary column takes the same share of bytes as a varbinary.
    - A column of a type the engine never keys that is neither (json, xml, PostgreSQL's text of a value, a type the user
      chose), or a MySQL key with no room left for a text column, gives no key: `reason` says why. A type no rule reads
      keeps its key, with no note (N1)."""
    limit = KEY_BYTES.get(dialect)
    if limit is None:
        return KeyPlan(tuple(type_sql for _, type_sql, _ in columns), (None,) * len(columns))
    name = engine_name(dialect)
    types: list[str] = []
    notes: list[str | None] = []
    shared: list[tuple[int, TableColumn, NamedType]] = []  # MySQL: its place, the column, its type as read
    fixed = 0
    for column, type_sql, chosen in columns:
        named = of_name(dialect, type_sql)
        string = column.family in _STRING and named.family in _STRING
        bounds = not chosen and (string or (column.family == "binary" and named.family == "binary"))
        if _unkeyable(dialect, named) and not bounds:
            return KeyPlan(reason=f"{column.name} would be {type_sql}, which {name} can't key")
        types.append(type_sql)
        notes.append(None)
        if dialect is Dialect.TSQL:
            if _unkeyable(dialect, named):  # a max type: the bounded type of its kind
                unicode, other, binary = _MSSQL_KEY_TYPES
                kept = binary if named.family == "binary" else unicode if type_sql.strip().lower().startswith("n") \
                    else other
                types[-1], notes[-1] = kept, _bounded_note(column, kept, dialect)
        elif bounds and (string or _unkeyable(dialect, named)):  # MySQL: shares the bytes left (N3)
            shared.append((len(types) - 1, column, named))
        else:
            fixed += _mysql_key_width(named)
    if dialect is Dialect.TSQL or not shared and fixed <= limit:
        return KeyPlan(tuple(types), tuple(notes))
    share = (limit - fixed) // len(shared) if shared and fixed < limit else 0
    if fixed > limit or share < (_MYSQL_CHAR_BYTES if any(n.family in _STRING for _, _, n in shared) else 1):
        return KeyPlan(reason=f"its columns would take more than the {limit:,} bytes {name} keys")
    for at, column, named in shared:
        if named.family == "binary":
            kept = f"varbinary({share})"
        elif named.length is not None and named.length <= share // _MYSQL_CHAR_BYTES:
            continue  # its declared length fits: kept, no note
        else:
            kept = f"varchar({share // _MYSQL_CHAR_BYTES})"
        types[at], notes[at] = kept, _bounded_note(column, kept, dialect)
    return KeyPlan(tuple(types), tuple(notes))


# ---- values ----

class RowFailure(Exception):
    """A value its target column cannot hold: its row fails (the table is rolled back). The reason never quotes the
    value beyond a special word (`infinity`, a zero date, a number that is neither 0 nor 1)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


Convert = Callable[[Any], Any]

_PG_SPECIAL = frozenset({"infinity", "-infinity"})
_ZERO_DATE = re.compile(r"(?:0000-\d\d-\d\d|\d{4}-00-\d\d|\d{4}-\d\d-00)(?:$|[ T])", re.ASCII)
_NAN = frozenset({"nan", "infinity", "-infinity", "inf", "-inf"})
# A date and time with an offset, as PostgreSQL (`+08`, `+05:30`, `+08:05:43`, ` BC`), SQL Server (`+08:00`) and an ISO
# text (`Z`) write it; any number of fractional digits (SQL Server's 7th) is kept as text, never rounded
_WITH_OFFSET = re.compile(
    r"(?P<date>\d{4,})-(?P<month>\d{2})-(?P<day>\d{2})(?P<sep>[ T])(?P<hour>\d{2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2}))?(?P<fraction>\.\d+)?\s*(?P<offset>Z|[+-]\d{2}(?::?\d{2}(?::?\d{2})?)?)(?P<bc> BC)?",
    re.ASCII,
)
_TIME_WITH_OFFSET = re.compile(r"(?P<time>\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)\s*(?P<offset>Z|[+-]\d{2}(?::?\d{2})*)",
                               re.ASCII)
_DURATION = re.compile(r"(?P<sign>-)?(?P<hours>\d+):(?P<minutes>\d{2})(?::(?P<seconds>\d{2})(?:\.\d+)?)?", re.ASCII)
_DAY_HOURS = 24


def _offset(text: str) -> timedelta:
    if text == "Z":
        return timedelta(0)
    digits = text[1:].replace(":", "")
    hours, minutes, seconds = int(digits[:2]), int(digits[2:4] or 0), int(digits[4:6] or 0)
    delta = timedelta(hours=hours, minutes=minutes, seconds=seconds)
    return -delta if text[0] == "-" else delta


def _parts(value: str) -> re.Match[str]:
    found = _WITH_OFFSET.fullmatch(value.strip())
    if found is None:
        raise RowFailure("is not a date and time with an offset")
    if found.group("bc"):
        raise RowFailure("is a date before the year 1 (BC)")
    return found


def _utc(value: str) -> tuple[datetime, str]:
    """The UTC wall time of a date and time with an offset (no fraction), and its fraction's text."""
    found = _parts(value)
    try:
        local = datetime(int(found["date"]), int(found["month"]), int(found["day"]), int(found["hour"]),
                         int(found["minute"]), int(found["second"] or 0))
        return local - _offset(found["offset"]), found["fraction"] or ""
    except (ValueError, OverflowError):
        raise RowFailure("is outside the years 1 to 9999 in UTC") from None


def to_utc(value: str) -> str:
    """`2026-10-05 12:34:56.1234567+08:00` → `2026-10-05 04:34:56.1234567` (spec §3.5.3: a time zone's value into a
    column without one is converted to UTC; never by the server, which drops the offset, A S10)."""
    when, fraction = _utc(value)
    return when.isoformat(sep=" ") + fraction


def with_minute_offset(value: str) -> str:
    """The offset as ±HH:MM (MySQL and SQL Server refuse PostgreSQL's `+00`, A X5, S10); an offset with seconds (an old
    local mean time) has none, so the value goes in UTC, `+00:00`, the same instant."""
    found = _parts(value)
    offset = found["offset"]
    digits = offset[1:].replace(":", "") if offset != "Z" else "0000"
    if len(digits) > 4 and int(digits[4:]):
        when, fraction = _utc(value)
        return when.isoformat(sep=found["sep"]) + fraction + "+00:00"
    sign = "+" if offset == "Z" else offset[0]
    head = value.strip()[:found.start("offset")].rstrip()
    return f"{head}{sign}{digits[:2]}:{digits[2:4] or '00'}"


def _no_pg_special(target: str) -> Convert:
    def check(value: Any) -> Any:
        if isinstance(value, str) and (value.lower() in _PG_SPECIAL or value.endswith(" BC")):
            what = f"'{value.lower()}'" if value.lower() in _PG_SPECIAL else "a date before the year 1 (BC)"
            raise RowFailure(f"is {what}, which {target} cannot hold")
        return value
    return check


def _no_zero_date(target: str) -> Convert:
    def check(value: Any) -> Any:
        if isinstance(value, str) and _ZERO_DATE.match(value):
            raise RowFailure(f"is a date with a zero part (MySQL's 0000-00-00), which {target} cannot hold")
        return value
    return check


def _finite_number(target: str) -> Convert:
    def check(value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value) or (
                isinstance(value, str) and value.strip().lower() in _NAN):
            raise RowFailure(f"is not a finite number (NaN or infinity), which {target} cannot hold")
        return value
    return check


def _not_nan(value: Any) -> Any:
    """SQLite holds ±infinity (written 9e999) but stores NaN as NULL: the row fails instead (§3.5.3, Q3)."""
    if isinstance(value, float) and math.isnan(value) or isinstance(value, str) and value.strip().lower() == "nan":
        raise RowFailure("is NaN, which SQLite cannot hold (it would store NULL)")
    return value


def _within_a_day(value: Any) -> Any:
    if isinstance(value, str) and (found := _DURATION.fullmatch(value.strip())):
        if found["sign"] or int(found["hours"]) >= _DAY_HOURS:
            raise RowFailure("is a time of 24 hours or more, or negative, which SQL Server's time cannot hold")
    return value


def _drop_offset(value: Any) -> Any:
    if isinstance(value, str) and (found := _TIME_WITH_OFFSET.fullmatch(value.strip())):
        return found["time"]
    return value


def _utc_text(value: Any) -> Any:
    return to_utc(value) if isinstance(value, str) else value


def _minute_offset_text(value: Any) -> Any:
    return with_minute_offset(value) if isinstance(value, str) else value


def _time_offset_text(value: Any) -> Any:
    """A time with an offset as text: `+05` → `+05:00`."""
    if isinstance(value, str) and (found := _TIME_WITH_OFFSET.fullmatch(value.strip())):
        offset = found["offset"]
        if offset == "Z":
            return found["time"] + "+00:00"
        digits = offset[1:].replace(":", "")
        return f"{found['time']}{offset[0]}{digits[:2]}:{digits[2:4] or '00'}"
    return value


def _to_bool(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, int):
        return value  # a bool, or text the server reads itself ('t', 'true')
    if value in (0, 1):
        return bool(value)
    raise RowFailure(f"holds {value}, which is neither 0 nor 1 (true or false)")


def _bool_to_int(value: Any) -> Any:
    return int(value) if isinstance(value, bool) else value


def _bool_text(value: Any) -> Any:
    """A bool column's value as text: `true` / `false`, also from the integer 0 or 1 a MySQL or SQLite bool column is
    read as; any other integer keeps its digits (a TINYINT(1) may hold 5)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return "false" if value == 0 else "true" if value == 1 else str(value)
    return value


def _number_text(value: Any) -> Any:
    """A number into a text column as its text (an INTEGER or REAL literal would be stored by SQLite as a number of 15
    digits, R-06-10): every digit kept."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _no_nul(target: str) -> Convert:
    def check(value: Any) -> Any:
        if isinstance(value, str) and "\x00" in value:
            raise RowFailure(f"holds a NUL character (U+0000), which {target} text cannot hold here")
        return value
    return check


def _no_binary(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise RowFailure("holds binary data, which does not go into a text column")
    return value


UTC_NOTE = "converted to UTC: the offset is not kept"
_PG_SPECIAL_NOTE = "PostgreSQL's infinity and dates before the year 1 fail their row"
_ZERO_DATE_NOTE = "MySQL's zero dates (0000-00-00) fail their row"
_DROP_OFFSET_NOTE = "the offset is dropped"
_DAY_NOTE = "a value of 24 hours or more, or a negative one, fails its row"
_TO_BOOL_NOTE = "0 and 1 as false and true; any other number fails its row"
_TO_INT_NOTE = "true and false as 1 and 0"
_BOOL_TEXT_NOTE = "true and false as text; other numbers as written"
_FINITE_NOTE = "NaN and infinity fail their row"
_NAN_NOTE = "NaN fails its row (SQLite would store NULL)"
_NUL_NOTE = "text holding a NUL character (U+0000) fails its row"


@dataclass(frozen=True)
class Conversion:
    """How a source column's values go into a target column: `convert` (None: as read) and what the preview says of
    it, one note per rule that changes or refuses a value."""

    convert: Convert | None
    notes: tuple[str, ...] = ()


def _chain(steps: list[Convert]) -> Convert | None:
    if not steps:
        return None
    if len(steps) == 1:
        only = steps[0]
        return lambda value: None if value is None else only(value)

    def convert(value: Any) -> Any:
        if value is None:
            return None
        for step in steps:
            value = step(value)
        return value
    return convert


def conversion(
    source: TableColumn, target: TableColumn, source_dialect: Dialect, target_dialect: Dialect, *,
    backslash_escapes: bool | None = None,
) -> Conversion:
    """How a value of `source` goes into `target` (spec §3.5.3). A value of the same engine's same kind of column goes
    as read: it came from that engine. `backslash_escapes` False: a MySQL target session with NO_BACKSLASH_ESCAPES,
    where no literal can write a NUL."""
    family, kind, into = source.family, target.value_kind, target.family
    if source_dialect is target_dialect and family == into and source.value_kind == kind:
        return Conversion(None)
    name = engine_name(target_dialect)
    steps: list[tuple[Convert, str | None]] = []
    if source_dialect is Dialect.POSTGRES and target_dialect is not Dialect.POSTGRES and family in _DATED:
        steps.append((_no_pg_special(name), _PG_SPECIAL_NOTE))
    if source_dialect is Dialect.MYSQL and target_dialect is not Dialect.MYSQL and family in ("date", "timestamp"):
        steps.append((_no_zero_date(name), _ZERO_DATE_NOTE))
    if family == "timestamp_tz":
        if into in ("timestamp", "date"):
            steps.append((_utc_text, UTC_NOTE))
        elif into == "timestamp_tz" or kind == "text":
            steps.append((_minute_offset_text, None))
    elif family == "time_tz":
        # into a time of day (MySQL's TIME is a duration's family): the offset dropped; into text, kept as ±HH:MM
        dropped = into in ("time", "duration")
        steps.append((_drop_offset, _DROP_OFFSET_NOTE) if dropped else (_time_offset_text, None))
    elif family == "duration" and target_dialect is Dialect.TSQL and into == "time":
        steps.append((_within_a_day, _DAY_NOTE))
    elif family == "bool":
        steps.append((_to_bool, _TO_BOOL_NOTE) if kind == "bool" else (_bool_to_int, _TO_INT_NOTE)
                     if kind == "number" else (_bool_text, _BOOL_TEXT_NOTE))
    numeric = family in _NUMERIC or family == "other"
    if kind == "number" and family in _NON_FINITE and source_dialect in (Dialect.POSTGRES, Dialect.SQLITE):
        if target_dialect in (Dialect.MYSQL, Dialect.TSQL):
            steps.append((_finite_number(name), _FINITE_NOTE))  # MySQL and SQL Server hold no NaN or infinity (06a S3)
        elif target_dialect is Dialect.SQLITE and source_dialect is Dialect.POSTGRES:
            steps.append((_not_nan, _NAN_NOTE))
    if kind in ("text", "json", "datetime"):
        if numeric:
            steps.append((_number_text, None))
        if family != "binary":
            steps.append((_no_binary, None))
        if target_dialect is Dialect.POSTGRES or (target_dialect is Dialect.MYSQL and backslash_escapes is False):
            said = source_dialect is not Dialect.POSTGRES and kind != "datetime"  # PostgreSQL's text holds none
            steps.append((_no_nul(name), _NUL_NOTE if said else None))
    notes = [note for _, note in steps if note]
    # Only a time or a date and time keeps a fraction to round: not a date, nor text, which holds the 7th digit as it is
    # (N1: by the kind, as MySQL's time(6) is the duration family)
    if (source.scale or 0) > _FRACTION_DIGITS and family in _FRACTIONAL and target_dialect in (
            Dialect.POSTGRES, Dialect.MYSQL) and kind == "datetime" and into != "date":
        notes.append(FRACTION_NOTE)  # the 7th digit goes up to the server as text, which rounds it (I1)
    if source_dialect is not target_dialect and (said := _normalised_json(target, target_dialect)) is not None:
        notes.append(said)  # whether the table is created or there already (06c Task 2 fix round 1)
    return Conversion(_chain([step for step, _ in steps]), tuple(dict.fromkeys(notes)))
