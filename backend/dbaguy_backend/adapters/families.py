"""What a column holds whatever its engine calls it (spec ⑥ §3.5.2, `TypeFamily`): each engine's rule, from what its
catalog says (`<engine>/columns.py` reads it) or from a type's text — a query result's column (`ColumnMeta.type_name`)
or a type the user typed for a copy's new table (`of_name`). One rule per engine, used both ways (06c). Pure."""

import re
from dataclasses import dataclass

from ..domain.column_category import ColumnCategory
from ..domain.dialect import Dialect
from .columns import TypeFamily

# ---- PostgreSQL: pg_type.typname of the column's type (a domain read through to its base) ----

PG_FAMILIES: dict[str, TypeFamily] = {
    "bool": "bool", "int2": "int16", "int4": "int32", "int8": "int64", "numeric": "decimal", "money": "money",
    "float4": "float32", "float8": "float64", "bpchar": "char", "varchar": "varchar", "text": "text", "citext": "text",
    "name": "text", "bytea": "binary", "date": "date", "time": "time", "timetz": "time_tz", "timestamp": "timestamp",
    "timestamptz": "timestamp_tz", "interval": "interval", "uuid": "uuid", "json": "json", "jsonb": "json",
    "xml": "xml", "geometry": "spatial", "geography": "spatial",
}
# The SQL names PostgreSQL also takes for them (format_type writes these; a typed type may use either)
_PG_ALIASES = {
    "boolean": "bool", "smallint": "int2", "integer": "int4", "int": "int4", "bigint": "int8", "decimal": "numeric",
    "real": "float4", "float": "float8", "double precision": "float8", "character": "bpchar", "char": "bpchar",
    "character varying": "varchar", "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz", "time without time zone": "time", "time with time zone": "timetz",
    "smallserial": "int2", "serial": "int4", "bigserial": "int8",
}


def pg_family(base: str, typtype: str = "b", category: str = "") -> TypeFamily:
    """`typtype` e: an enum, c: a composite (row) type; `category` A (typcategory): an array, PostgreSQL's text."""
    if typtype == "e":
        return "enum"
    if typtype == "c":
        return "composite"
    if category == "A":
        return "other"
    return PG_FAMILIES.get(base, "other")


# ---- MySQL: information_schema.COLUMNS.DATA_TYPE, with COLUMN_TYPE for unsigned and tinyint(1) ----

# The spatial types' names in information_schema.COLUMNS.DATA_TYPE (the protocol calls them all GEOMETRY)
SPATIAL_TYPES = frozenset({
    "geometry", "point", "linestring", "polygon", "multipoint", "multilinestring", "multipolygon", "geometrycollection",
    "geomcollection",
})
MYSQL_FAMILIES: dict[str, TypeFamily] = {
    "tinyint": "int16", "smallint": "int16", "mediumint": "int32", "int": "int32", "integer": "int32",
    "bigint": "int64", "decimal": "decimal", "numeric": "decimal", "float": "float32", "double": "float64",
    "real": "float64", "char": "char", "varchar": "varchar", "tinytext": "text", "text": "text", "mediumtext": "text",
    "longtext": "text", "binary": "binary", "varbinary": "binary", "tinyblob": "binary", "blob": "binary",
    "mediumblob": "binary", "longblob": "binary", "date": "date", "time": "duration", "year": "year",
    "datetime": "timestamp", "timestamp": "timestamp", "json": "json", "enum": "enum", "set": "set",
    "bit": "int64",  # the adapter reads BIT(n) as an integer (A M7)
    **dict.fromkeys(SPATIAL_TYPES, "spatial"),
}
# One step wider than the signed family, so that every unsigned value fits; BIGINT UNSIGNED has a family of its own
_MYSQL_UNSIGNED: dict[str, TypeFamily] = {"tinyint": "int16", "smallint": "int32", "mediumint": "int32",
                                          "int": "int64", "integer": "int64", "bigint": "uint64"}


