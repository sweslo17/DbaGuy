import re

from ..sql.dialects import PG_RESERVED, Dialect, qualified, quote_ident, quote_literal  # noqa: F401  (re-exported)

_LEADING_JUNK = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)+", re.DOTALL)
_FIRST_WORD = re.compile(r"[A-Za-z_]+")


def quote_ident_pg(name: str) -> str:
    """Quote a PostgreSQL identifier only when needed."""
    return quote_ident(Dialect.POSTGRES, name)


def qualified_pg(schema: str, name: str) -> str:
    return qualified(Dialect.POSTGRES, schema, name)


def quote_ident_sqlite(name: str) -> str:
    """Always quote: introspection SQL interpolates arbitrary names."""
    return '"' + name.replace('"', '""') + '"'


def strip_leading_comments(sql: str) -> str:
    return _LEADING_JUNK.sub("", sql, count=1)


def first_keyword(sql: str) -> str:
    """Lower-cased first keyword of a statement ("(" for a parenthesised query, "" if empty)."""
    rest = strip_leading_comments(sql)
    if rest.startswith("("):
        return "("
    match = _FIRST_WORD.match(rest)
    return match.group(0).lower() if match else ""
