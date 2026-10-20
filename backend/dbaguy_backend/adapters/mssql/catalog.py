"""What the SQL Server object tree and the schema snapshot share: system names, quoting, and reading catalog rows.

Internal SQL only. Names reach a statement through `q()` (identifiers), `literal()` (values) or `object_id_sql()`,
never through driver parameters: pymssql would substitute `%`, and user SQL must not be touched either (the same
helpers keep one rule for both).
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from ...sql.dialects import Dialect, force_quote_ident, quote_literal
from . import driver

SYSTEM_DATABASES = frozenset({"master", "model", "msdb", "tempdb"})
SYSTEM_SCHEMAS = frozenset({"sys", "INFORMATION_SCHEMA", "guest"})
# The fixed database roles (db_owner, db_datareader, …) each own a schema with the id of the role.
_ROLE_SCHEMA_IDS = range(16384, 16400)

CHARS = frozenset({"char", "varchar", "binary", "varbinary"})
NCHARS = frozenset({"nchar", "nvarchar"})  # max_length counts bytes: two per character
_SCALED = frozenset({"decimal", "numeric"})
FRACTION = frozenset({"datetime2", "datetimeoffset", "time"})  # (scale): the digits of the fraction of a second
# Object ids inlined into one catalog query at most: an IN list of many thousands of values can make the optimizer give
# up (8623); a dump of more objects reads them in chunks, still a fixed number of queries per thousand objects
ID_CHUNK = 1000


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


def char_length(type_name: str, max_length: int) -> int | None:
    """A char, binary or Unicode type's declared length from sys.columns / sys.types' max_length, in its own unit
    (characters for nchar and nvarchar, whose max_length counts bytes); None for (max)."""
    if max_length == -1:
        return None
    return max_length // 2 if type_name in NCHARS else max_length


def type_sql(type_name: str, max_length: int, precision: int, scale: int) -> str:
    """A column's type as T-SQL writes it, from sys.types / sys.columns. An alias type (`email_address`) keeps its
    name: its length and precision are the alias's."""
    if type_name in NCHARS or type_name in CHARS:
        length = char_length(type_name, max_length)
        return f"{type_name}({'max' if length is None else length})"
    if type_name in _SCALED:
        return f"{type_name}({precision},{scale})"
    if type_name in FRACTION:
        return f"{type_name}({scale})"
    if type_name == "float" and precision != 53:
        return f"float({precision})"
    return type_name


def id_list(ids: Iterable[int]) -> str:
    """Object ids inlined into a catalog query (integers only); `NULL` for none, which matches nothing."""
    return ", ".join(str(int(value)) for value in ids) or "NULL"


def chunks(ids: Iterable[int]) -> Iterator[str]:
    """`ids` as id_list texts of at most ID_CHUNK ids each; none for no ids."""
    found = list(ids)
    for start in range(0, len(found), ID_CHUNK):
        yield id_list(found[start:start + ID_CHUNK])


def id_sources(ids: Iterable[int], within: str | None = None) -> Iterator[str]:
    """What the `IN (…)` of a catalog query over `ids` takes: `within`, a subquery giving at least them (one query,
    which the server compiles fast: a list of a thousand literal ids costs ~0.1 s to compile per statement, every dump
    anew), or the ids themselves, chunks at a time. The reader keeps only the rows of its ids; no ids, no query."""
    found = list(ids)
    if not found:
        return
    if within is not None:
        yield within
    else:
        yield from chunks(found)


@dataclass(frozen=True, slots=True)
class PerObject:
    """A catalog query over one object's rows (`one(o)`: `o` an id expression such as object_id_sql's) or over many
    objects' (`many(ids)`: an id_list, or a subquery giving ids; each row then starts with its object's id, and an
    object's rows come together, in the order `one` gives them). `sql` holds `{key}` before its first column, `{where}`
    for its filter and `{order}` before the first key of its ORDER BY."""

    sql: str
    column: str  # the object id column the query filters on

    def one(self, o: str) -> str:
        return self.sql.format(key="", where=f"{self.column} = {o}", order="")

    def many(self, ids: str) -> str:
        return self.sql.format(key=f"{self.column}, ", where=f"{self.column} IN ({ids})", order=f"{self.column}, ")


def by_object(found: list[tuple]) -> dict[int, list[tuple]]:
    """PerObject.many's rows by their object's id, without it, in the order they came."""
    grouped: dict[int, list[tuple]] = {}
    for row in found:
        grouped.setdefault(int(row[0]), []).append(row[1:])
    return grouped
