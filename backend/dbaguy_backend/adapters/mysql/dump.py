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
its DEFINER (the account that runs the file becomes it; keeping it needs SUPER or SET_USER_ID, 1227; A14). Programs
and views are made under their own collation_connection, which their literals compare by, and character_set_client
(review T6). Routines and events follow `routines`, as in S17.

What cannot be written takes what needs it out with it, said by name (review T3–T5): a program made under
NO_BACKSLASH_ESCAPES (DbaGuy splits files with backslash escapes, whatever a statement's mode), a routine the login
cannot read, an invalid view; then the views that use them (VIEW_ROUTINE_USAGE, VIEW_TABLE_USAGE) and the triggers and
events that call them. A MERGE or FEDERATED table's rows are other tables' and are not written (T2).

A `tables` scope writes its tables and views, the tables' triggers, and the functions its views call
(VIEW_ROUTINE_USAGE). Only InnoDB reads from the snapshot: any other table is read as the dump reaches it, and the
header says so (pre-flight M6). Every object of the scope that is not written is counted for the header (H6)."""

from typing import NamedTuple

import mysql.connector
from loguru import logger
from mysql.connector.connection import MySQLConnection

from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ...domain.errors import NotFound, ValidationFailed
from ...sql.dialects import Dialect, qualified, quote_ident, quote_literal
from ...sql.lexer import INSIGNIFICANT, Token, TokenKind, scan
from ...sql.scope import routines_called
from ...sql.splitter import after_account
from ..columns import TableColumn
from ..dump import (
    OWNERS_AND_PRIVILEGES, Consistency, DumpCatalog, DumpObject, DumpSession, DumpTable, Ordering, kept_foreign_key,
    shown_names,
)
from . import catalog
from .columns import schema_columns

# The file's own settings (its header, sql/dump_script.py): zero dates and an id of 0 restore as they are, strict
RESTORE_SQL_MODE = "STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO"
RESTORE_TIME_ZONE = "+00:00"
RESTORE_CHARSET = "utf8mb4"  # SET NAMES
SNAPSHOT_TEXT = "one consistent snapshot (InnoDB)"
DEFINERS_LEFT_OUT = "DEFINER clauses (the account running the file becomes the definer)"  # pre-flight L6
NO_BACKSLASH_ESCAPES = "NO_BACKSLASH_ESCAPES"
_UNKNOWN_TABLE = 1109  # an information_schema table this server does not have (before 8.0.13, MariaDB)
_ROWS_ELSEWHERE = frozenset({"mrg_myisam", "federated"})  # T2: a MERGE table's rows are its tables', FEDERATED's remote
_VALID_VIEW = "VIEW"  # a view's TABLE_COMMENT; an invalid one's holds the server's message instead (T5, probed)
_UTF8_CLIENTS = frozenset({"utf8mb4", "utf8mb3", "utf8"})  # read the file's UTF-8 as it is
# Its default, and utf8mb4's (what a file restores under; a parameter without a character set takes the database's)
_SCHEMA = """
SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME,
       (SELECT COLLATION_NAME FROM information_schema.COLLATIONS WHERE CHARACTER_SET_NAME = 'utf8mb4'
        AND IS_DEFAULT = 'Yes')
FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s
"""
_RELATIONS = """
SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS, DATA_LENGTH + INDEX_LENGTH, ENGINE, TABLE_COMMENT
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
# a plan, which reads no definition, knows it too. The programs' sql_mode and bodies (what they call) are read here
# for the same reason
_ROUTINES = """
SELECT ROUTINE_NAME, ROUTINE_TYPE, ROUTINE_DEFINITION IS NULL, SQL_MODE FROM information_schema.ROUTINES
WHERE ROUTINE_SCHEMA = %s ORDER BY ROUTINE_NAME
"""
_TRIGGERS = """
SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, SQL_MODE, ACTION_STATEMENT FROM information_schema.TRIGGERS
WHERE TRIGGER_SCHEMA = %s ORDER BY EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_ORDER
"""
_EVENTS = """
SELECT EVENT_NAME, SQL_MODE, EVENT_DEFINITION FROM information_schema.EVENTS WHERE EVENT_SCHEMA = %s
ORDER BY EVENT_NAME
"""
_HISTOGRAMS = "SELECT TABLE_NAME FROM information_schema.COLUMN_STATISTICS WHERE SCHEMA_NAME = %s"
# CREATE ALGORITHM = UNDEFINED DEFINER = `u` @ `h` SQL SECURITY DEFINER VIEW …: DEFINER comes before the object's word
_HEAD_TOKENS = 24
_OBJECT_WORDS = frozenset({"PROCEDURE", "FUNCTION", "TRIGGER", "EVENT", "VIEW"})


class _Show(NamedTuple):
    """SHOW CREATE <word>: where its DDL, sql_mode, time_zone, character_set_client and collation_connection are."""

    word: str
    ddl: int
    sql_mode: int | None = None
    time_zone: int | None = None
    client: int | None = None
    collation: int | None = None


_SHOW = {"table": _Show("TABLE", 1), "view": _Show("VIEW", 1, client=2, collation=3),
         "procedure": _Show("PROCEDURE", 2, 1, None, 3, 4), "function": _Show("FUNCTION", 2, 1, None, 3, 4),
         "trigger": _Show("TRIGGER", 2, 1, None, 3, 4), "event": _Show("EVENT", 3, 1, 2, 4, 5)}


def session(database: str) -> DumpSession:
    return DumpSession(
        setup=(f"USE {quote_ident(Dialect.MYSQL, database)}", f"SET NAMES {RESTORE_CHARSET}",
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


def _literal(text: str) -> str:
    return quote_literal(Dialect.MYSQL, text)


def own_settings(ddl: str, client: str, collation: str, sql_mode: str | None = None,
                 time_zone: str | None = None) -> str:
    """The SET an object is made under: a program's sql_mode (an event's time_zone too), and the
    collation_connection its literals compare by and its character_set_client (T6). The file is UTF-8: a client
    character set that would read it otherwise stays utf8mb4 unless the definition is ASCII (latin1 makes 'é' 'Ã©',
    probed); what the definition means is the same, as its literals take collation_connection's."""
    parts = [] if sql_mode is None else [f"SESSION sql_mode = {_literal(sql_mode)}"]
    if time_zone is not None:
        parts.append(f"SESSION time_zone = {_literal(time_zone)}")
    reads_utf8 = client.lower() in _UTF8_CLIENTS or ddl.isascii()
    parts += [f"SESSION character_set_client = {_literal(client if reads_utf8 else RESTORE_CHARSET)}",
              f"SESSION collation_connection = {_literal(collation)}"]
    return "SET " + ", ".join(parts)


def restored_settings(sql_mode: bool = False, time_zone: bool = False) -> str:
    """The SET that brings the file's own settings back after an object."""
    parts = [f"NAMES {RESTORE_CHARSET}"]
    if sql_mode:
        parts.append(f"SESSION sql_mode = {_literal(RESTORE_SQL_MODE)}")
    if time_zone:
        parts.append(f"SESSION time_zone = {_literal(RESTORE_TIME_ZONE)}")
    return "SET " + ", ".join(parts)


def _modes(value: object) -> set[str]:
    """A sql_mode as information_schema gives it (a SET column: the driver gives a set) or SHOW CREATE does (text)."""
    if isinstance(value, set | frozenset):
        return {str(mode).upper() for mode in value}
    return {mode.strip().upper() for mode in str(value or "").split(",") if mode.strip()}


def _mode_text(value: object) -> str:
    return ",".join(sorted(value)) if isinstance(value, set | frozenset) else str(value or "")


class _Reader:
    """One catalog read of `database`; `ddl` False (a dump plan) reads which objects there are, not their
    definitions: whatever decides that an object is left out is read from information_schema, so a plan lists what
    the dump writes (a definition SHOW CREATE refuses aside)."""

    def __init__(self, conn: MySQLConnection, database: str, scope: DumpScope, kinds: DumpObjectKinds,
                 ddl: bool) -> None:
        self.conn, self.database, self.scope, self.kinds, self.ddl = conn, database, scope, kinds, ddl
        self.named = scope.kind == "tables"
        self.left_out: dict[str, int] = {}  # counted for the header's Not included
        self.warnings: list[str] = []
        self.outside: list[str] = []  # the tables whose rows are read that are not InnoDB: not in the snapshot (M6)
        self.no_backslash: list[str] = []  # T3: "kind name" of each, for the warning
        self.needing: list[str] = []  # T4: what uses a routine or view the dump leaves out
        self.invalid: list[str] = []  # T5
        self.cannot: set[str] = set()  # the routines no file can carry (unreadable, NO_BACKSLASH_ESCAPES), folded

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

    def show(self, kind: str, name: str) -> tuple | None:
        """SHOW CREATE's row, or None when the account may not read the definition (it is left out)."""
        word, index = _SHOW[kind].word, _SHOW[kind].ddl
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
        views = [row for row in relations if row[1] == "VIEW"]
        if not (self.kinds.views or self.named):
            self.leave_out("views (not chosen)", len(views))
            views = []
        uses_routines: dict[str, set[str]] = {}
        for view, routine in self.optional_rows(_VIEW_ROUTINES, self.database, self.database) if views else []:
            uses_routines.setdefault(view, set()).add(routine.lower())
        routines = self._routines({routine for row in views for routine in uses_routines.get(row[0], ())})
        post = [*routines, *self._views(views, uses_routines), *self._triggers(names), *self._events()]
        self._said()
        self.leave_out("column histograms", sum(table in names for (table,) in self.optional_rows(
            _HISTOGRAMS, self.database)))
        references = self._foreign_keys(names)
        elsewhere = [row for row in tables if (row[4] or "").lower() in _ROWS_ELSEWHERE]  # T2: the DDL only
        self.leave_out("MERGE and FEDERATED tables written without their rows (other tables hold them)",
                       len(elsewhere))
        read = [row for row in tables if row not in elsewhere]
        self.outside = [row[0] for row in read if (row[4] or "").lower() != "innodb"]
        columns = schema_columns(self.conn, self.database)  # T7: one query, not one per table
        return pre, [self._data(row, columns.get(row[0], []), references.get(row[0], ())) for row in read], post

    def _said(self) -> None:
        """The objects left out because of what they are or use, counted (H6) and named (T3–T5)."""
        for names, what, why in (
            (self.no_backslash, "stored programs made under NO_BACKSLASH_ESCAPES",
             "made under NO_BACKSLASH_ESCAPES (the file reads a backslash in a string as an escape)"),
            (self.invalid, "invalid views", "invalid where they are (a table, column or function they use is gone, "
                                            "or their definer lacks rights to it)"),
            (self.needing, "views, triggers and events that use a routine or view the dump leaves out",
             "they use a routine or view the dump leaves out"),
        ):
            self.leave_out(what, len(names))
            if names:
                self.warnings.append(f"Not written, {why}: {shown_names(names)}")

    def _table(self, name: str) -> DumpObject | None:
        statements: tuple[str, ...] = ()
        if self.ddl:
            row = self.show("table", name)
            if row is None:
                return None
            statements = (row[1],)
        return DumpObject("table", name, statements, f"DROP TABLE IF EXISTS {quote_ident(Dialect.MYSQL, name)}")

    def _data(self, row: tuple, columns: list[TableColumn], references: tuple[str, ...]) -> DumpTable:
        name, _, estimate, size, _, _ = row
        return DumpTable(schema=self.database, table=name, name=name, columns=tuple(columns),
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

    def _writable(self, kind: str, name: str, sql_mode: object) -> bool:
        """Whether a program can be written at all: not made under NO_BACKSLASH_ESCAPES (T3)."""
        if NO_BACKSLASH_ESCAPES in _modes(sql_mode):
            self.no_backslash.append(f"{kind} {name}")
            return False
        return True

    def _routines(self, called: set[str]) -> list[DumpObject]:
        """Every routine, or (a `tables` scope, routines not chosen) the functions the views call (`called`,
        folded). A routine no file can carry is left out, and so is what uses it (`cannot`)."""
        routines = self.rows(_ROUTINES, self.database)
        for name, _, unreadable, sql_mode in routines:
            if unreadable or NO_BACKSLASH_ESCAPES in _modes(sql_mode):
                self.cannot.add(name.lower())
        if self.kinds.routines and not self.named:
            chosen = routines
        else:
            chosen = [row for row in routines if row[1] == "FUNCTION" and row[0].lower() in called]
            if not self.named:
                self.leave_out("functions and procedures (not chosen)", len(routines) - len(chosen))
        found = []
        for name, kind, unreadable, sql_mode in chosen:
            if unreadable:
                self.leave_out(f"{kind.lower()}s whose definition cannot be read")
            elif not self._writable(kind.lower(), name, sql_mode):
                continue
            elif (obj := self._program(kind.lower(), name)) is not None:
                found.append(obj)
            else:
                self.cannot.add(name.lower())  # SHOW CREATE refused it after all (counted there)
        return found

    def _calls_what_is_left_out(self, kind: str, name: str, body: str | None) -> bool:
        """A trigger or an event that calls a routine the dump leaves out (it would fail each time it runs)."""
        if not {routine.lower() for routine in routines_called(body or "", Dialect.MYSQL)} & self.cannot:
            return False
        self.needing.append(f"{kind} {name}")
        return True

    def _triggers(self, tables: set[str]) -> list[DumpObject]:
        triggers = [row for row in self.rows(_TRIGGERS, self.database) if row[1] in tables]
        if not self.kinds.triggers:
            self.leave_out("triggers (not chosen)", len(triggers))
            return []
        return [obj for name, _, sql_mode, body in triggers
                if self._writable("trigger", name, sql_mode) and not self._calls_what_is_left_out("trigger", name, body)
                and (obj := self._program("trigger", name)) is not None]

    def _events(self) -> list[DumpObject]:
        if self.named:
            return []
        events = self.rows(_EVENTS, self.database)
        if not self.kinds.routines:
            self.leave_out("events (not chosen)", len(events))
            return []
        return [obj for name, sql_mode, body in events
                if self._writable("event", name, sql_mode) and not self._calls_what_is_left_out("event", name, body)
                and (obj := self._program("event", name)) is not None]

    def _program(self, kind: str, name: str) -> DumpObject | None:
        show = _SHOW[kind]
        statements: tuple[str, ...] = ()
        if self.ddl:
            row = self.show(kind, name)
            if row is None:
                return None
            ddl = strip_definer(row[show.ddl])
            zone = None if show.time_zone is None else str(row[show.time_zone] or "SYSTEM")
            statements = (own_settings(ddl, str(row[show.client]), str(row[show.collation]),
                                       _mode_text(row[show.sql_mode]), zone),
                          ddl, restored_settings(sql_mode=True, time_zone=zone is not None))
        return DumpObject(kind, name, statements, f"DROP {show.word} IF EXISTS {quote_ident(Dialect.MYSQL, name)}",
                          compound=True)

    def _views(self, views: list[tuple], uses_routines: dict[str, set[str]]) -> list[DumpObject]:
        """In dependency order; an invalid view (T5), and a view that uses a routine or a view the dump leaves out
        (T4), are left out."""
        ordering = Ordering()
        for view, uses in self.optional_rows(_VIEW_USES, self.database, self.database) if views else []:
            ordering.depends.setdefault(view, set()).add(uses)
        comments = {row[0]: row[5] for row in views}
        left: set[str] = set()  # views left out, for the views built on them
        found = []
        for name in ordering.order(list(comments)):
            if comments[name] != _VALID_VIEW:
                self.invalid.append(f"view {name}")
                left.add(name)
                continue
            if uses_routines.get(name, set()) & self.cannot or ordering.depends.get(name, set()) & left:
                self.needing.append(f"view {name}")
                left.add(name)
                continue
            statements: tuple[str, ...] = ()
            if self.ddl:
                row = self.show("view", name)
                if row is None:
                    left.add(name)
                    continue
                ddl = strip_definer(row[_SHOW["view"].ddl])
                statements = (own_settings(ddl, str(row[_SHOW["view"].client]), str(row[_SHOW["view"].collation])),
                              ddl, restored_settings())
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
    found = catalog.first_row(conn, _SCHEMA, (name,))
    if found is None:
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
    charset, collation, utf8mb4 = found
    if collation != utf8mb4 and any(item.compound for item in post):  # T6
        warnings.append(
            f"This database's default is {charset} ({collation}), not {RESTORE_CHARSET} ({utf8mb4}): its stored "
            "programs' parameters, return values and variables without a character set of their own take the default "
            f"of the database the file is run in, so run it in one created with CHARACTER SET {charset} COLLATE "
            f"{collation}")
    if reader.outside:  # pre-flight M6
        consistency = "per_table"
        snapshot = (f"one consistent snapshot of the InnoDB tables; {len(reader.outside):,} other tables read as the "
                    "dump reached them")
        warnings.insert(0, f"Not in the snapshot (not InnoDB): {shown_names(reader.outside)}")
    return DumpCatalog(server=server, scope=f"database {name}", consistency=consistency, pre=tuple(pre),
                       tables=tuple(tables), post=tuple(post), not_included=tuple(left_out),
                       warnings=tuple(warnings), snapshot_text=snapshot)
