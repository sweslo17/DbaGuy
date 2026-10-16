from ..sql.dialects import PG_RESERVED, Dialect, qualified, quote_ident, quote_literal  # noqa: F401  (re-exported)
from ..sql.lexer import TokenKind, first_token


def quote_ident_pg(name: str) -> str:
    """Quote a PostgreSQL identifier only when needed."""
    return quote_ident(Dialect.POSTGRES, name)


def qualified_pg(schema: str, name: str) -> str:
    return qualified(Dialect.POSTGRES, schema, name)


def quote_ident_sqlite(name: str) -> str:
    """Always quote: introspection SQL interpolates arbitrary names."""
    return '"' + name.replace('"', '""') + '"'


def first_keyword(sql: str) -> str:
    """Lower-cased first keyword of a PostgreSQL statement ("(" for a parenthesised query, "" otherwise)."""
    token = first_token(sql, Dialect.POSTGRES)
    if token is None:
        return ""
    if token.kind == TokenKind.LPAREN:
        return "("
    return token.text.lower() if token.kind == TokenKind.WORD else ""
