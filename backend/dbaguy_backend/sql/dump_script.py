"""The text of a dump file (spec ⑥ §3.3.2), piece by piece as the dump job writes it: the header, the settings a
restore needs (the literals are written for them: MySQL's backslash escapes, PostgreSQL's standard strings), the DROPs
of a `drop_first` dump, the objects before the data, each table's rows, the objects after the data, and the end (the
`Dump complete` line a cut file lacks, pre-flight M15). UTF-8, LF, a `;` after each statement (T-SQL: a GO line after
each batch; MySQL stored programs between DELIMITER lines). Every comment line goes through `comment_safe` (S15), and
a catalog's statement ending in a comment cannot take its terminator into it (06a Task 6 review T9). Pure."""

from collections import Counter
from collections.abc import Iterable, Iterator

from ..adapters.columns import SQL_VARIANT
from ..adapters.dump import DumpCatalog, DumpDependent, DumpObject, DumpTable, plural, shown_names
from ..domain.data_jobs import DumpContent
from ..domain.profiles import Engine
from .dialects import RULES, ClientCommand, Dialect, identity_insert, qualified, quote_ident, quote_literal
from .dump_header import comment_safe, complete_line, filtered_rows, header_lines
from .lexer import Token, TokenKind, scan

_CONTENT = {"schema": "schema only", "data": "data only", "both": "schema and data"}

# MySQL: what a file restores under, and so what its dump session reads definitions and rows under (adapters/mysql/
# dump.py, pre-flight M5): zero dates and an id of 0 restore as they are (no NO_ZERO_DATE; NO_AUTO_VALUE_ON_ZERO),
# strict, so a value that does not fit stops the run instead of being cut; TIMESTAMPs read and written in UTC
RESTORE_SQL_MODE = "STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO"
# … so a restore reads backslashes as escapes, and a dump's MySQL literals are written for that (never for the mode
# of the server dumped)
RESTORE_BACKSLASH_ESCAPES = "NO_BACKSLASH_ESCAPES" not in RESTORE_SQL_MODE.split(",")
RESTORE_TIME_ZONE = "+00:00"
RESTORE_CHARSET = "utf8mb4"  # SET NAMES
# What a restore must run under (D-06-7, §3.3.3)
RESTORE_SETTINGS: dict[Dialect, tuple[str, ...]] = {
    Dialect.POSTGRES: ("SET client_encoding = 'UTF8'", "SET standard_conforming_strings = on",
                       "SET check_function_bodies = false"),
    Dialect.MYSQL: (f"SET NAMES {RESTORE_CHARSET}", "SET FOREIGN_KEY_CHECKS = 0",
                    f"SET SESSION sql_mode = {quote_literal(Dialect.MYSQL, RESTORE_SQL_MODE)}",
                    f"SET time_zone = {quote_literal(Dialect.MYSQL, RESTORE_TIME_ZONE)}"),
    # no SET NOCOUNT ON (unlike SSMS's scripts): under it no INSERT reports its rows, and the run of the dump would
    # say 0 rows restored (S21)
    Dialect.TSQL: ("SET QUOTED_IDENTIFIER ON", "SET ANSI_NULLS ON"),
    # foreign_keys = OFF does nothing inside a transaction (an all-or-nothing run): defer_foreign_keys then holds the
    # checks until its COMMIT
    Dialect.SQLITE: ("PRAGMA foreign_keys = OFF", "PRAGMA defer_foreign_keys = ON"),
}
# The restores that check each row's foreign keys as it loads (MySQL's header turns the checks off, SQLite's defers
# them to the COMMIT): only there can a data-only restore fail on the order of its rows (pre-flight M9, Task 9 fix M4)
RESTORE_CHECKS_FOREIGN_KEYS = frozenset({Dialect.POSTGRES, Dialect.TSQL})
MAX_GO_REPEAT = 1000  # the largest `GO n` a dump writes: the query tab runs up to this many (Task 9 fix L4)
RESET_SETTINGS: dict[Dialect, tuple[str, ...]] = {
    Dialect.MYSQL: ("SET FOREIGN_KEY_CHECKS = 1",), Dialect.SQLITE: ("PRAGMA foreign_keys = ON",),
}
# The kinds the Content line counts (what a reader looks for); the others (indexes, constraints, policies, …) are in
# the file, not in the count
_COUNTED = ("table", "partition", "view", "materialized view", "sequence", "type", "domain", "function", "aggregate",
            "procedure", "trigger", "event", "synonym")
