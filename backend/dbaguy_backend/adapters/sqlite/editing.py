"""What grid editing needs to know about a SQLite table (spec ⑤ §3.2.2): tables only (no view, no virtual table), each
column read by its declared type's affinity (ruling E6), the primary key or a unique index on NOT NULL columns. The
rowid is never a key: `SELECT *` does not return it."""

import sqlite3

from ...domain.edits import Compare, EditColumn, EditKind, EditTarget
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from ..sql_text import quote_ident_sqlite as q

_GENERATED = frozenset({2, 3})  # pragma_table_xinfo.hidden: a VIRTUAL or STORED generated column
_COLUMNS = 'SELECT name, type, "notnull", dflt_value, pk, hidden FROM pragma_table_xinfo(?, ?) ORDER BY cid'
_INDEXES = 'SELECT name, "unique", origin, partial FROM pragma_index_list(?, ?)'
_INDEX_COLUMNS = "SELECT name FROM pragma_index_info(?, ?) ORDER BY seqno"


def affinity_kind(declared: str) -> tuple[EditKind, Compare]:
    """SQLite's affinity rules in their order — INT; CHAR, CLOB, TEXT; BLOB or no type; REAL, FLOA, DOUB; else NUMERIC
    — and inside NUMERIC what the declared name says: DATE / TIME, BOOL, JSON (ruling E6). REAL affinity holds floating
    point, which is not compared."""
    upper = declared.upper()
    if "INT" in upper:
        return "number", "equals"
    if any(word in upper for word in ("CHAR", "CLOB", "TEXT")):
        return "text", "equals"
    if "BLOB" in upper:
        return "binary", "equals"
    if not upper.strip():
        return "other", "equals"
    if any(word in upper for word in ("REAL", "FLOA", "DOUB")):
        return "number", "none"
    if "DATE" in upper or "TIME" in upper:
        return "datetime", "equals"
    if "BOOL" in upper:
        return "bool", "equals"
    if "JSON" in upper:
        return "json", "equals"
    return "number", "equals"


def edit_target(conn: sqlite3.Connection, path: list[PathSegment]) -> EditTarget:
    schema, name = relation_from_path(path)
    schema = schema or "main"
    found = conn.execute(
        f"SELECT type, sql FROM {q(schema)}.sqlite_master WHERE name = ? AND type IN ('table', 'view')", (name,)
    ).fetchone()
    if found is None:
        raise NotFound(f"Table {name} not found")
    kind, ddl = found
    if kind == "view":
        return read_only_relation(path, VIEWS)
    if (ddl or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
        return read_only_relation(path, "Virtual tables are read-only here")
    rows = conn.execute(_COLUMNS, (name, schema)).fetchall()
    indexes = conn.execute(_INDEXES, (name, schema)).fetchall()
    key = [column for _, column in sorted((pk, column) for column, _, _, _, pk, _ in rows if pk > 0)]
    # INTEGER PRIMARY KEY is the rowid: never NULL, assigned when left out. Not when the key has an index of its own
    # (origin pk): WITHOUT ROWID (NOT NULL, never assigned) or INTEGER PRIMARY KEY DESC (an ordinary nullable column).
    rowid = key[0] if (
        len(key) == 1 and next(t for c, t, *_ in rows if c == key[0]).upper() == "INTEGER"
        and not any(origin == "pk" for _, _, origin, _ in indexes)
    ) else None
    columns = []
    for column, declared, notnull, default, _, hidden in rows:
        kind_of, compare = affinity_kind(declared or "")
        generated = hidden in _GENERATED
        columns.append(EditColumn(
            name=column, type_name=declared or "", kind=kind_of, nullable=not notnull and column != rowid,
            has_default=default is not None or generated or column == rowid, writable=not generated,
            read_only_reason="generated column" if generated else None, compare=compare,
        ))
    candidates = [KeyCandidate("PRIMARY KEY", tuple(key), True)] if key else []
    for index, unique, origin, partial in indexes:
        if not unique or partial or origin == "pk":
            continue
        parts = [part for (part,) in conn.execute(_INDEX_COLUMNS, (index, schema)).fetchall()]
        if parts and None not in parts:  # an expression has no column name
            candidates.append(KeyCandidate(index, tuple(parts), False))
    return build_target(path, columns, candidates)
