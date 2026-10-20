"""A SQLite relation's columns (spec ⑥ D-06-1), read by the declared type's affinity in SQLite's own order (⑤ ruling
E6; ANY in a STRICT table has none). Grid editing's kind and compare rules (spec ⑤ §3.2.2) are made here, once, for
editing and data tools alike. The rowid is never a column: `SELECT *` does not return it."""

import re
import sqlite3
from dataclasses import dataclass

from ...domain.edits import Compare, EditKind
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..columns import GENERATED, TableColumn, TypeFamily, not_copied

# The pragma takes the name only (a second argument raises); it matches the name without case, as SQLite does
_TABLE = "SELECT type, strict FROM pragma_table_list(?) WHERE schema = ?"
_COLUMNS = 'SELECT name, type, "notnull", dflt_value, pk, hidden FROM pragma_table_xinfo(?, ?) ORDER BY cid'
_INDEXES = 'SELECT name, "unique", origin, partial FROM pragma_index_list(?, ?)'
_GENERATED = frozenset({2, 3})  # pragma_table_xinfo.hidden: a VIRTUAL or STORED generated column
_HIDDEN = 1  # pragma_table_xinfo.hidden: a virtual table's hidden column
# At most 9 digits: SQLite takes any declared size, and int() refuses a text of 4,300 digits or more (no size then)
_SIZES = re.compile(r"\(\s*(\d{1,9})\s*(?:,\s*(\d{1,9})\s*)?\)")


@dataclass(frozen=True)
class SqliteRelation:
    """pragma_table_list's row and the relation's pragma_table_xinfo and pragma_index_list rows."""

    kind: str  # table, view, virtual, shadow
    strict: bool
    rows: list[tuple]
    indexes: list[tuple]


def relation_kind(conn: sqlite3.Connection, schema: str, name: str) -> tuple[str, bool]:
    """pragma_table_list's type (table, view, virtual, shadow) and STRICT flag; NotFound when there is no such relation.
    Read on its own first where the columns are wanted only for a table: a view over a dropped table lists, but its
    pragma_table_xinfo fails (no such table)."""
    found = conn.execute(_TABLE, (name, schema)).fetchone()
    if found is None:
        raise NotFound(f"Table {name} not found")
    return found[0], bool(found[1])


def read_relation(
    conn: sqlite3.Connection, schema: str, name: str, listed: tuple[str, bool] | None = None,
) -> SqliteRelation:
    """`listed`: what relation_kind() already gave for it."""
    kind, strict = listed or relation_kind(conn, schema, name)
    return SqliteRelation(kind, strict, conn.execute(_COLUMNS, (name, schema)).fetchall(),
                          conn.execute(_INDEXES, (name, schema)).fetchall())


def _affinity(upper: str, strict: bool) -> str:
    """What a declared type (upper case) is, by SQLite's affinity rules in their order — INT; CHAR, CLOB, TEXT;
    BLOB; no type; REAL, FLOA, DOUB; else NUMERIC — and inside NUMERIC by what its name says: DATE / TIME, BOOL, JSON
    (ruling E6). In a STRICT table ANY keeps every value as given, like no type. The one ladder `affinity_kind` and
    `_family` both read."""
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


# REAL affinity holds floating point, which is not compared. Outside a STRICT table ANY is NUMERIC ('007' is stored as
# 7); in one it keeps every value as given, like no type ('007' stays text)
_EDIT_KINDS: dict[str, tuple[EditKind, Compare]] = {
    "none": ("other", "equals"), "integer": ("number", "equals"), "text": ("text", "equals"),
    "blob": ("binary", "equals"), "real": ("number", "none"), "datetime": ("datetime", "equals"),
    "bool": ("bool", "equals"), "json": ("json", "equals"), "numeric": ("number", "equals"),
}
_FAMILIES: dict[str, TypeFamily] = {"none": "other", "integer": "int64", "blob": "binary", "real": "float64",
                                    "bool": "bool", "json": "json"}


def affinity_kind(declared: str, strict: bool = False) -> tuple[EditKind, Compare]:
    """Grid editing's kind and compare rule for a declared type (`_affinity`)."""
    return _EDIT_KINDS[_affinity(declared.upper(), strict)]


def _family(declared: str, strict: bool) -> tuple[TypeFamily, dict]:
    """The family a declared type names (`_affinity`), with its sizes."""
    upper = declared.upper()
    affinity = _affinity(upper, strict)
    if affinity in _FAMILIES:
        return _FAMILIES[affinity], {}
    sizes = _SIZES.search(upper)
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


def rowid_alias(relation: SqliteRelation) -> str | None:
    """INTEGER PRIMARY KEY is the rowid: never NULL, assigned when left out. Not when the key has an index of its own
    (origin pk): WITHOUT ROWID (NOT NULL, never assigned) or INTEGER PRIMARY KEY DESC (an ordinary nullable column)."""
    key = [column for _, column in sorted((pk, column) for column, _, _, _, pk, _ in relation.rows if pk > 0)]
    if len(key) != 1 or any(origin == "pk" for _, _, origin, _ in relation.indexes):
        return None
    declared = next(t for c, t, *_ in relation.rows if c == key[0])
    return key[0] if (declared or "").upper() == "INTEGER" else None


def columns_of(relation: SqliteRelation) -> list[TableColumn]:
    rowid = rowid_alias(relation)
    found = []
    for name, declared, notnull, default, _, hidden in relation.rows:
        if hidden == _HIDDEN:
            continue
        kind, compare = affinity_kind(declared or "", strict=relation.strict)
        family, sizes = _family(declared or "", relation.strict)
        generated = hidden in _GENERATED
        found.append(TableColumn(
            name=name, type_name=declared or "", family=family, nullable=not notnull and name != rowid,
            has_default=default is not None or generated or name == rowid, kind=kind, compare=compare,
            writable=not generated, read_only_reason=GENERATED if generated else None, **sizes,
            identity="by_default" if name == rowid else None, generated=generated,
            **(not_copied(GENERATED) if generated else {}),
        ))
    return found


def table_columns(conn: sqlite3.Connection, path: list[PathSegment]) -> list[TableColumn]:
    """The columns of the table or view at `path`; NotFound when it is gone."""
    schema, name = relation_from_path(path)
    return columns_of(read_relation(conn, schema or "main", name))
