"""What a MySQL dump writes, and in which order (spec ⑥ §3.3.3), read inside the dump's snapshot. The dump session
`USE`s the database, so SHOW CREATE VIEW leaves the database out of names (it writes every name in full otherwise,
probed) and the file restores into whichever database its run uses. It reads every definition under the sql_mode the
file restores under (pre-flight M5: under ANSI_QUOTES, SHOW CREATE TABLE and VIEW quote with ", which the restore
cannot parse).

Order. Before the data: tables, as SHOW CREATE TABLE writes them (foreign keys inside: the header turns
FOREIGN_KEY_CHECKS off; AUTO_INCREMENT kept, read live outside the snapshot: S10). After the data: routines (a body
may name a table that is not there yet), views by VIEW_TABLE_USAGE (a view calling a missing function is 1305), the
tables' triggers in ACTION_ORDER (SHOW CREATE TRIGGER leaves FOLLOWS / PRECEDES out: made in that order, they run in
it again), events. Every stored program is written between DELIMITER lines, under the sql_mode it was created with
(S20; an event also under its own time_zone, which its STARTS and ENDS are written in: pre-flight L13), and without
its DEFINER (the account that runs the file becomes it; keeping it needs SUPER or SET_USER_ID, 1227; A14). Routines
and events follow `routines`, as in S17.

A `tables` scope writes its tables and views, the tables' triggers, and the functions its views call
(VIEW_ROUTINE_USAGE). Only InnoDB reads from the snapshot: any other table is read as the dump reaches it, and the
header says so (pre-flight M6). Every object of the scope that is not written is counted for the header (H6)."""

import mysql.connector
from loguru import logger
from mysql.connector.connection import MySQLConnection

from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ...domain.errors import NotFound, ValidationFailed
from ...sql.dialects import Dialect, qualified, quote_ident, quote_literal
from ...sql.lexer import INSIGNIFICANT, Token, TokenKind, scan
from ...sql.splitter import after_account
from ..dump import (
    OWNERS_AND_PRIVILEGES, Consistency, DumpCatalog, DumpObject, DumpSession, DumpTable, Ordering, kept_foreign_key,
)
from . import catalog
from .columns import read_columns

# The file's own settings (its header, sql/dump_script.py): zero dates and an id of 0 restore as they are, strict
RESTORE_SQL_MODE = "STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO"
RESTORE_TIME_ZONE = "+00:00"
SNAPSHOT_TEXT = "one consistent snapshot (InnoDB)"
DEFINERS_LEFT_OUT = "DEFINER clauses (the account running the file becomes the definer)"  # pre-flight L6
_SHOWN_NAMES = 10  # names a warning lists before "…"
_UNKNOWN_TABLE = 1109  # an information_schema table this server does not have (before 8.0.13, MariaDB)
_SCHEMA = "SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s"
_RELATIONS = """
SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS, DATA_LENGTH + INDEX_LENGTH, ENGINE
FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME
"""
_FOREIGN_KEYS = """
SELECT TABLE_NAME, CONSTRAINT_NAME, UNIQUE_CONSTRAINT_SCHEMA, REFERENCED_TABLE_NAME
FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = %s ORDER BY TABLE_NAME, CONSTRAINT_NAME
"""
_VIEW_USES = """
SELECT VIEW_NAME, TABLE_NAME FROM information_schema.VIEW_TABLE_USAGE
WHERE VIEW_SCHEMA = %s AND TABLE_SCHEMA = %s
"""
_VIEW_ROUTINES = """
SELECT TABLE_NAME, SPECIFIC_NAME FROM information_schema.VIEW_ROUTINE_USAGE
WHERE TABLE_SCHEMA = %s AND SPECIFIC_SCHEMA = %s
"""
# ROUTINE_DEFINITION is NULL exactly when SHOW CREATE gives none (another definer's, without SHOW_ROUTINE; probed):
# a plan, which reads no definition, knows it too
_ROUTINES = """
SELECT ROUTINE_NAME, ROUTINE_TYPE, ROUTINE_DEFINITION IS NULL FROM information_schema.ROUTINES
WHERE ROUTINE_SCHEMA = %s ORDER BY ROUTINE_NAME
"""
_TRIGGERS = """
SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s
ORDER BY EVENT_OBJECT_TABLE, ACTION_ORDER
"""
_EVENTS = "SELECT EVENT_NAME FROM information_schema.EVENTS WHERE EVENT_SCHEMA = %s ORDER BY EVENT_NAME"
_HISTOGRAMS = "SELECT TABLE_NAME FROM information_schema.COLUMN_STATISTICS WHERE SCHEMA_NAME = %s"
# SHOW CREATE <word>: the index of its sql_mode, its DDL and (an event's) time_zone in the row
_PROGRAMS = {"procedure": ("PROCEDURE", 1, 2, None), "function": ("FUNCTION", 1, 2, None),
             "trigger": ("TRIGGER", 1, 2, None), "event": ("EVENT", 1, 3, 2)}
