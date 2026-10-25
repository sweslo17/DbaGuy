"""A PostgreSQL relation's columns (spec ⑥ D-06-1): one catalog query on the connection's native loaders. A domain is
read through its base type, however many domains deep (⑤ ruling E23), and so is its type modifier (precision, length).
Grid editing's kind and compare rules (spec ⑤ §3.2.2) are made here, once, for editing and data tools alike."""

import psycopg

from ...domain.edits import Compare, EditKind, edit_kind
from ...domain.metadata import PathSegment
from ...sql.dialects import Dialect, qualified
from ...sql.select_builder import relation_from_path
from ..columns import GENERATED, TableColumn, not_copied
from ..families import pg_family
from .introspection import relation_oid
from .loaders import category_for_type_name

# `base` follows typbasetype from each column's type until a type that is not a domain, one row per column, carrying
# the innermost type modifier a domain sets (a domain column's own atttypmod is -1). typtype 'c' is a composite type,
# 'e' an enum; typcategory 'A' an array. The last column: the column's own type is the database's (a domain, an enum, a
# row type, an extension's type, an array of one), which another database may not have (a copy, 06c). After it: the
# column's place in the relation's primary key (a copy's new table, 06c Task 2 fix round 3), NULL when it is in none:
# `keyed` reads every primary key of the relations once, MATERIALIZED so that it is hashed and joined (inlined, the
# planner probes pg_index per column; a subquery per column made the dump's catalog read about 1.5 times as long: 06c
# final review M-3, 1,000 tables of 20 columns, medians: 53 ms before 06c, 79 ms with the subquery, 58 ms now).
_COLUMNS = """
WITH RECURSIVE base (attrelid, attnum, typname, typtype, typbasetype, typtypmod, typcategory, typmod) AS (
    SELECT a.attrelid, a.attnum, t.typname, t.typtype, t.typbasetype, t.typtypmod, t.typcategory, a.atttypmod
    FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid
    WHERE a.attrelid = ANY(%(oids)s) AND a.attnum > 0 AND NOT a.attisdropped
  UNION ALL
    SELECT d.attrelid, d.attnum, t.typname, t.typtype, t.typbasetype, t.typtypmod, t.typcategory,
           CASE WHEN d.typmod <> -1 THEN d.typmod ELSE d.typtypmod END
    FROM base d JOIN pg_type t ON t.oid = d.typbasetype
    WHERE d.typtype = 'd'
),
keyed (attrelid, attnum, ord) AS MATERIALIZED (
    SELECT i.indrelid, k.attnum, k.ord::int
    FROM pg_index i CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k (attnum, ord)
    WHERE i.indisprimary AND i.indrelid = ANY(%(oids)s)
)
SELECT a.attrelid, a.attname::text, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, a.atthasdef,
       a.attidentity::text, a.attgenerated::text, b.typname::text, b.typtype::text, b.typcategory::text, b.typmod,
       own.typtype IN ('d', 'e', 'c') OR own.typnamespace <> 'pg_catalog'::regnamespace,
       k.ord
FROM pg_attribute a
JOIN pg_type own ON own.oid = a.atttypid
JOIN base b ON b.attrelid = a.attrelid AND b.attnum = a.attnum AND b.typtype <> 'd'
LEFT JOIN keyed k ON k.attrelid = a.attrelid AND k.attnum = a.attnum
WHERE a.attrelid = ANY(%(oids)s) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attrelid, a.attnum
"""
_NOT_EXACT = frozenset({"float4", "float8"})  # loaded as Python floats: their text is not the server's
# Compared with `=`, not ::text: bpchar, whose ::text drops the padding the loaded value has (`=` ignores it, A4); bytea,
# whose ::text follows bytea_output (escape: every original would differ) while `= '\x…'::bytea` is exact (05a review M3)
_EQUALS = frozenset({"bpchar", "bytea"})
_LENGTHS = frozenset({"bpchar", "varchar"})
_FRACTIONS = frozenset({"time", "timetz", "timestamp", "timestamptz"})
# money's text follows lc_monetary (`$1,234.56`): a dump or copy reads it as numeric, exact, and writes the number (P10;
# A13: an array of money and a domain over money too)
_MONEY_READ = {"money": ("CAST({} AS numeric)", "number"), "_money": ("CAST({} AS numeric[])", "other")}


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
    typtype: str, category: str, typmod: int, custom: bool = False, key_position: int | None = None,
) -> TableColumn:
    """One row of _COLUMNS."""
    kind: EditKind = "other" if base == "money" else edit_kind(category_for_type_name(base))  # money: lc_monetary
    compare: Compare = "none" if base in _NOT_EXACT else "equals" if base in _EQUALS else "as_text"
    reason = GENERATED if generated else "identity column (GENERATED ALWAYS)" if identity == "a" else None
    read_as, literal = _MONEY_READ.get(base, (None, None))
    return TableColumn(
        name=name, type_name=type_name, family=pg_family(base, typtype, category), nullable=nullable,
        has_default=has_default or bool(identity) or bool(generated), kind=kind, compare=compare,
        writable=reason is None, read_only_reason=reason, **_sizes(base, typmod),
        identity="always" if identity == "a" else "by_default" if identity == "d" else None,
        generated=bool(generated), **(not_copied(GENERATED) if generated else {}), read_as=read_as, literal=literal,
        custom_type=custom, key_position=key_position,
    )


def read_columns_of(conn: psycopg.Connection, oids: list[int]) -> dict[int, list[TableColumn]]:
    """The columns of every relation in `oids`, in one query (a dump reads every table's)."""
    found: dict[int, list[TableColumn]] = {oid: [] for oid in oids}
    if oids:
        for oid, *row in conn.execute(_COLUMNS, {"oids": list(oids)}):
            found[oid].append(column(*row))
    return found


def read_columns(conn: psycopg.Connection, oid: int) -> list[TableColumn]:
    return read_columns_of(conn, [oid])[oid]


def table_columns(conn: psycopg.Connection, path: list[PathSegment]) -> list[TableColumn]:
    """The columns of the relation at `path` (a table, a view, a materialized view); NotFound when it is gone."""
    schema, name = relation_from_path(path)
    return read_columns(conn, relation_oid(conn, qualified(Dialect.POSTGRES, schema, name)))
