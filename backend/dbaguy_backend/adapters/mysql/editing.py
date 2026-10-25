"""What grid editing needs to know about a MySQL table (spec ⑤ §3.2.2): base tables only, each column's kind and whether
it can be written and compared, the key, whether the table's engine can roll back (D-05-6), and whether the session
reads backslashes as escapes (D-05-13). Four catalog queries on the metadata connection, which has the session's
profile options (time_zone, sql_mode)."""

from mysql.connector.connection import MySQLConnection

from ...domain.edits import EditTarget
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from . import catalog
from .columns import read_columns

_TABLE = """
SELECT t.TABLE_TYPE, e.TRANSACTIONS
FROM information_schema.TABLES t LEFT JOIN information_schema.ENGINES e ON e.ENGINE = t.ENGINE
WHERE t.TABLE_SCHEMA = %s AND t.TABLE_NAME = %s
"""
# COLUMN_NAME is NULL for a functional key part (ruling E4)
_KEYS = """
SELECT INDEX_NAME, COLUMN_NAME FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND NON_UNIQUE = 0 ORDER BY INDEX_NAME, SEQ_IN_INDEX
"""
_SQL_MODE = "SELECT @@SESSION.sql_mode"


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
    columns = [column.edit_column() for column in read_columns(conn, schema, name) if not column.hidden]
    parts: dict[str, list[str | None]] = {}
    for index, column in catalog.query(conn, _KEYS, (schema, name)):
        parts.setdefault(index, []).append(column)
    candidates = [KeyCandidate(index, tuple(c for c in cols if c is not None), index == "PRIMARY")
                  for index, cols in parts.items() if None not in cols]
    mode = catalog.first_row(conn, _SQL_MODE)
    escapes = "NO_BACKSLASH_ESCAPES" not in str(mode[0] if mode else "").upper().split(",")
    return build_target(path, columns, candidates, transactional=transactions == "YES", backslash_escapes=escapes)
