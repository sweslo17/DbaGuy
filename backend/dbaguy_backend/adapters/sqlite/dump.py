"""What a SQLite dump writes, and in which order (spec ⑥ §3.3.3): sqlite_master's own texts in its order (creation
order), read in one read transaction (WAL: other connections keep writing; a rollback journal: their writes wait until
the dump ends, A5). Before the data: tables (foreign keys inside: the header turns the checks off, and defers them
inside a transaction). After the data: AUTOINCREMENT counters (sqlite_sequence follows the ids written, but not a
counter past them), indexes, views (SQLite does not check a view's tables when it is created, L1), triggers on tables
and on views (pre-flight H6), and a whole file's user_version and application_id (an application's own record of its
schema). A virtual table, its internal tables and SQLite's own tables are left out and counted (⑤ G4: writing them
breaks their index)."""

import sqlite3

from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ...domain.errors import NotFound, ValidationFailed
from ...sql.dialects import Dialect, quote_ident, quote_literal
from ...sql.scope import names_used
from ..dump import (
    OWNERS_AND_PRIVILEGES, DumpCatalog, DumpObject, DumpSession, DumpTable, kept_foreign_key, shown_names,
)
from .columns import columns_of, read_relation

SESSION = DumpSession(setup=("BEGIN",), teardown=("ROLLBACK",), consistency="snapshot")
SNAPSHOT_TEXT = "one consistent snapshot (a read transaction)"
JOURNAL_WARNING = ("While the dump reads, other connections cannot write to this file (it uses a rollback journal, "
                   "not WAL)")  # A5: their writes wait for it (database is locked); under WAL they go through
_MASTER = "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid"
_KINDS = "SELECT name, type FROM pragma_table_list WHERE schema = 'main'"  # table, view, virtual, shadow
_SEQUENCES = "SELECT name, seq FROM sqlite_sequence"
_HAS_SEQUENCES = "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'"
_REFERENCES = 'SELECT DISTINCT "table" FROM pragma_foreign_key_list(?, ?)'
_SETTINGS = ("user_version", "application_id")  # the database header's own numbers: a whole file's dump keeps them
_STATISTICS = "sqlite_stat"  # sqlite_stat1, sqlite_stat4: ANALYZE's
_OWN = "sqlite_"  # SQLite's own tables (sqlite_sequence is written as counters)


def _q(name: str) -> str:
    return quote_ident(Dialect.SQLITE, name)


def _folded(name: str) -> str:
    """SQLite matches names without case, ASCII letters only."""
    return name.encode().lower().decode()