# CREATE ALGORITHM = UNDEFINED DEFINER = `u` @ `h` SQL SECURITY DEFINER VIEW …: DEFINER comes before the object's word
_HEAD_TOKENS = 24
_OBJECT_WORDS = frozenset({"PROCEDURE", "FUNCTION", "TRIGGER", "EVENT", "VIEW"})


def session(database: str) -> DumpSession:
    return DumpSession(
        setup=(f"USE {quote_ident(Dialect.MYSQL, database)}", "SET NAMES utf8mb4",
               f"SET SESSION sql_mode = {quote_literal(Dialect.MYSQL, RESTORE_SQL_MODE)}",  # pre-flight M5
               f"SET time_zone = {quote_literal(Dialect.MYSQL, RESTORE_TIME_ZONE)}",
               "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"),
        teardown=("ROLLBACK",), consistency="snapshot",
    )


def strip_definer(sql: str) -> str:
    """`sql` without its `DEFINER = account` clause (A14: user@host, 'u'@'h', `u`@`h`, "u"@"h", CURRENT_USER[()]),
    the one before the object's word; SQL SECURITY DEFINER stays, and nothing after the object's word is looked at."""
    head: list[Token] = []
    for token in scan(sql, Dialect.MYSQL):
        if token.kind in INSIGNIFICANT or token.kind == TokenKind.EXEC_COMMENT:
            continue
        head.append(token)
        if (token.kind == TokenKind.WORD and token.upper in _OBJECT_WORDS) or len(head) >= _HEAD_TOKENS:
            break
    for i, token in enumerate(head):
        if token.kind == TokenKind.WORD and token.upper == "DEFINER" and i + 1 < len(head) and head[i + 1].text == "=":
            end = after_account(head, i + 1)
            stop = head[end].start if end < len(head) else head[-1].end
            return sql[:token.start] + sql[stop:]
    return sql


def _set_session(sql_mode: str, time_zone: str | None = None) -> str:
    text = f"SET SESSION sql_mode = {quote_literal(Dialect.MYSQL, sql_mode)}"
    return text + (f", SESSION time_zone = {quote_literal(Dialect.MYSQL, time_zone)}" if time_zone is not None else "")


def _shown(names: list[str]) -> str:
    return ", ".join(names[:_SHOWN_NAMES]) + (" …" if len(names) > _SHOWN_NAMES else "")


