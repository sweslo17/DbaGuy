"""What grid editing needs to know about a MySQL table (spec ⑤ §3.2.2): base tables only, each column's kind and whether
it can be written and compared, the key, whether the table's engine can roll back (D-05-6), and whether the session
reads backslashes as escapes (D-05-13). Four catalog queries on the metadata connection, which has the session's
profile options (time_zone, sql_mode)."""

from mysql.connector.connection import MySQLConnection

from ...domain.edits import Compare, EditColumn, EditTarget, edit_kind
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from . import catalog
from .types import SPATIAL_TYPES, category_for_data_type

_TABLE = """
SELECT t.TABLE_TYPE, e.TRANSACTIONS
FROM information_schema.TABLES t LEFT JOIN information_schema.ENGINES e ON e.ENGINE = t.ENGINE
WHERE t.TABLE_SCHEMA = %s AND t.TABLE_NAME = %s
"""
_COLUMNS = """
SELECT COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA
FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION
"""
# COLUMN_NAME is NULL for a functional key part (ruling E4)
_KEYS = """
SELECT INDEX_NAME, COLUMN_NAME FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND NON_UNIQUE = 0 ORDER BY INDEX_NAME, SEQ_IN_INDEX
"""
_SQL_MODE = "SELECT @@SESSION.sql_mode"
_GENERATED = ("VIRTUAL GENERATED", "STORED GENERATED")  # not DEFAULT_GENERATED: an expression default (ruling E15)
_NOT_EXACT = frozenset({"float", "double", "real", "vector", *SPATIAL_TYPES})


def _column(
    name: str, column_type: str, data_type: str, is_nullable: str, default: str | None, extra: str | None
) -> EditColumn:
    data_type, extra = data_type.lower(), (extra or "").upper()
    generated = any(word in extra for word in _GENERATED)
    if generated:
        reason = "generated column"
    elif data_type in SPATIAL_TYPES:
        reason = "geometry values are not edited here"
    elif data_type == "vector":
        reason = "vector values are not edited here"
    else:
        reason = None
    compare: Compare = "none" if data_type in _NOT_EXACT else "equals"
    return EditColumn(
        name=name, type_name=column_type, kind=edit_kind(category_for_data_type(data_type)),
        nullable=is_nullable == "YES",
        has_default=default is not None or generated or "AUTO_INCREMENT" in extra or "DEFAULT_GENERATED" in extra,
        writable=reason is None, read_only_reason=reason, compare=compare,
    )


def edit_target(conn: MySQLConnection, path: list[PathSegment]) -> EditTarget:
    schema, name = relation_from_path(path)
    found = catalog.first_row(conn, _TABLE, (schema, name))
    if found is None:
        raise NotFound(f"Table {schema}.{name} not found")
    table_type, transactions = found
    if table_type != "BASE TABLE":
        return read_only_relation(path, VIEWS if table_type == "VIEW" else "System views are read-only here")
    # An INVISIBLE column is not in the grid's SELECT *: it is left out of the target, so it is never the key (a generated
    # invisible primary key, my_row_id: the key is then a visible unique key, or there is none)
    columns = [_column(*row) for row in catalog.query(conn, _COLUMNS, (schema, name))
               if "INVISIBLE" not in (row[5] or "").upper()]
    parts: dict[str, list[str | None]] = {}
    for index, column in catalog.query(conn, _KEYS, (schema, name)):
        parts.setdefault(index, []).append(column)
    candidates = [KeyCandidate(index, tuple(c for c in cols if c is not None), index == "PRIMARY")
                  for index, cols in parts.items() if None not in cols]
    mode = catalog.first_row(conn, _SQL_MODE)
    escapes = "NO_BACKSLASH_ESCAPES" not in str(mode[0] if mode else "").upper().split(",")
    return build_target(path, columns, candidates, transactional=transactions == "YES", backslash_escapes=escapes)