_COMMENTS = frozenset({TokenKind.COMMENT, TokenKind.EXEC_COMMENT})
MAX_DEFAULT_ROWS = 1000  # rows of defaults a statement (MySQL) or a piece of text (SQLite) holds


def _end(dialect: Dialect) -> str:
    return "\nGO\n" if RULES[dialect].client_command is ClientCommand.GO else ";\n"


def _last(dialect: Dialect, sql: str) -> Token | None:
    last = None
    for last in scan(sql, dialect):
        pass
    return last


def _closed(dialect: Dialect, sql: str) -> str:
    """`sql` such that the terminator after it ends it (T9). SQLite keeps a view's or index's trailing `-- …`, MySQL a
    program's `# …`: written after it, the `;` (`;;`) would be part of the comment and the next statement would run
    into this one, so a line break goes first. A block comment left open at the end (SQLite takes one) is closed; one
    that closing does not end (a quote inside a MySQL version comment) is left out: either way the rest of the file
    never becomes a comment. Scans the statement: for catalog DDL, not for the batcher's 1 MiB INSERTs."""
    last = _last(dialect, sql)
    if last is not None and last.kind in _COMMENTS and not last.terminated:
        for depth in range(1, last.text.count("/*") + 1):  # PostgreSQL and T-SQL nest them
            if (end := _last(dialect, sql + " */" * depth)) is not None and end.terminated:
                sql += " */" * depth
                break
        else:
            sql = sql[:last.start].rstrip()
        last = _last(dialect, sql)
    line_comment = last is not None and last.kind is TokenKind.COMMENT and not last.text.startswith("/*")
    return sql + "\n" if line_comment else sql


def statement_text(dialect: Dialect, sql: str) -> str:
    """One statement as the file writes it: `;` and a line break after it, or a GO line after a T-SQL batch; a
    trailing comment cannot take the terminator (`_closed`). For InsertBatcher's statements, `insert_text`."""
    return _closed(dialect, sql) + _end(dialect)


def insert_text(dialect: Dialect, statement: str) -> str:
    """One of InsertBatcher's statements (or another the dump itself wrote) as the file writes it. It ends in its last
    row's `)`, so no comment can take the terminator, and it is not scanned (the lexer takes 0.25 s a MiB)."""
    return statement + _end(dialect)


def statement_end(dialect: Dialect) -> str:
    """What `insert_text` puts after a statement: a dump writes it on its own after a large INSERT, so the statement's
    text is never copied once more (Task 9 fix M1)."""
    return _end(dialect)


def object_text(dialect: Dialect, item: DumpObject) -> str:
    if item.compound and dialect is Dialect.MYSQL:
        return ("DELIMITER ;;\n" + "".join(f"{_closed(dialect, statement)};;\n" for statement in item.statements)
                + "DELIMITER ;\n")
    return "".join(statement_text(dialect, statement) for statement in item.statements)


def table_sql(dialect: Dialect, table: DumpTable) -> str:
    """The name a dump's INSERTs use (§R-06-3): PostgreSQL and SQL Server with the schema, MySQL and SQLite without
    (the file restores into the database its run uses)."""
    if dialect in (Dialect.POSTGRES, Dialect.TSQL):
        return qualified(dialect, table.schema, table.table)
    return quote_ident(dialect, table.table)


def lost_columns(catalog: DumpCatalog) -> list[str]:
    """The columns whose values a dump cannot carry (sql_variant): the header names them."""
    return [f"{table.name}.{column.name}" for table in catalog.tables for column in table.columns
            if column.not_copied_reason == SQL_VARIANT]


def not_included(catalog: DumpCatalog) -> list[str]:
    """The header's Not included list, which a dump plan shows word for word: what the catalog left out, then the
    values of each column a dump cannot carry."""
    return [*catalog.not_included, *(f"the values of sql_variant column {name}" for name in lost_columns(catalog))]


def drops_objects(content: DumpContent, drop_first: bool) -> bool:
    """Whether the file drops what it creates: a data-only file creates nothing, so it drops nothing (and its header
    says so)."""
    return drop_first and content in ("schema", "both")


