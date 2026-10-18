"""What the SQL Server object tree and the schema snapshot share: system names, quoting, and reading catalog rows.

Internal SQL only. Names reach a statement through `q()` (identifiers), `literal()` (values) or `object_id_sql()`,
never through driver parameters: pymssql would substitute `%`, and user SQL must not be touched either (the same
helpers keep one rule for both).
"""

from ...sql.dialects import Dialect, force_quote_ident, quote_literal
from . import driver

SYSTEM_DATABASES = frozenset({"master", "model", "msdb", "tempdb"})
SYSTEM_SCHEMAS = frozenset({"sys", "INFORMATION_SCHEMA", "guest"})
# The fixed database roles (db_owner, db_datareader, …) each own a schema with the id of the role.
_ROLE_SCHEMA_IDS = range(16384, 16400)

_CHARS = frozenset({"char", "varchar", "binary", "varbinary"})
_NCHARS = frozenset({"nchar", "nvarchar"})  # max_length counts bytes: two per character
_SCALED = frozenset({"decimal", "numeric"})
_FRACTION = frozenset({"datetime2", "datetimeoffset", "time"})


def is_system_schema(name: str, schema_id: int) -> bool:
    return name in SYSTEM_SCHEMAS or schema_id in _ROLE_SCHEMA_IDS


def q(name: str) -> str:
    return force_quote_ident(Dialect.TSQL, name)


def literal(value: str) -> str:
    return quote_literal(Dialect.TSQL, value)


def name_literal(schema: str, name: str) -> str:
    """`N'[schema].[name]'`: a two-part name as the text OBJECT_ID, TYPE_ID and the like take."""
    return literal(f"{q(schema)}.{q(name)}")


def object_id_sql(schema: str, name: str) -> str:
    return f"OBJECT_ID({name_literal(schema, name)})"


def read_rows(conn: driver.Connection) -> list[tuple]:
    """The current result set's rows, then everything left of the statement read and dropped (later result sets, the
    DONE of a trailing SET). Nothing stays unread, so the next execute_query() does not first send an attention to
    discard it: one round trip per internal query, also in manual mode (R-03b-P7)."""
    found = [tuple(row) for row in conn.get_iterator(driver.ROW)]
    while conn.nextresult():
        for _ in conn.get_iterator(driver.ROW):
            pass
    return found


def rows(conn: driver.Connection, sql: str) -> list[tuple]:
    conn.execute_query(sql)
    return read_rows(conn)


def result_sets(conn: driver.Connection, sql: str) -> list[list[tuple]]:
    """Every result set of one batch, empty ones included, in order. Several catalog queries sent as one batch cost one
    round trip instead of one each (over an SSH tunnel that is the whole price); nothing stays unread, like read_rows."""
    conn.execute_query(sql)
    found = [[tuple(row) for row in conn.get_iterator(driver.ROW)]]
    while conn.nextresult():
        found.append([tuple(row) for row in conn.get_iterator(driver.ROW)])
    return found


def batch(*queries: str) -> str:
    """Statements joined into one batch for result_sets."""
    return ";\n".join(query.strip() for query in queries)


def first_row(conn: driver.Connection, sql: str) -> tuple | None:
    found = rows(conn, sql)
    return found[0] if found else None


def type_sql(type_name: str, max_length: int, precision: int, scale: int) -> str:
    """A column's type as T-SQL writes it, from sys.types / sys.columns. An alias type (`email_address`) keeps its
    name: its length and precision are the alias's."""
    if type_name in _NCHARS:
        return f"{type_name}({'max' if max_length == -1 else max_length // 2})"
    if type_name in _CHARS:
        return f"{type_name}({'max' if max_length == -1 else max_length})"
    if type_name in _SCALED:
        return f"{type_name}({precision},{scale})"
    if type_name in _FRACTION:
        return f"{type_name}({scale})"
    if type_name == "float" and precision != 53:
        return f"float({precision})"
    return type_name
