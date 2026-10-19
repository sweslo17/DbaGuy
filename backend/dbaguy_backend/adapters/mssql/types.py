"""SQL Server values as pymssql returns them, made lossless and displayable (spec §3.5). pymssql gives native Python
values; Decimal, datetime/date/time and UUID become text first (as the PostgreSQL and MySQL adapters load text), so
the grid, copy and "filter by value" show what SQL Server would write. A column's grid category comes from the server's
type name (`sys.dm_exec_describe_first_result_set`) or, for a result set the describe query did not cover, from its
first non-NULL value. Pure: no I/O.

Known limits (the driver's, probed): datetime2/datetimeoffset/time(7) arrive as microseconds (the 7th digit is lost),
and sql_variant arrives as raw bytes (the adapter tells the user to CAST it)."""

import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from ...domain.column_category import ColumnCategory


@dataclass(frozen=True, slots=True)
class ColumnKind:
    name: str  # what the grid header shows: the server's type name, or a guess
    category: ColumnCategory


_N, _B, _D, _T, _X = (ColumnCategory.NUMBER, ColumnCategory.BOOL, ColumnCategory.DATETIME, ColumnCategory.TEXT,
                      ColumnCategory.BINARY)
_CATEGORY_BY_TYPE: dict[str, ColumnCategory] = {
    **dict.fromkeys(("bigint", "int", "smallint", "tinyint", "decimal", "numeric", "money", "smallmoney", "float", "real"), _N),
    "bit": _B,
    **dict.fromkeys(("date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"), _D),
    **dict.fromkeys(("char", "varchar", "nchar", "nvarchar", "text", "ntext", "xml", "uniqueidentifier", "sysname"), _T),
    **dict.fromkeys(("binary", "varbinary", "image", "timestamp", "rowversion", "sql_variant", "hierarchyid", "geometry",
                     "geography"), _X),
    "json": ColumnCategory.JSON,
}
_GUESSED: dict[type, ColumnKind] = {  # exact types: bool is not taken for int, datetime is handled before date
    bool: ColumnKind("bit", _B), int: ColumnKind("int", _N), float: ColumnKind("float", _N),
    Decimal: ColumnKind("decimal", _N), str: ColumnKind("nvarchar", _T), bytes: ColumnKind("varbinary", _X),
    date: ColumnKind("date", _D), time: ColumnKind("time", _D), UUID: ColumnKind("uniqueidentifier", _T),
}
# pymssql's DB-API type codes for a column that is NULL in every looked-at row. Code 2 (BINARY) also covers dates and
# UUIDs, so it says nothing.
_BY_CODE = {1: ColumnKind("nvarchar", _T), 3: ColumnKind("int", _N), 4: ColumnKind("datetime", _D), 5: ColumnKind("decimal", _N)}
_UNKNOWN = ColumnKind("unknown", ColumnCategory.OTHER)


def category_for_type(base: str) -> ColumnCategory:
    """The grid category of a built-in type by its name (`decimal`, `nvarchar`, …)."""
    return _CATEGORY_BY_TYPE.get(base.lower(), ColumnCategory.OTHER)


def described_kind(system_type_name: str) -> ColumnKind:
    """From sys.dm_exec_describe_first_result_set's system_type_name, e.g. `decimal(38,10)`."""
    return ColumnKind(system_type_name, category_for_type(system_type_name.split("(", 1)[0].strip()))


def guessed_kinds(header: Sequence[tuple], rows: Sequence[Sequence[Any]]) -> list[ColumnKind]:
    """A result set the describe query did not cover: each column's first non-NULL value decides."""
    kinds: list[ColumnKind] = []
    for i, column in enumerate(header):
        value = next((row[i] for row in rows if row[i] is not None), None)
        if isinstance(value, datetime):
            kinds.append(ColumnKind("datetimeoffset" if value.tzinfo else "datetime2", _D))
        elif value is not None:
            kinds.append(_GUESSED.get(type(value), _UNKNOWN))
        else:
            kinds.append(_BY_CODE.get(column[1], _UNKNOWN))
    return kinds


def _timespec(microsecond: int) -> str:
    """As few fractional digits as the value needs: a `datetime` literal takes at most three (error 241)."""
    return "seconds" if microsecond == 0 else "milliseconds" if microsecond % 1000 == 0 else "microseconds"


_CONVERTERS: dict[type, Callable[[Any], Any]] = {
    Decimal: lambda v: format(v, "f"),  # plain digits: Decimal('0E-10') is 0.0000000000
    datetime: lambda v: v.isoformat(sep=" ", timespec=_timespec(v.microsecond)),  # keeps a datetimeoffset's offset
    date: date.isoformat,
    time: lambda v: v.isoformat(timespec=_timespec(v.microsecond)),
    UUID: lambda v: str(v).upper(),  # as SSMS shows it
}


def convert_row(row: Sequence[Any]) -> list[Any]:
    return [convert(value) if (convert := _CONVERTERS.get(type(value))) else value for value in row]


def _single(value: float) -> float:
    return struct.unpack("f", struct.pack("f", value))[0]


def shortest_real(value: float) -> float:
    """A `real` (float32) as the shortest decimal that reads back as the same float32: pymssql widens CAST(0.1 AS
    real) to 0.10000000149011612, SSMS shows 0.1."""
    single = _single(value)
    for digits in range(1, 10):  # nine significant digits always round-trip a float32
        if _single(shorter := float(f"{single:.{digits}g}")) == single:
            return shorter
    return single


def row_converter(kinds: Sequence[ColumnKind]) -> Callable[[Sequence[Any]], list[Any]]:
    """`convert_row`, plus `shortest_real` for the columns described as `real`."""
    reals = [i for i, kind in enumerate(kinds) if kind.name == "real"]
    if not reals:
        return convert_row

    def convert(row: Sequence[Any]) -> list[Any]:
        values = convert_row(row)
        for i in reals:
            if values[i] is not None:
                values[i] = shortest_real(values[i])
        return values

    return convert
