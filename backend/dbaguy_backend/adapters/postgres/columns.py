"""A PostgreSQL relation's columns (spec ⑥ D-06-1): one catalog query on the connection's native loaders. A domain is
read through its base type, however many domains deep (⑤ ruling E23), and so is its type modifier (precision, length).
Grid editing's kind and compare rules (spec ⑤ §3.2.2) are made here, once, for editing and data tools alike."""

import psycopg

from ...domain.edits import Compare, EditKind, edit_kind
from ...domain.metadata import PathSegment
from ...sql.dialects import Dialect, qualified
from ...sql.select_builder import relation_from_path
from ..columns import GENERATED, TableColumn, TypeFamily, not_copied
from .introspection import relation_oid
from .loaders import category_for_type_name

# `base` follows typbasetype from each column's type until a type that is not a domain, one row per column, carrying
# the innermost type modifier a domain sets (a domain column's own atttypmod is -1). typtype 'c' is a composite type,
# 'e' an enum; typcategory 'A' an array.
_COLUMNS = """
WITH RECURSIVE base (attnum, typname, typtype, typbasetype, typtypmod, typcategory, typmod) AS (
    SELECT a.attnum, t.typname, t.typtype, t.typbasetype, t.typtypmod, t.typcategory, a.atttypmod
    FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid
    WHERE a.attrelid = %(oid)s AND a.attnum > 0 AND NOT a.attisdropped
  UNION ALL
    SELECT d.attnum, t.typname, t.typtype, t.typbasetype, t.typtypmod, t.typcategory,
           CASE WHEN d.typmod <> -1 THEN d.typmod ELSE d.typtypmod END
    FROM base d JOIN pg_type t ON t.oid = d.typbasetype
    WHERE d.typtype = 'd'
)
SELECT a.attname::text, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, a.atthasdef, a.attidentity::text,
       a.attgenerated::text, b.typname::text, b.typtype::text, b.typcategory::text, b.typmod
FROM pg_attribute a
JOIN base b ON b.attnum = a.attnum AND b.typtype <> 'd'
WHERE a.attrelid = %(oid)s AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""
_NOT_EXACT = frozenset({"float4", "float8"})  # loaded as Python floats: their text is not the server's
# Compared with `=`, not ::text: bpchar, whose ::text drops the padding the loaded value has (`=` ignores it, A4); bytea,
# whose ::text follows bytea_output (escape: every original would differ) while `= '\x…'::bytea` is exact (05a review M3)
_EQUALS = frozenset({"bpchar", "bytea"})
_FAMILIES: dict[str, TypeFamily] = {
    "bool": "bool", "int2": "int16", "int4": "int32", "int8": "int64", "numeric": "decimal", "money": "money",
    "float4": "float32", "float8": "float64", "bpchar": "char", "varchar": "varchar", "text": "text", "citext": "text",
    "name": "text", "bytea": "binary", "date": "date", "time": "time", "timetz": "time_tz", "timestamp": "timestamp",
    "timestamptz": "timestamp_tz", "interval": "interval", "uuid": "uuid", "json": "json", "jsonb": "json",
    "xml": "xml", "geometry": "spatial", "geography": "spatial",
}
_LENGTHS = frozenset({"bpchar", "varchar"})
_FRACTIONS = frozenset({"time", "timetz", "timestamp", "timestamptz"})
# money's text follows lc_monetary (`$1,234.56`): a dump or copy reads it as numeric, exact, and writes the number (P10;
# A13: an array of money and a domain over money too)
_MONEY_READ = {"money": ("CAST({} AS numeric)", "number"), "_money": ("CAST({} AS numeric[])", "other")}


def _family(base: str, typtype: str, category: str) -> TypeFamily:
    if typtype == "e":
        return "enum"
    if typtype == "c":
        return "composite"
    if category == "A":
        return "other"  # an array: PostgreSQL's text
    return _FAMILIES.get(base, "other")


def _sizes(base: str, typmod: int) -> dict:
    """precision / scale / length from the type modifier (-1: none)."""
    if typmod < 0:
        return {}
    if base == "numeric" and typmod >= 4:
        scale = (typmod - 4) & 0x7FF
        return {"precision": ((typmod - 4) >> 16) & 0xFFFF, "scale": scale - 2048 if scale > 1023 else scale}
    if base in _LENGTHS and typmod >= 4:
        return {"length": typmod - 4}
    if base in _FRACTIONS:
        return {"scale": typmod}
    return {}


def column(
    name: str, type_name: str, nullable: bool, has_default: bool, identity: str, generated: str, base: str,
    typtype: str, category: str, typmod: int,
) -> TableColumn:
    """One row of _COLUMNS."""
    kind: EditKind = "other" if base == "money" else edit_kind(category_for_type_name(base))  # money: lc_monetary
    compare: Compare = "none" if base in _NOT_EXACT else "equals" if base in _EQUALS else "as_text"
    reason = GENERATED if generated else "identity column (GENERATED ALWAYS)" if identity == "a" else None
    read_as, literal = _MONEY_READ.get(base, (None, None))
    return TableColumn(
        name=name, type_name=type_name, family=_family(base, typtype, category), nullable=nullable,
        has_default=has_default or bool(identity) or bool(generated), kind=kind, compare=compare,
        writable=reason is None, read_only_reason=reason, **_sizes(base, typmod),
        identity="always" if identity == "a" else "by_default" if identity == "d" else None,
        generated=bool(generated), **(not_copied(GENERATED) if generated else {}), read_as=read_as, literal=literal,
    )


def read_columns(conn: psycopg.Connection, oid: int) -> list[TableColumn]:
    return [column(*row) for row in conn.execute(_COLUMNS, {"oid": oid})]


def table_columns(conn: psycopg.Connection, path: list[PathSegment]) -> list[TableColumn]:
    """The columns of the relation at `path` (a table, a view, a materialized view); NotFound when it is gone."""
    schema, name = relation_from_path(path)
    return read_columns(conn, relation_oid(conn, qualified(Dialect.POSTGRES, schema, name)))
