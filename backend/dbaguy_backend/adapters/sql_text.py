from ..sql.dialects import Dialect, force_quote_ident, qualified, quote_ident, quote_literal
from ..sql.lexer import TokenKind, first_token


def quote_ident_pg(name: str) -> str:
    """Quote a PostgreSQL identifier only when needed."""
    return quote_ident(Dialect.POSTGRES, name)


def qualified_pg(schema: str, name: str) -> str:
    return qualified(Dialect.POSTGRES, schema, name)


def quote_ident_sqlite(name: str) -> str:
    """Always quote: introspection SQL interpolates arbitrary names."""
    return force_quote_ident(Dialect.SQLITE, name)


def quote_ident_mysql(name: str) -> str:
    """Always backtick-quote: introspection SQL interpolates arbitrary names."""
    return force_quote_ident(Dialect.MYSQL, name)


def quote_literal_pg(value: str) -> str:
    """A PostgreSQL string literal (quotes doubled; the server reads backslashes literally)."""
    return quote_literal(Dialect.POSTGRES, value)


def first_keyword(sql: str) -> str:
    """Lower-cased first keyword of a PostgreSQL statement ("(" for a parenthesised query, "" otherwise)."""
    token = first_token(sql, Dialect.POSTGRES)
    if token is None:
        return ""
    if token.kind == TokenKind.LPAREN:
        return "("
    return token.text.lower() if token.kind == TokenKind.WORD else ""