class _Reader:
    def __init__(self, conn: sqlite3.Connection, scope: DumpScope, kinds: DumpObjectKinds, ddl: bool) -> None:
        self.conn, self.kinds, self.ddl = conn, kinds, ddl
        self.named = scope.kind == "tables"
        self.wanted = set(scope.names)
        self.left_out: dict[str, int] = {}
        self.warnings: list[str] = []

    def leave_out(self, what: str, count: int = 1) -> None:
        if count:
            self.left_out[what] = self.left_out.get(what, 0) + count

    def _ddl(self, *statements: str) -> tuple[str, ...]:
        return statements if self.ddl else ()

    def read(self) -> tuple[list[DumpObject], list[DumpTable], list[DumpObject]]:
        kind_of = dict(self.conn.execute(_KINDS).fetchall())
        master = self.conn.execute(_MASTER).fetchall()
        if self.named and (missing := self.wanted - {name for kind, name, _, _ in master if kind in ("table", "view")}):
            raise NotFound(f"Not found: {', '.join(sorted(missing))}")
        in_scope = [row for row in master if not self.named or row[0] not in ("table", "view") or row[1] in self.wanted]
        tables = [name for kind, name, _, _ in in_scope if kind == "table" and self._dumped_table(name, kind_of)]
        # What no dump writes (in any scope): what names it cannot work where the file is run (review T1)
        left = {_folded(name) for kind, name, _, _ in master
                if kind == "table" and (kind_of.get(name) in ("virtual", "shadow") or name.startswith(_STATISTICS))}
        all_views = {name: sql for kind, name, _, sql in in_scope if kind == "view"}
        views = list(all_views)
        if not (self.kinds.views or self.named):
            self.leave_out("views (not chosen)", len(views))
            views = []
        needing = self._needing(views, all_views, left)
        views = [name for name in views if name not in needing]
        written, on = set(tables), set(tables) | set(views)
        triggers = [row for row in in_scope if row[0] == "trigger" and (row[2] in on or row[2] in all_views)]
        if not self.kinds.triggers:
            self.leave_out("triggers (not chosen)", len(triggers))
            triggers = []
        kept = []
        for row in triggers:
            if row[2] not in on and row[2] not in needing:
                self.leave_out("triggers on views not chosen")
            elif {_folded(name) for name in names_used(row[3], Dialect.SQLITE)} & left:  # its ON view's name too
                needing[row[1]] = "trigger"
            else:
                kept.append(row)
        triggers = kept
        not_written = [f"{kind} {name}" for name, kind in needing.items()]
        self.leave_out("views and triggers that use a virtual table, SQLite's statistics or a view the dump leaves "
                       "out", len(not_written))
        if not_written:
            self.warnings.append("Not written, they use a virtual table (or its internal tables), SQLite's statistics "
                                 f"or a view the dump leaves out: {shown_names(not_written)}")
        pre = [DumpObject("table", name, self._ddl(sql), f"DROP TABLE IF EXISTS {_q(name)}")
               for kind, name, _, sql in in_scope if kind == "table" and name in written]
        post = [*self._counters(written),
                *(DumpObject("index", name, self._ddl(sql)) for kind, name, table, sql in in_scope
                  if kind == "index" and table in written),
                *(DumpObject("view", name, self._ddl(sql), f"DROP VIEW IF EXISTS {_q(name)}")
                  for kind, name, _, sql in in_scope if kind == "view" and name in views),
                *(DumpObject("trigger", name, self._ddl(sql)) for _, name, _, sql in triggers)]
        if not self.named:
            post += self._settings()
        dumped = {_folded(name): name for name in tables}
        return pre, [self._data(name, dumped) for name in tables], post

    @staticmethod
    def _needing(views: list[str], sql: dict[str, str], left: set[str]) -> dict[str, str]:
        """The views that use what no dump writes, or such a view (any order: SQLite does not check a view's names
        when it is made); `left` (folded names) grows with them."""
        needing: dict[str, str] = {}
        changed = True
        while changed:
            changed = False
            for name in views:
                if name not in needing and {_folded(used) for used in names_used(sql[name], Dialect.SQLITE)} & left:
                    needing[name] = "view"
                    left.add(_folded(name))
                    changed = True
        return needing

    def _dumped_table(self, name: str, kind_of: dict[str, str]) -> bool:
        """Whether a table of the scope is written; what is not is counted."""
        if name.startswith(_STATISTICS):
            self.leave_out("SQLite statistics tables (ANALYZE)")
        elif name.startswith(_OWN):
            pass  # sqlite_sequence: written as the counters
        elif kind_of.get(name) == "virtual":
            self.leave_out("virtual tables")
        elif kind_of.get(name) == "shadow":
            self.leave_out("internal tables of virtual tables")
        else:
            return True
        return False

    def _counters(self, tables: set[str]) -> list[DumpObject]:
        """AUTOINCREMENT counters, after the data: the ids written move sqlite_sequence up to the largest of them, a
        counter past it (rows deleted) comes back with `max`, and a table with no row gets its row."""
        if self.conn.execute(_HAS_SEQUENCES).fetchone() is None:
            return []
        found = []
        for name, seq in self.conn.execute(_SEQUENCES).fetchall():
            if name not in tables:
                continue
            if not isinstance(seq, int):  # sqlite_sequence is an ordinary table: anything can be written in it
                self.leave_out("AUTOINCREMENT counters that are not integers")
                continue
            literal = quote_literal(Dialect.SQLITE, name)
            found.append(DumpObject("sequence value", name, self._ddl(
                f"UPDATE sqlite_sequence SET seq = max(seq, {seq}) WHERE name = {literal}",
                f"INSERT INTO sqlite_sequence (name, seq) SELECT {literal}, {seq} "
                f"WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = {literal})",
            ), section="data"))
        return found

    def _settings(self) -> list[DumpObject]:
        found = []
        for setting in _SETTINGS:
            (value,) = self.conn.execute(f"PRAGMA {setting}").fetchone()
            if value:
                found.append(DumpObject("setting", setting, self._ddl(f"PRAGMA {setting} = {int(value)}")))
        return found

    def _data(self, name: str, dumped: dict[str, str]) -> DumpTable:
        """The table's columns, and the other tables of the dump (`dumped`, by folded name) its foreign keys reference
        (pre-flight M9); a foreign key to a table outside the dump stays in its DDL, and a warning says the file needs
        that table."""
        references = set()
        for (target,) in self.conn.execute(_REFERENCES, (name, "main")).fetchall():
            if (found := dumped.get(_folded(target))) is None:
                self.warnings.append(kept_foreign_key(name, target))
            elif found != name:
                references.add(found)
        return DumpTable(schema="main", table=name, name=name,
                         columns=tuple(columns_of(read_relation(self.conn, "main", name))),
                         references=tuple(sorted(references)))


def dump_catalog(
    conn: sqlite3.Connection, scope: DumpScope, kinds: DumpObjectKinds, *, ddl: bool = True,
) -> DumpCatalog:
    if scope.kind == "database" or (scope.schema_name or "main") != "main":
        raise ValidationFailed("A SQLite dump covers the main database")
    reader = _Reader(conn, scope, kinds, ddl)
    pre, tables, post = reader.read()
    not_included = [OWNERS_AND_PRIVILEGES, *(f"{count:,} {what}" for what, count in reader.left_out.items())]
    (journal,) = conn.execute("PRAGMA journal_mode").fetchone()
    warnings = ([] if str(journal).lower() == "wal" else [JOURNAL_WARNING]) + reader.warnings
    return DumpCatalog(server=f"SQLite {sqlite3.sqlite_version}", scope="schema main", consistency="snapshot",
                       pre=tuple(pre), tables=tuple(tables), post=tuple(post), not_included=tuple(not_included),
                       warnings=tuple(warnings), snapshot_text=SNAPSHOT_TEXT)
