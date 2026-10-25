"""What keeps a MySQL session read-only and readable as DbaGuy's lexer reads it (spec ⑧ D-08-1, CR-08-5): an agent's
session leaves out the sql_mode flags that make the server read strings and quotes unlike the lexer (H2)."""

from mysql.connector.connection import MySQLConnection

from ...sql.dialects import Dialect, quote_literal
from . import catalog

# NO_BACKSLASH_ESCAPES and ANSI_QUOTES (and ANSI, which turns ANSI_QUOTES back on): with them, `'a\' INTO @x -- '` and
# `"a\" INTO @x -- "` hide an INTO from the lexer that the server runs (H2)
LEXER_UNSAFE_MODES = frozenset({"NO_BACKSLASH_ESCAPES", "ANSI_QUOTES", "ANSI"})


def lexer_safe(sql_mode: str) -> str:
    """`sql_mode` without LEXER_UNSAFE_MODES, the rest in its order."""
    return ",".join(mode for mode in sql_mode.split(",") if mode and mode.upper() not in LEXER_UNSAFE_MODES)


def unsafe_modes(sql_mode: str) -> list[str]:
    return [mode for mode in sql_mode.split(",") if mode.upper() in LEXER_UNSAFE_MODES]


def make_agent_sql_mode(conn: MySQLConnection) -> None:
    """At an agent connection's open, after the profile's own settings: its sql_mode without the unsafe flags."""
    row = catalog.first_row(conn, "SELECT @@SESSION.sql_mode")
    current = str(row[0]) if row and row[0] is not None else ""
    if unsafe_modes(current):
        conn.cmd_query(f"SET SESSION sql_mode = {quote_literal(Dialect.MYSQL, lexer_safe(current))}")