def mysql_family(data_type: str, column_type: str, bits: int | None = None) -> TypeFamily:
    """`column_type` lower case (`tinyint(1)`, `int unsigned`); `bits`: a BIT column's width (NUMERIC_PRECISION)."""
    if column_type.startswith("tinyint(1)") and "unsigned" not in column_type:
        return "bool"  # MySQL's BOOLEAN
    if "unsigned" in column_type and data_type in _MYSQL_UNSIGNED:
        return _MYSQL_UNSIGNED[data_type]
    if data_type == "bit" and (bits or 0) > 63:
        return "uint64"  # BIT(64) holds up to 2^64 - 1
    return MYSQL_FAMILIES.get(data_type, "other")


# ---- SQL Server: sys.types' name of the column's base type ----

MSSQL_FAMILIES: dict[str, TypeFamily] = {
    "bit": "bool", "tinyint": "int16", "smallint": "int16", "int": "int32", "bigint": "int64", "decimal": "decimal",
    "numeric": "decimal", "money": "money", "smallmoney": "money", "real": "float32", "char": "char", "nchar": "char",
    "varchar": "varchar", "nvarchar": "varchar", "text": "text", "ntext": "text", "binary": "binary",
    "varbinary": "binary", "image": "binary", "date": "date", "time": "time", "datetime": "timestamp",
    "smalldatetime": "timestamp", "datetime2": "timestamp", "datetimeoffset": "timestamp_tz",
    "uniqueidentifier": "uuid", "xml": "xml", "json": "json", "timestamp": "rowversion", "rowversion": "rowversion",
    "sql_variant": "variant", "hierarchyid": "variant", "geometry": "spatial", "geography": "spatial",
}


def mssql_family(base: str, max_length: int, precision: int) -> TypeFamily:
    """`max_length` -1: (max); `precision`: a float's bits (53 unless float(n))."""
    if base == "float":
        return "float64" if precision > 24 else "float32"
    if base in ("varchar", "nvarchar") and max_length == -1:
        return "text"
    return MSSQL_FAMILIES.get(base, "other")


# ---- SQLite: the declared type, by affinity in SQLite's own order (⑤ ruling E6) ----

_SQLITE_SIZES = re.compile(r"\(\s*(\d{1,9})\s*(?:,\s*(\d{1,9})\s*)?\)")  # at most 9 digits: int() stays cheap
_SQLITE_FAMILIES: dict[str, TypeFamily] = {"none": "other", "integer": "int64", "blob": "binary", "real": "float64",
                                           "bool": "bool", "json": "json"}


def sqlite_affinity(upper: str, strict: bool) -> str:
    """What a declared type (upper case) is, by SQLite's affinity rules in their order — INT; CHAR, CLOB, TEXT;
    BLOB; no type; REAL, FLOA, DOUB; else NUMERIC — and inside NUMERIC by what its name says: DATE / TIME, BOOL, JSON
    (ruling E6). In a STRICT table ANY keeps every value as given, like no type."""
    if strict and upper == "ANY":
        return "none"
    if "INT" in upper:
        return "integer"
    if any(word in upper for word in ("CHAR", "CLOB", "TEXT")):
        return "text"
    if "BLOB" in upper:
        return "blob"
    if not upper.strip():
        return "none"
    if any(word in upper for word in ("REAL", "FLOA", "DOUB")):
        return "real"
    if "DATE" in upper or "TIME" in upper:
        return "datetime"
    if "BOOL" in upper:
        return "bool"
    if "JSON" in upper:
        return "json"
    return "numeric"


