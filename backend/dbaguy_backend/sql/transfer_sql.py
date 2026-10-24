"""The SQL a copy sends besides its rows (spec ⑥ §3.5.4–3.5.5): a new table, the DELETE of the rows it replaces, an
identity's switch and its sequence after, its sessions' settings, and the History record's text. Quoting and literals
come from `dialects` only. Pure."""

from collections.abc import Sequence

from .dialects import Dialect, identity_insert, qualified, quote_ident, quote_literal, script_text
from .dump_script import RESET_SETTINGS, RESTORE_SETTINGS

# The copy's own target session, before its first table: what a restore of a dump runs under (DRY, D-06-7) — PostgreSQL
# standard strings, MySQL's strict sql_mode that keeps zero dates and AUTO_INCREMENT 0 with time_zone +00:00 and its
# foreign key checks off, SQLite's foreign keys off (each table commits on its own, so deferring them to a COMMIT would
# not help a later table, 06c) — and SQL Server's implicit transactions off (06a L8: a login's user options)
TARGET_SETUP: dict[Dialect, tuple[str, ...]] = {
    **RESTORE_SETTINGS,
    Dialect.TSQL: ("SET IMPLICIT_TRANSACTIONS OFF", *RESTORE_SETTINGS[Dialect.TSQL]),
}
TARGET_RESET = RESET_SETTINGS  # after its last table (the session is never used again; a courtesy)
# A source's own session settings for the copy's SELECT, read before and set back after (a query tab's session is the
# user's): PostgreSQL's text of dates and intervals as every engine reads them; MySQL's TIMESTAMP in UTC, as the target
# writes it, and a reader that may wait on a slow target longer than net_write_timeout (60 s by default) without the
# server cutting the read (A11: probed, 2013 after 66,001 of 1,000,000 rows with 2 s and an 8 s wait; all of them with
# 3,600 s)
SOURCE_SETTINGS: dict[Dialect, tuple[tuple[str, str], ...]] = {
    Dialect.POSTGRES: (("DateStyle", "ISO, MDY"), ("IntervalStyle", "postgres")),
    Dialect.MYSQL: (("net_write_timeout", "3600"), ("time_zone", "+00:00")),
}


def read_settings(dialect: Dialect) -> str | None:
    """The SELECT of a source session's settings `SOURCE_SETTINGS` names, in their order; None: it has none."""
    names = [name for name, _ in SOURCE_SETTINGS.get(dialect, ())]
    if not names:
        return None
    if dialect is Dialect.POSTGRES:
        return "SELECT " + ", ".join(f"current_setting({quote_literal(dialect, name)})" for name in names)
    return "SELECT " + ", ".join(f"@@SESSION.{name}" for name in names)


def set_setting(dialect: Dialect, name: str, value: str) -> str:
    """`SET` one setting `SOURCE_SETTINGS` names to `value` (a number goes bare in MySQL)."""
    if dialect is Dialect.POSTGRES:
        return f"SET {name} = {quote_literal(dialect, value)}"
    shown = value if value.isdigit() else quote_literal(dialect, value)
    return f"SET SESSION {name} = {shown}"


def create_table(
    dialect: Dialect, schema: str | None, name: str, columns: Sequence[tuple[str, str, bool]], key: Sequence[str],
) -> str:
    """A copy's new table (spec §3.5.4): its columns (name, type, nullable) with NOT NULL, and the source's primary key
    when every column of it is copied. Nothing else (defaults, checks, indexes, foreign keys and identities are each
    engine's own: not translated, R-06-9)."""
    lines = [f"  {quote_ident(dialect, column)} {type_sql}".rstrip() + ("" if nullable else " NOT NULL")
             for column, type_sql, nullable in columns]  # SQLite's untyped column: no type at all
    if key:
        lines.append(f"  PRIMARY KEY ({', '.join(quote_ident(dialect, column) for column in key)})")
    return f"CREATE TABLE {qualified(dialect, schema, name)} (\n" + ",\n".join(lines) + "\n)"


def delete_rows(dialect: Dialect, schema: str | None, name: str) -> str:
    """Replace rows (spec §3.5.4): every row goes, in the copy's transaction (DELETE_WITHOUT_WHERE, said in danger)."""
    return f"DELETE FROM {qualified(dialect, schema, name)}"


def identity_switch(dialect: Dialect, schema: str | None, name: str) -> tuple[str, str] | None:
    """SQL Server's IDENTITY_INSERT on and off around a table whose identity values are kept (one table at a time)."""
    return identity_insert(dialect, qualified(dialect, schema, name))


def advance_sequence(schema: str | None, name: str, column: str) -> str:
    """PostgreSQL, after writing a column's own values: its sequence (an identity's or a serial's) moves past the
    largest value, never back (writing them does not move it, A P2). A column that owns no sequence changes nothing;
    neither does an empty table."""
    table = quote_literal(Dialect.POSTGRES, qualified(Dialect.POSTGRES, schema, name))
    quoted = quote_ident(Dialect.POSTGRES, column)
    return ("SELECT setval(s.seq, GREATEST(s.top, COALESCE(pg_sequence_last_value(s.seq), s.top)))\n"
            f"FROM (SELECT pg_get_serial_sequence({table}, {quote_literal(Dialect.POSTGRES, column)})::regclass AS seq,\n"
            f"             (SELECT max({quoted}) FROM {qualified(Dialect.POSTGRES, schema, name)}) AS top) s\n"
            "WHERE s.seq IS NOT NULL AND s.top IS NOT NULL")


def summary(source: str, target: str, tables: Sequence[tuple[str, str, str]], dialect: Dialect,
            statements: Sequence[str]) -> str:
    """The History record's text (D-06-9): what goes where, then the statements the copy adds (DDL, DELETE), never its
    INSERTs: `-- Copy data: Local PG public.orders → Dev MySQL orders (create)`."""
    if len(tables) == 1:
        source_name, target_name, mode = tables[0]
        head = f"-- Copy data: {source} {source_name} → {target} {target_name} ({mode})"
    else:
        head = f"-- Copy data: {source} → {target}, {len(tables):,} tables"
        head += "".join(f"\n--   {source_name} → {target_name} ({mode})" for source_name, target_name, mode in tables)
    return head + ("\n" + script_text(dialect, statements) if statements else "")
