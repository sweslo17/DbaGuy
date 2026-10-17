"""Catalog queries over a mysql-connector connection: what the object tree and the schema snapshot read.

Internal SQL only. It never contains the word "delimiter": statement splitting (Task 9) treats that word at the start
of a statement as a client command, and these helpers must never be mistaken for user SQL.
"""

from collections.abc import Sequence
from typing import Any

from mysql.connector.connection import MySQLConnection
from mysql.connector.cursor import MySQLCursorBuffered

SYSTEM_SCHEMAS = frozenset({"information_schema", "mysql", "performance_schema", "sys"})
# Unknown database, table, routine, trigger, event.
MISSING_OBJECT_ERRNOS = frozenset({1049, 1146, 1305, 1360, 1539})


def _decode(value: Any) -> Any:
    """Catalog text columns may arrive as bytes (binary collations, GROUP_CONCAT, SHOW CREATE output)."""
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    return value


def query(conn: MySQLConnection, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
    """Run an internal statement and return every row, cells decoded to Python values.

    The cursor is built directly: `conn.cursor()` sends a PING first, a wasted round trip per catalog query.
    (The driver itself reads `@@session.sql_mode` through `conn.cursor()` once per connection, when it first
    substitutes parameters; queries without parameters never do.)
    """
    cursor = MySQLCursorBuffered(conn)
    try:
        cursor.execute(sql, tuple(params))
        if not cursor.with_rows:
            return []
        return [tuple(_decode(cell) for cell in row) for row in cursor.fetchall()]
    finally:
        cursor.close()


def first_row(conn: MySQLConnection, sql: str, params: Sequence[Any] = ()) -> tuple | None:
    rows = query(conn, sql, params)
    return rows[0] if rows else None


def list_schemas(conn: MySQLConnection) -> list[tuple[str, bool]]:
    """(name, is_system) of every schema the account may access: user schemas first, each group by name."""
    rows = query(conn, "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME")
    schemas = [(name, name in SYSTEM_SCHEMAS) for (name,) in rows]
    return sorted(schemas, key=lambda schema: schema[1])  # stable: keeps the name order inside each group
