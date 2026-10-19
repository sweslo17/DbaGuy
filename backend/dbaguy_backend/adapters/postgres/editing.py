"""What grid editing needs to know about a PostgreSQL table (spec ⑤ §3.2.2): its kind, how each column is written and
compared, and the key a row is identified by. Three catalog queries on the metadata connection (native loaders); the
first also reads the session's standard_conforming_strings, which the literals of sql/dialects.py assume on (G3)."""

import psycopg

from ...domain.edits import Compare, EditColumn, EditKind, EditTarget, edit_kind
from ...domain.metadata import PathSegment
from ...sql.dialects import Dialect, qualified
from ...sql.select_builder import relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from .introspection import relation_oid
from .loaders import category_for_type_name

# standard_conforming_strings rides along (no round trip of its own): off, a backslash in '…' escapes (ruling G3)
_RELKIND = "SELECT relkind::text, current_setting('standard_conforming_strings') FROM pg_class WHERE oid = %(oid)s"
# A domain is read through its base type, however many domains deep (ruling E23): `base` follows typbasetype from each
# column's type until a type that is not a domain, one row per column. typtype 'c' marks a composite type (ruling E4).
_COLUMNS = """
WITH RECURSIVE base (attnum, typname, typtype, typbasetype) AS (
    SELECT a.attnum, t.typname, t.typtype, t.typbasetype
    FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid
    WHERE a.attrelid = %(oid)s AND a.attnum > 0 AND NOT a.attisdropped
  UNION ALL
    SELECT d.attnum, t.typname, t.typtype, t.typbasetype
    FROM base d JOIN pg_type t ON t.oid = d.typbasetype
    WHERE d.typtype = 'd'
)
SELECT a.attname::text, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, a.atthasdef, a.attidentity::text,
       a.attgenerated::text, b.typname::text, b.typtype::text
FROM pg_attribute a
JOIN base b ON b.attnum = a.attnum AND b.typtype <> 'd'
WHERE a.attrelid = %(oid)s AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""
# Key columns only (INCLUDE columns come after indnkeyatts); no partial, expression or invalid index.
_KEYS = """
SELECT ic.relname::text, i.indisprimary, array_agg(a.attname::text ORDER BY k.ord)
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k (attnum, ord)
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
WHERE i.indrelid = %(oid)s AND i.indisvalid AND k.ord <= i.indnkeyatts
  AND (i.indisprimary OR (i.indisunique AND i.indpred IS NULL AND i.indexprs IS NULL))
GROUP BY ic.relname, i.indisprimary
"""
_STRINGS_NOT_STANDARD = "standard_conforming_strings is off: values with backslashes would be changed"
_READ_ONLY = {"v": VIEWS, "m": "Materialized views are read-only here", "f": "Foreign tables are read-only here"}
_NOT_EXACT = frozenset({"float4", "float8"})  # loaded as Python floats: their text is not the server's
_PADDED = frozenset({"bpchar"})  # ::text drops the padding the loaded value has; `=` ignores it (A4)


def _column(
    name: str, type_name: str, nullable: bool, has_default: bool, identity: str, generated: str, base: str,
    typtype: str,
) -> tuple[EditColumn, bool]:
    """The column, and whether its type is composite (a key on it cannot be written as `c = '…'`, 0A000)."""
    kind: EditKind = "other" if base == "money" else edit_kind(category_for_type_name(base))  # money: lc_monetary
    compare: Compare = "none" if base in _NOT_EXACT else "equals" if base in _PADDED else "as_text"
    reason = "generated column" if generated else "identity column (GENERATED ALWAYS)" if identity == "a" else None
    column = EditColumn(
        name=name, type_name=type_name, kind=kind, nullable=nullable,
        has_default=has_default or bool(identity) or bool(generated), writable=reason is None,
        read_only_reason=reason, compare=compare,
    )
    return column, typtype == "c"


def edit_target(conn: psycopg.Connection, path: list[PathSegment]) -> EditTarget:
    schema, name = relation_from_path(path)
    oid = relation_oid(conn, qualified(Dialect.POSTGRES, schema, name))
    row = conn.execute(_RELKIND, {"oid": oid}).fetchone()
    relkind, standard_strings = row if row is not None else ("", None)
    if relkind not in ("r", "p"):
        return read_only_relation(path, _READ_ONLY.get(relkind, "This object is read-only here"))
    if standard_strings != "on":  # fail closed: only the setting the literals are written for
        return read_only_relation(path, _STRINGS_NOT_STANDARD)
    read = [_column(*row) for row in conn.execute(_COLUMNS, {"oid": oid})]
    composite = {column.name for column, is_composite in read if is_composite}
    candidates = [
        KeyCandidate(index, tuple(columns), primary)
        for index, primary, columns in conn.execute(_KEYS, {"oid": oid})
        if not composite.intersection(columns)
    ]
    return build_target(path, [column for column, _ in read], candidates)