def filtered_table(catalog: DumpCatalog, content: DumpContent, filter: str | None) -> str | None:
    """The table whose rows a filter picked, as the file names it (its header's Rows line), when the file holds rows;
    else None. A filter's scope is one table (`filter_fits`)."""
    if filter is None or content == "schema" or not catalog.tables:
        return None
    return catalog.tables[0].name


def filter_warnings(catalog: DumpCatalog, content: DumpContent, drop_first: bool, filter: str | None) -> list[str]:
    """A filtered file that also drops and recreates its table: restoring it replaces the table with the filter's rows
    (06a fix wave F2). The dump's plan and its outcome say it."""
    table = filtered_table(catalog, content, filter)
    if table is None or not drops_objects(content, drop_first):
        return []
    return [filtered_rows(table, replaces=True)]


DROP_BLOCKED = "Drop objects first: "  # how drop_warnings' texts start (the dump dialog shows them first, in red)


def _blocked(target: str, dependents: list[DumpDependent]) -> str:
    if len(dependents) == 1:
        dependent = dependents[0]
        return (f"{DROP_BLOCKED}{dependent.name} ({dependent.kind}, not in this dump) depends on {target}, so restoring "
                "where it exists fails at that DROP. Dump it too, or turn Drop objects first off.")
    named = shown_names([f"{dependent.name} ({dependent.kind})" for dependent in dependents])
    return (f"{DROP_BLOCKED}{named}, not in this dump, depend on {target}, so restoring where they exist fails at that "
            "DROP. Dump them too, or turn Drop objects first off.")


def drop_warnings(catalog: DumpCatalog, content: DumpContent, drop_first: bool) -> list[str]:
    """What a `drop_first` file's DROPs run into (06b I2): one warning per object it drops that something it does not
    drop depends on (`catalog.dependents`: out of the scope, or of a kind left out) — restoring where that exists fails
    at the DROP, since no dump writes CASCADE. The dump's plan and its outcome say it."""
    if not drops_objects(content, drop_first):
        return []
    dropped = {item.name for item in (*catalog.pre, *catalog.post) if item.drop}
    blocking: dict[str, list[DumpDependent]] = {}
    for dependent in dict.fromkeys(catalog.dependents):
        if dependent.target in dropped:
            blocking.setdefault(dependent.target, []).append(dependent)
    return [_blocked(target, dependents) for target, dependents in blocking.items()]


def restore_warnings(catalog: DumpCatalog, content: DumpContent, drop_first: bool, filter: str | None) -> list[str]:
    """What restoring the file does that its reader must know first: it replaces a filtered table's rows (F2), or its
    DROPs fail (I2). The dump's plan and its outcome put them before the catalog's own warnings."""
    return [*filter_warnings(catalog, content, drop_first, filter), *drop_warnings(catalog, content, drop_first)]


def default_rows_text(dialect: Dialect, table: DumpTable, count: int) -> Iterator[str]:
    """`count` rows of a table with no column a dump can write (only rowversion, computed or sql_variant columns; a
    PostgreSQL table of no columns): each a row of its defaults, so the restore has as many rows as the source (06a
    Task 7 review). PostgreSQL in one statement; MySQL MAX_DEFAULT_ROWS rows a statement; T-SQL one batch run n times
    (`GO n`, which Run SQL file runs and counts each time), n at most MAX_GO_REPEAT, as the query tab runs it; SQLite
    (whose tables always have such a column) a statement a row."""
    if count <= 0:
        return
    name = table_sql(dialect, table)
    if dialect is Dialect.POSTGRES:
        yield insert_text(dialect, f"INSERT INTO {name} SELECT FROM generate_series(1, {count})")
    elif dialect is Dialect.MYSQL:
        for start in range(0, count, MAX_DEFAULT_ROWS):
            rows = min(MAX_DEFAULT_ROWS, count - start)
            yield insert_text(dialect, f"INSERT INTO {name} () VALUES " + ", ".join(["()"] * rows))
    elif dialect is Dialect.TSQL:
        for start in range(0, count, MAX_GO_REPEAT):
            yield f"INSERT INTO {name} DEFAULT VALUES\nGO {min(MAX_GO_REPEAT, count - start)}\n"
    else:
        statement = insert_text(dialect, f"INSERT INTO {name} DEFAULT VALUES")
        for start in range(0, count, MAX_DEFAULT_ROWS):
            yield statement * min(MAX_DEFAULT_ROWS, count - start)