class _Reader:
    """One catalog read of `database`; `ddl` False (a dump plan) reads which objects there are, not their
    definitions (and so cannot know which of them are unreadable)."""

    def __init__(self, conn: MySQLConnection, database: str, scope: DumpScope, kinds: DumpObjectKinds,
                 ddl: bool) -> None:
        self.conn, self.database, self.scope, self.kinds, self.ddl = conn, database, scope, kinds, ddl
        self.named = scope.kind == "tables"
        self.left_out: dict[str, int] = {}  # counted for the header's Not included
        self.warnings: list[str] = []
        self.outside: list[str] = []  # the tables written that are not InnoDB: not in the snapshot (M6)

    def rows(self, sql: str, *params: object) -> list[tuple]:
        return catalog.query(self.conn, sql, params)

    def optional_rows(self, sql: str, *params: object) -> list[tuple]:
        """An information_schema table that MySQL 8.0.13 added (VIEW_TABLE_USAGE, VIEW_ROUTINE_USAGE) or that 8.0 did
        (COLUMN_STATISTICS): no rows on a server without it (MariaDB)."""
        try:
            return self.rows(sql, *params)
        except mysql.connector.Error as exc:
            if exc.errno != _UNKNOWN_TABLE:
                raise
            logger.debug("MySQL error {} reading the dump catalog: that table is not on this server", exc.errno)
            return []

    def leave_out(self, what: str, count: int = 1) -> None:
        if count:
            self.left_out[what] = self.left_out.get(what, 0) + count

    def show(self, word: str, name: str, index: int) -> tuple | None:
        """SHOW CREATE <word> <name>'s row, or None when the account may not read the definition (it is left out)."""
        try:
            row = catalog.first_row(self.conn, f"SHOW CREATE {word} {qualified(Dialect.MYSQL, self.database, name)}")
        except mysql.connector.Error as exc:
            if exc.errno not in catalog.UNREADABLE_DEFINITION_ERRNOS:
                raise
            logger.debug("MySQL error {} on SHOW CREATE {} {}: left out of the dump", exc.errno, word, name)
            row = None
        if row is None or not row[index]:
            self.leave_out(f"{word.lower()}s whose definition cannot be read")
            return None
        return row

    def read(self) -> tuple[list[DumpObject], list[DumpTable], list[DumpObject]]:
        relations = self.rows(_RELATIONS, self.database)
        if self.named:
            wanted = set(self.scope.names)
            if missing := wanted - {row[0] for row in relations}:
                raise NotFound(f"Not found in {self.database}: {', '.join(sorted(missing))}")
            relations = [row for row in relations if row[0] in wanted]
        for kind in sorted({row[1] for row in relations} - {"BASE TABLE", "VIEW"}):  # MariaDB's SEQUENCE, …
            self.leave_out(f"relations of type {kind}", sum(row[1] == kind for row in relations))
        pre: list[DumpObject] = []
        tables: list[tuple] = []
        for row in (row for row in relations if row[1] == "BASE TABLE"):
            if (table := self._table(row[0])) is not None:
                pre.append(table)
                tables.append(row)
        names = {row[0] for row in tables}
        views = [row[0] for row in relations if row[1] == "VIEW"]
        if not (self.kinds.views or self.named):
            self.leave_out("views (not chosen)", len(views))
            views = []
        post = [*self._routines(views), *self._views(views), *self._triggers(names), *self._events()]
        self.leave_out("column histograms", sum(table in names for (table,) in self.optional_rows(
            _HISTOGRAMS, self.database)))
        references = self._foreign_keys(names)
        self.outside = [row[0] for row in tables if (row[4] or "").lower() != "innodb"]
        return pre, [self._data(row, references.get(row[0], ())) for row in tables], post

    def _table(self, name: str) -> DumpObject | None:
        statements: tuple[str, ...] = ()
        if self.ddl:
            row = self.show("TABLE", name, 1)
            if row is None:
                return None
            statements = (row[1],)
        return DumpObject("table", name, statements, f"DROP TABLE IF EXISTS {quote_ident(Dialect.MYSQL, name)}")

    def _data(self, row: tuple, references: tuple[str, ...]) -> DumpTable:
        name, _, estimate, size, _ = row
        return DumpTable(schema=self.database, table=name, name=name,
                         columns=tuple(read_columns(self.conn, self.database, name)),
                         rows_estimate=int(estimate) if estimate is not None else None,
                         bytes_estimate=int(size) if size is not None else None, references=references)

    def _foreign_keys(self, names: set[str]) -> dict[str, tuple[str, ...]]:
        """The dumped tables each table's foreign keys reference (pre-flight M9); a foreign key to a table outside the
        dump stays in its table's DDL, and a warning says the file needs that table."""
        found: dict[str, set[str]] = {}
        for table, key, schema, target in self.rows(_FOREIGN_KEYS, self.database):
            if table not in names:
                continue
            if schema == self.database and target in names:
                if target != table:
                    found.setdefault(table, set()).add(target)
            else:
                where = target if schema == self.database else f"{schema}.{target}"
                self.warnings.append(kept_foreign_key(table, where, key))
        return {table: tuple(sorted(targets)) for table, targets in found.items()}

    def _routines(self, views: list[str]) -> list[DumpObject]:
        """Every routine, or (a `tables` scope, routines not chosen) the functions the written views call."""
        routines = self.rows(_ROUTINES, self.database)
        if self.kinds.routines and not self.named:
            chosen = routines
        else:
            called = {name for view, name in self.optional_rows(_VIEW_ROUTINES, self.database, self.database)
                      if view in views}
            chosen = [row for row in routines if row[1] == "FUNCTION" and row[0] in called]
            if not self.named:
                self.leave_out("functions and procedures (not chosen)", len(routines) - len(chosen))
        found = []
        for name, kind, unreadable in chosen:
            if unreadable:
                self.leave_out(f"{kind.lower()}s whose definition cannot be read")
            elif (obj := self._program(kind.lower(), name)) is not None:
                found.append(obj)
        return found

    def _events(self) -> list[DumpObject]:
        if self.named:
            return []
        events = self.rows(_EVENTS, self.database)
        if not self.kinds.routines:
            self.leave_out("events (not chosen)", len(events))
            return []
        return [obj for (name,) in events if (obj := self._program("event", name)) is not None]

    def _triggers(self, tables: set[str]) -> list[DumpObject]:
        triggers = [name for name, table in self.rows(_TRIGGERS, self.database) if table in tables]
        if not self.kinds.triggers:
            self.leave_out("triggers (not chosen)", len(triggers))
            return []
        return [obj for name in triggers if (obj := self._program("trigger", name)) is not None]

    def _program(self, kind: str, name: str) -> DumpObject | None:
        word, mode_index, ddl_index, zone_index = _PROGRAMS[kind]
        statements: tuple[str, ...] = ()
        if self.ddl:
            row = self.show(word, name, ddl_index)
            if row is None:
                return None
            zone = None if zone_index is None else str(row[zone_index] or "SYSTEM")
            statements = (_set_session(str(row[mode_index] or ""), zone), strip_definer(row[ddl_index]),
                          _set_session(RESTORE_SQL_MODE, None if zone is None else RESTORE_TIME_ZONE))
        return DumpObject(kind, name, statements, f"DROP {word} IF EXISTS {quote_ident(Dialect.MYSQL, name)}",
                          compound=True)

    def _views(self, names: list[str]) -> list[DumpObject]:
        ordering = Ordering()
        for view, uses in self.optional_rows(_VIEW_USES, self.database, self.database) if names else []:
            ordering.depends.setdefault(view, set()).add(uses)
        found = []
        for name in ordering.order(names):
            statements: tuple[str, ...] = ()
            if self.ddl:
                row = self.show("VIEW", name, 1)
                if row is None:
                    continue
                statements = (strip_definer(row[1]),)
            found.append(DumpObject("view", name, statements,
                                    f"DROP VIEW IF EXISTS {quote_ident(Dialect.MYSQL, name)}"))
        return found