def sqlite_family(declared: str, strict: bool = False) -> tuple[TypeFamily, dict]:
    """The family a declared type names, with its sizes (`length`, or `precision` and `scale`)."""
    upper = declared.upper()
    affinity = sqlite_affinity(upper, strict)
    if affinity in _SQLITE_FAMILIES:
        return _SQLITE_FAMILIES[affinity], {}
    sizes = _SQLITE_SIZES.search(upper)
    first = int(sizes.group(1)) if sizes else None
    second = int(sizes.group(2)) if sizes and sizes.group(2) else None
    if affinity == "text":
        if "CHAR" in upper and first is not None:
            return ("varchar" if "VAR" in upper else "char"), {"length": first}
        return "text", {}
    if affinity == "datetime":
        if "DATE" in upper and "TIME" in upper or "TIMESTAMP" in upper:
            return "timestamp", {}
        return ("date" if "DATE" in upper else "time"), {}
    return "decimal", ({"precision": first, "scale": second if second is not None else 0} if first else {})


# ---- a type's text: a query result's column, a type typed for a new table ----

@dataclass(frozen=True, slots=True)
class NamedType:
    """A type's text read as its family and sizes (what the catalog would say of a column of that type)."""

    family: TypeFamily
    precision: int | None = None
    scale: int | None = None
    length: int | None = None  # None: no limit (text, max)


_NAME = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_ ]*?)\s*(?:\(\s*([^)]*)\s*\))?\s*(\[\])?\s*(unsigned)?\s*$", re.IGNORECASE)
# A query result's column with no family its name gives: by what the grid knows of it (spec §3.5.2's last sentence)
_BY_CATEGORY: dict[ColumnCategory, TypeFamily] = {
    ColumnCategory.NUMBER: "decimal", ColumnCategory.TEXT: "text", ColumnCategory.DATETIME: "timestamp",
    ColumnCategory.BINARY: "binary", ColumnCategory.BOOL: "bool", ColumnCategory.JSON: "json",
}
_SIZED = frozenset({"char", "varchar", "binary"})
_SCALED = frozenset({"time", "timestamp", "timestamp_tz", "time_tz"})


def _numbers(text: str | None) -> list[int]:
    if not text:
        return []
    found = []
    for part in text.split(","):
        part = part.strip().lower()
        if part == "max":
            return [-1]
        if not part.isdigit() or len(part) > 9:
            return []
        found.append(int(part))
    return found


def of_name(dialect: Dialect, type_name: str, category: ColumnCategory | None = None) -> NamedType:
    """The family of a type's text in `dialect` (`numeric(10,2)`, `int4[]`, `nvarchar(max)`, `int unsigned`), with the
    sizes its parentheses give; a name no rule knows is the family `category` gives (a query result's column), else
    `other`. Never raises."""
    found = _NAME.match(type_name or "")
    if found is None:
        return NamedType(_BY_CATEGORY.get(category, "other") if category else "other")
    base, args, array, unsigned = found.group(1).lower(), found.group(2), found.group(3), found.group(4)
    numbers = _numbers(args)
    family: TypeFamily
    if dialect is Dialect.POSTGRES:
        family = "other" if array else pg_family(_PG_ALIASES.get(base, base))
    elif dialect is Dialect.MYSQL:
        column_type = f"{base}({args})" if args else base
        family = mysql_family(base, f"{column_type} unsigned" if unsigned else column_type,
                              numbers[0] if base == "bit" and numbers else None)
    elif dialect is Dialect.TSQL:
        max_length = -1 if numbers == [-1] else 0
        family = mssql_family(base, max_length, numbers[0] if base == "float" and numbers else 53)
    elif dialect is Dialect.SQLITE:
        sqlite, sizes = sqlite_family(type_name)
        return NamedType(sqlite, sizes.get("precision"), sizes.get("scale"), sizes.get("length"))
    else:
        family = "other"
    if family == "other" and category is not None and not array:
        family = _BY_CATEGORY.get(category, "other")
    if family == "decimal" and numbers and numbers != [-1]:
        return NamedType(family, precision=numbers[0], scale=numbers[1] if len(numbers) > 1 else 0)
    if family in _SIZED and numbers and numbers != [-1]:
        return NamedType(family, length=numbers[0])
    if family in _SCALED and numbers and numbers != [-1]:
        return NamedType(family, scale=numbers[0])
    return NamedType(family)