def _comment(text: str) -> str:
    return f"-- {comment_safe(text)}\n"


class DumpScript:
    """The pieces of one dump's text, in the order the file holds them: head(), pre(), for each table table_head(),
    its INSERTs (`insert_text`) and table_tail(), post(), tail()."""

    def __init__(
        self, dialect: Dialect, engine: Engine, catalog: DumpCatalog, *, content: DumpContent, drop_first: bool,
        created: str, filter: str | None = None,
    ) -> None:
        self.dialect, self.engine, self.catalog = dialect, engine, catalog
        self.content, self.drop_first, self.created = content, drop_first, created
        self.schema = content in ("schema", "both")
        self.data = content in ("data", "both")
        self.drops = drops_objects(content, drop_first)
        table = filtered_table(catalog, content, filter)
        self.filtered = (table, filter) if table is not None and filter is not None else None  # the Rows line

    def _written(self, objects: Iterable[DumpObject]) -> list[DumpObject]:
        """The objects this content writes: the structure's, the data's (setval, counters), or both; a data-only
        dump's own (PostgreSQL's REFRESH MATERIALIZED VIEW) only there."""
        wanted = {"schema": self.schema, "data": self.data, "data_only": self.data and not self.schema}
        return [item for item in objects if wanted[item.section]]

    def summary(self) -> str:
        counts = Counter(item.kind for item in (*self._written(self.catalog.pre), *self._written(self.catalog.post)))
        if not self.schema:
            counts["table"] = len(self.catalog.tables)
        parts = [plural(kind, counts[kind]) for kind in _COUNTED if counts[kind]]
        return _CONTENT[self.content] + (" · " + ", ".join(parts) if parts else "")

    def head(self) -> str:
        """The header, the restore's settings, and a `drop_first` file's DROPs: those of the objects after the data
        newest first, then those before it (H7: a foreign key's goes before its tables'). Only objects the file
        creates again are dropped."""
        lines = header_lines(
            engine=self.engine, server=self.catalog.server, scope=self.catalog.scope, created=self.created,
            content=self.summary(), snapshot=self.catalog.snapshot_text, not_included=not_included(self.catalog),
            drop_first=self.drops, filtered=self.filtered,
        )
        text = "\n".join(lines) + "\n\n" + "".join(insert_text(self.dialect, s) for s in RESTORE_SETTINGS[self.dialect])
        if self.drops:
            drops = [item.drop for item in (*reversed(self.catalog.post), *reversed(self.catalog.pre))
                     if item.drop and item.statements]
            if drops:
                text += "\n" + _comment("Drop objects first") + "".join(
                    statement_text(self.dialect, drop) for drop in drops)
        return text

    def pre(self) -> str:
        return self._section(self._written(self.catalog.pre))

    def table_head(self, table: DumpTable) -> str:
        text = "\n" + _comment(f"Data: {table.name}")
        if (switch := self._identity(table)) is not None:
            text += insert_text(self.dialect, switch[0])
        return text

    def table_tail(self, table: DumpTable, rows: int) -> str:
        text = ""
        if (switch := self._identity(table)) is not None:
            text += insert_text(self.dialect, switch[1])
        return text + _comment(f"{table.name}: {rows:,} rows")

    def post(self) -> str:
        """The objects after the data; in a data-only file, only the data's own (setval, counters)."""
        return self._section(self._written(self.catalog.post))

    def tail(self, rows: int) -> str:
        reset = "".join(insert_text(self.dialect, s) for s in RESET_SETTINGS.get(self.dialect, ()))
        tables = len(self.catalog.tables) if self.data else 0
        return "\n" + reset + complete_line(rows, tables) + "\n"

    def _identity(self, table: DumpTable) -> tuple[str, str] | None:
        if not any(column.identity == "always" and column.copied for column in table.columns):
            return None
        return identity_insert(self.dialect, table_sql(self.dialect, table))

    def _section(self, objects: list[DumpObject]) -> str:
        return "".join("\n" + _comment(f"{item.kind}: {item.name}") + object_text(self.dialect, item)
                       for item in objects if item.statements)
