"""What grid editing needs to know about a SQLite table (spec ⑤ §3.2.2): tables only (no view, no virtual table, none
of a virtual table's internal tables), each column read by its declared type's affinity (ruling E6; ANY in a STRICT
table has none), the primary key or a unique index on NOT NULL columns. The rowid is never a key: `SELECT *` does not
return it."""

import sqlite3

from ...domain.edits import EditTarget
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from .columns import columns_of, read_relation, relation_kind

_READ_ONLY = {
    "view": VIEWS, "virtual": "Virtual tables are read-only here",
    "shadow": "Internal table of a virtual table: read-only here",  # e.g. fts5's _data, _idx, _content, _config
}
_INDEX_COLUMNS = "SELECT name FROM pragma_index_info(?, ?) ORDER BY seqno"


def edit_target(conn: sqlite3.Connection, path: list[PathSegment]) -> EditTarget:
    schema, name = relation_from_path(path)
    schema = schema or "main"
    listed = relation_kind(conn, schema, name)
    if listed[0] != "table":  # its columns are not read: a view over a dropped table has none to read
        return read_only_relation(path, _READ_ONLY.get(listed[0], "This object is read-only here"))
    relation = read_relation(conn, schema, name, listed)
    columns = [column.edit_column() for column in columns_of(relation)]
    key = [column for _, column in sorted((pk, column) for column, _, _, _, pk, _ in relation.rows if pk > 0)]
    candidates = [KeyCandidate("PRIMARY KEY", tuple(key), True)] if key else []
    for index, unique, origin, partial in relation.indexes:
        if not unique or partial or origin == "pk":
            continue
        parts = [part for (part,) in conn.execute(_INDEX_COLUMNS, (index, schema)).fetchall()]
        if parts and None not in parts:  # an expression has no column name
            candidates.append(KeyCandidate(index, tuple(parts), False))
    return build_target(path, columns, candidates)