def dump_catalog(
    conn: MySQLConnection, database: str | None, scope: DumpScope, kinds: DumpObjectKinds, server: str, *,
    ddl: bool = True,
) -> DumpCatalog:
    """`database`: the connection's current one (DATABASE()); with `ddl` it must be the dumped one, which the dump
    session USEs (SHOW CREATE VIEW names everything in full otherwise)."""
    if scope.kind == "database":
        raise ValidationFailed("A MySQL dump covers one database: dump its schema node")  # S18
    name = scope.schema_name or database or ""
    if catalog.first_row(conn, _SCHEMA, (name,)) is None:
        raise NotFound(f"Database {name} not found")
    if ddl and database != name:
        raise RuntimeError(f"The dump session USEs {name} before its catalog is read (the connection is in {database})")
    reader = _Reader(conn, name, scope, kinds, ddl)
    pre, tables, post = reader.read()
    left_out = [OWNERS_AND_PRIVILEGES, *(f"{count:,} {what}" for what, count in reader.left_out.items())]
    if post:  # views and stored programs: their DEFINER is not written (A14)
        left_out.append(DEFINERS_LEFT_OUT)
    consistency: Consistency = "snapshot"
    snapshot, warnings = SNAPSHOT_TEXT, list(reader.warnings)
    if reader.outside:  # pre-flight M6
        consistency = "per_table"
        snapshot = (f"one consistent snapshot of the InnoDB tables; {len(reader.outside):,} other tables read as the "
                    "dump reached them")
        warnings.insert(0, f"Not in the snapshot (not InnoDB): {_shown(reader.outside)}")
    return DumpCatalog(server=server, scope=f"database {name}", consistency=consistency, pre=tuple(pre),
                       tables=tuple(tables), post=tuple(post), not_included=tuple(left_out),
                       warnings=tuple(warnings), snapshot_text=snapshot)
