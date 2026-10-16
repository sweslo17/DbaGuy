import re
from dataclasses import dataclass

from ..domain.dialect import Dialect  # the key of every rule table in sql/; imported from here by the sql modules


@dataclass(frozen=True)
class LexRules:
    nested_block_comments: bool
    line_comment_end: re.Pattern[str]  # ends a `--` comment: LF or CR (PostgreSQL), LF only (SQLite)
    dollar_quotes: bool
    escape_string_prefixes: frozenset[str]  # e.g. E'...' in PostgreSQL
    identifier_quotes: tuple[tuple[str, str], ...]  # (open, close)
    block_bodies: frozenset[str]  # "begin_atomic" (PG SQL-standard bodies), "trigger" (SQLite trigger bodies)


RULES: dict[Dialect, LexRules] = {
    Dialect.POSTGRES: LexRules(
        nested_block_comments=True, line_comment_end=re.compile(r"[\n\r]"), dollar_quotes=True,
        escape_string_prefixes=frozenset({"e", "E"}), identifier_quotes=(('"', '"'),),
        block_bodies=frozenset({"begin_atomic"}),
    ),
    Dialect.SQLITE: LexRules(
        nested_block_comments=False, line_comment_end=re.compile(r"\n"), dollar_quotes=False,
        escape_string_prefixes=frozenset(), identifier_quotes=(('"', '"'), ("[", "]"), ("`", "`")),
        block_bodies=frozenset({"trigger"}),
    ),
}

PG_RESERVED = frozenset(
    """all analyse analyze and any array as asc asymmetric authorization binary both case cast check
    collate collation column concurrently constraint create cross current_catalog current_date current_role
    current_schema current_time current_timestamp current_user default deferrable desc distinct do else end
    except false fetch for foreign freeze from full grant group having ilike in initially inner intersect into
    is isnull join lateral leading left like limit localtime localtimestamp natural not notnull null offset on
    only or order outer overlaps placing primary references returning right select session_user similar some
    symmetric system_user table tablesample then to trailing true union unique user using variadic verbose
    when where window with""".split()
)

SQLITE_RESERVED = frozenset(
    """abort action add after all alter always analyze and as asc attach autoincrement before begin between by
    cascade case cast check collate column commit conflict constraint create cross current current_date
    current_time current_timestamp database default deferrable deferred delete desc detach distinct do drop each
    else end escape except exclude exclusive exists explain fail filter first following for foreign from full
    generated glob group groups having if ignore immediate in index indexed initially inner insert instead
    intersect into is isnull join key last left like limit match materialized natural no not nothing notnull
    null nulls of offset on or order others outer over partition plan pragma preceding primary query raise range
    recursive references regexp reindex release rename replace restrict returning right rollback row rows
    savepoint select set table temp temporary then ties to transaction trigger unbounded union unique update
    using vacuum values view virtual when where window with without""".split()
)

_SIMPLE_IDENT = {
    Dialect.POSTGRES: re.compile(r"[a-z_][a-z0-9_$]*"),  # unquoted PG identifiers fold to lower case
    Dialect.SQLITE: re.compile(r"[A-Za-z_][A-Za-z0-9_]*"),  # SQLite identifiers are case-insensitive
}
_RESERVED = {Dialect.POSTGRES: PG_RESERVED, Dialect.SQLITE: SQLITE_RESERVED}


def needs_quote(dialect: Dialect, name: str) -> bool:
    return not _SIMPLE_IDENT[dialect].fullmatch(name) or name.lower() in _RESERVED[dialect]


def quote_ident(dialect: Dialect, name: str) -> str:
    if not needs_quote(dialect, name):
        return name
    return '"' + name.replace('"', '""') + '"'


def qualified(dialect: Dialect, schema: str | None, name: str) -> str:
    return f"{quote_ident(dialect, schema)}.{quote_ident(dialect, name)}" if schema else quote_ident(dialect, name)


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
