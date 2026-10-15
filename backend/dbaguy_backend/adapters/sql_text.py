import re

_PG_SIMPLE_IDENT = re.compile(r"[a-z_][a-z0-9_$]*")
_LEADING_JUNK = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)+", re.DOTALL)
_FIRST_WORD = re.compile(r"[A-Za-z_]+")

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


def quote_ident_pg(name: str) -> str:
    """Quote a PostgreSQL identifier only when needed."""
    if _PG_SIMPLE_IDENT.fullmatch(name) and name not in PG_RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'


def qualified_pg(schema: str, name: str) -> str:
    return f"{quote_ident_pg(schema)}.{quote_ident_pg(name)}"


def quote_ident_sqlite(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def strip_leading_comments(sql: str) -> str:
    return _LEADING_JUNK.sub("", sql, count=1)


def first_keyword(sql: str) -> str:
    """Lower-cased first keyword of a statement ("(" for a parenthesised query, "" if empty)."""
    rest = strip_leading_comments(sql)
    if rest.startswith("("):
        return "("
    match = _FIRST_WORD.match(rest)
    return match.group(0).lower() if match else ""
