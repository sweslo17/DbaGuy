"""MySQL's text protocol, value by value: what each column type is called, its grid category, and how its raw bytes
become a lossless Python value (spec §3.5). Big unsigned integers stay exact, DECIMAL and dates stay text (so
`0000-00-00` and TIME beyond 24 h survive), binary columns stay bytes. Pure: no I/O."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from mysql.connector.constants import FieldFlag, FieldType

from ...domain.column_category import ColumnCategory
from ...domain.execution import ColumnMeta
from ..families import SPATIAL_TYPES  # the spatial types' names in information_schema.COLUMNS.DATA_TYPE

BINARY_CHARSET = 63  # MySQL's `binary` character set: BLOB/BINARY/VARBINARY hold bytes, not text


def _int(raw: bytes) -> int:
    return int(raw)


def _float(raw: bytes) -> float:
    return float(raw)


def _text(raw: bytes) -> str:
    return bytes(raw).decode("utf-8", errors="replace")  # the connection charset is utf8mb4; never raise on bad bytes


def _bytes(raw: bytes) -> bytes:
    return bytes(raw)


def _bit(raw: bytes) -> int:
    return int.from_bytes(raw, "big")


@dataclass(frozen=True, slots=True)
class ColumnKind:
    name: str
    category: ColumnCategory
    convert: Callable[[bytes], Any]


_INTEGERS = {FieldType.TINY: "tinyint", FieldType.SHORT: "smallint", FieldType.INT24: "mediumint",
             FieldType.LONG: "int", FieldType.LONGLONG: "bigint"}
_FIXED: dict[int, ColumnKind] = {
    **{t: ColumnKind(n, ColumnCategory.NUMBER, _int) for t, n in _INTEGERS.items()},
    FieldType.FLOAT: ColumnKind("float", ColumnCategory.NUMBER, _float),
    FieldType.DOUBLE: ColumnKind("double", ColumnCategory.NUMBER, _float),
    FieldType.DECIMAL: ColumnKind("decimal", ColumnCategory.NUMBER, _text),  # exact digits: text, like PG numeric
    FieldType.NEWDECIMAL: ColumnKind("decimal", ColumnCategory.NUMBER, _text),
    FieldType.BIT: ColumnKind("bit", ColumnCategory.NUMBER, _bit),
    FieldType.DATE: ColumnKind("date", ColumnCategory.DATETIME, _text),  # text keeps 0000-00-00
    FieldType.NEWDATE: ColumnKind("date", ColumnCategory.DATETIME, _text),
    FieldType.DATETIME: ColumnKind("datetime", ColumnCategory.DATETIME, _text),
    FieldType.TIMESTAMP: ColumnKind("timestamp", ColumnCategory.DATETIME, _text),
    FieldType.TIME: ColumnKind("time", ColumnCategory.DATETIME, _text),  # TIME can be negative or over 24 h
    FieldType.YEAR: ColumnKind("year", ColumnCategory.DATETIME, _text),
    FieldType.JSON: ColumnKind("json", ColumnCategory.JSON, _text),
    FieldType.GEOMETRY: ColumnKind("geometry", ColumnCategory.BINARY, _bytes),
    FieldType.VECTOR: ColumnKind("vector", ColumnCategory.BINARY, _bytes),
    FieldType.NULL: ColumnKind("null", ColumnCategory.OTHER, _text),
}
_STRINGS: dict[int, tuple[str, str]] = {  # (text name, binary name)
    FieldType.VARCHAR: ("varchar", "varbinary"), FieldType.VAR_STRING: ("varchar", "varbinary"),
    FieldType.STRING: ("char", "binary"), FieldType.TINY_BLOB: ("text", "blob"), FieldType.MEDIUM_BLOB: ("text", "blob"),
    FieldType.LONG_BLOB: ("text", "blob"), FieldType.BLOB: ("text", "blob"),
}

_BY_DATA_TYPE: dict[str, ColumnCategory] = {
    **{kind.name: kind.category for kind in _FIXED.values()},
    **{text: ColumnCategory.TEXT for text, _ in _STRINGS.values()},
    **{binary: ColumnCategory.BINARY for _, binary in _STRINGS.values()},
    **dict.fromkeys(("tinytext", "mediumtext", "longtext", "enum", "set"), ColumnCategory.TEXT),
    **dict.fromkeys(("tinyblob", "mediumblob", "longblob", *SPATIAL_TYPES), ColumnCategory.BINARY),
}


def category_for_data_type(data_type: str) -> ColumnCategory:
    """The grid category of a column by its information_schema DATA_TYPE (grid editing reads the catalog)."""
    return _BY_DATA_TYPE.get(data_type.lower(), ColumnCategory.OTHER)


def column_kind(description: tuple) -> ColumnKind:
    """description: a mysql-connector cursor description entry (name, type, …, flags at [7], charset at [8])."""
    field_type, flags, charset = description[1], description[7], description[8]
    if field_type in _STRINGS:
        text_name, binary_name = _STRINGS[field_type]
        if charset == BINARY_CHARSET:
            return ColumnKind(binary_name, ColumnCategory.BINARY, _bytes)
        if flags & FieldFlag.ENUM:
            return ColumnKind("enum", ColumnCategory.TEXT, _text)
        if flags & FieldFlag.SET:
            return ColumnKind("set", ColumnCategory.TEXT, _text)
        return ColumnKind(text_name, ColumnCategory.TEXT, _text)
    kind = _FIXED.get(field_type) or ColumnKind(f"type {field_type}", ColumnCategory.OTHER, _text)
    if field_type in _INTEGERS and flags & FieldFlag.UNSIGNED:
        return ColumnKind(f"{kind.name} unsigned", kind.category, kind.convert)
    return kind


def column_meta(description: tuple) -> ColumnMeta:
    kind = column_kind(description)
    return ColumnMeta(name=description[0], type_name=kind.name, category=kind.category)


def row_converter(descriptions: Sequence[tuple]) -> Callable[[Sequence[bytes | None]], list[Any]]:
    """One function per result set that turns a raw text-protocol row (bytes or None per cell) into Python values."""
    converters = [column_kind(d).convert for d in descriptions]
    return lambda row: [None if value is None else convert(value) for convert, value in zip(converters, row)]
