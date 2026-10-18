import pytest

from dbaguy_backend.sql.dialects import (
    Dialect, bool_literal, bytes_literal, datetime_literal, force_quote_ident, json_literal, needs_quote, qualified,
    quote_ident, quote_literal, zero_date_as_text,
)
from dbaguy_backend.sql.lexer import INSIGNIFICANT, TokenKind, tokenize
from dbaguy_backend.sql.offsets import Utf16Index
from dbaguy_backend.sql.sqlglot_rules import parse

PG, LITE, MY, TS = Dialect.POSTGRES, Dialect.SQLITE, Dialect.MYSQL, Dialect.TSQL


@pytest.mark.parametrize(
    ("dialect", "name", "expected"),
    [
        (PG, "users", "users"),
        (PG, "Users", '"Users"'),
        (PG, "order", '"order"'),
        (PG, "Order Items", '"Order Items"'),
        (PG, 'a"b', '"a""b"'),
        (LITE, "Users", "Users"),  # SQLite identifiers are case-insensitive: no quotes needed
        (LITE, "order", '"order"'),
        (LITE, "my table", '"my table"'),
        (MY, "users", "users"),
        (MY, "Users", "Users"),  # MySQL keeps the case of an unquoted name
        (MY, "order", "`order`"),
        (MY, "Order Items", "`Order Items`"),
        (MY, "a`b", "`a``b`"),
        (MY, "a$b", "a$b"),
        (MY, "1abc", "`1abc`"),
        (MY, "中文", "`中文`"),
        (MY, "delimiter", "delimiter"),
        (TS, "users", "users"),
        (TS, "Users", "Users"),
        (TS, "order", "[order]"),
        (TS, "select", "[select]"),
        (TS, "Order Items", "[Order Items]"),
        (TS, "a]b", "[a]]b]"),
        (TS, "a$b", "[a$b]"),
        (TS, "1abc", "[1abc]"),
        (TS, "中文", "[中文]"),
        (TS, "go", "[go]"),  # not reserved by the server, but alone on a line it is the batch separator
        (TS, "GO", "[GO]"),
    ],
)
def test_quote_ident(dialect, name, expected):
    assert quote_ident(dialect, name) == expected
    assert needs_quote(dialect, name) == (expected != name)


def test_qualified_and_literal():
    assert qualified(PG, "sales", "orders") == "sales.orders"
    assert qualified(PG, None, "orders") == "orders"
    assert qualified(LITE, "main", "my table") == 'main."my table"'
    assert quote_literal(PG, "it's") == "'it''s'"


def test_literals_per_dialect():
    assert quote_literal(PG, "it's \\ ok") == "'it''s \\ ok'"
    assert quote_literal(LITE, "a\\b") == "'a\\b'"
    assert quote_literal(MY, "it's \\ ok") == "'it''s \\\\ ok'"  # MySQL reads \ as an escape: doubled
    assert qualified(MY, "dbaguy", "Order Items") == "dbaguy.`Order Items`"
    assert force_quote_ident(MY, "x") == "`x`" and force_quote_ident(LITE, 'a"b') == '"a""b"'
    assert [bool_literal(d, True) for d in (PG, LITE, MY)] == ["TRUE", "1", "TRUE"]
    assert [bool_literal(d, False) for d in (PG, LITE, MY)] == ["FALSE", "0", "FALSE"]
    assert [bytes_literal(d, "dead") for d in (PG, LITE, MY)] == ["'\\xdead'::bytea", "X'dead'", "X'dead'"]
    assert [json_literal(d, '{"a": 1}') for d in (PG, LITE, MY)] == [
        """'{"a": 1}'""", """'{"a": 1}'""", """CAST('{"a": 1}' AS JSON)""",
    ]


def test_tsql_literals():
    assert quote_literal(TS, "it's 中文") == "N'it''s 中文'"
    assert quote_literal(TS, "a\\b") == "N'a\\b'"  # no backslash escapes
    assert [bool_literal(TS, True), bool_literal(TS, False)] == ["1", "0"]
    assert bytes_literal(TS, "dead") == "0xdead"
    assert qualified(TS, "dbo", "Order Items") == "dbo.[Order Items]"
    assert force_quote_ident(TS, "x") == "[x]"
    assert json_literal(TS, '{"a": 1}') == "N'{\"a\": 1}'"
    assert zero_date_as_text(TS, "d", "0000-00-00") is None


def test_tsql_literal_never_carries_a_raw_nul():
    # pymssql sends the SQL as a C string: a raw NUL cuts it off (error 105), and every later row of an export is lost
    literal = quote_literal(TS, "a\x00b'c\x00")
    assert "\x00" not in literal
    nul = "CAST(NCHAR(0) AS NVARCHAR(MAX))"  # MAX: a plain NCHAR(0) would cap the concatenation at 4000 characters
    assert literal == f"N'a' + {nul} + N'b''c' + {nul} + N''"
    tokens = [t for t in tokenize(literal, TS) if t.kind not in INSIGNIFICANT]
    assert all(t.terminated for t in tokens)
    assert [t.text for t in tokens if t.kind == TokenKind.STRING] == ["N'a'", "N'b''c'", "N''"]  # no string leaks out
    assert "\x00" not in json_literal(TS, '{"a": "x\x00"}')
    [statement] = parse(f"SELECT {literal}", TS)  # and it is one valid expression, not a Command fallback
    assert type(statement).__name__ == "Select"


@pytest.mark.parametrize("dialect, expected, strings", [
    (MY, "'a\\0b''c\\\\'", 1),  # the \0 escape of MySQL's default sql_mode; a real backslash stays doubled
    (LITE, "'a' || char(0) || 'b''c\\'", 2),
])
def test_no_literal_carries_a_raw_nul(dialect, expected, strings):  # the worker refuses a raw NUL for every engine
    """PostgreSQL has no entry: its text cannot hold a NUL at all."""
    literal = quote_literal(dialect, "a\x00b'c\\")
    assert literal == expected and "\x00" not in literal
    tokens = [t for t in tokenize(literal, dialect) if t.kind not in INSIGNIFICANT]
    assert all(t.terminated for t in tokens) and len([t for t in tokens if t.kind == TokenKind.STRING]) == strings


def test_tsql_datetime_literals_are_language_neutral():  # R-03b-P10
    assert datetime_literal(TS, "2026-10-18 12:34:56.123") == "N'2026-10-18T12:34:56.123'"
    assert datetime_literal(TS, "2026-10-18 12:34:56.123456+08:00") == "N'2026-10-18T12:34:56.123456+08:00'"
    assert datetime_literal(TS, "2026-10-18") == "N'2026-10-18'"  # a date alone is neutral for date columns
    assert datetime_literal(TS, "12:34:56") == "N'12:34:56'"
    assert datetime_literal(PG, "2026-10-18 12:34:56") == "'2026-10-18 12:34:56'"
    assert datetime_literal(MY, "2026-10-18 12:34:56") == "'2026-10-18 12:34:56'"


@pytest.mark.parametrize("dialect", list(Dialect))
def test_every_dialect_has_an_entry_in_every_dialect_table(dialect):  # tripwire for 03b's T-SQL
    from dbaguy_backend.sql import classifier, completion, dialects, sqlglot_rules

    tables = {
        "dialects.RULES": dialects.RULES,
        "dialects._IDENT_QUOTES": dialects._IDENT_QUOTES,
        "dialects._SIMPLE_IDENT": dialects._SIMPLE_IDENT,
        "dialects._RESERVED": dialects._RESERVED,
        "dialects._BOOL_LITERALS": dialects._BOOL_LITERALS,
        "dialects._BYTES_LITERALS": dialects._BYTES_LITERALS,
        "dialects._JSON_LITERALS": dialects._JSON_LITERALS,
        "dialects._ZERO_DATE_AS_TEXT": dialects._ZERO_DATE_AS_TEXT,
        "dialects._STRING_PREFIXES": dialects._STRING_PREFIXES,
        "dialects._ISO_DATETIME": dialects._ISO_DATETIME,
        "dialects._NUL_IN_LITERAL": dialects._NUL_IN_LITERAL,
        "classifier._EXPLAIN_KEYWORDS": classifier._EXPLAIN_KEYWORDS,
        "sqlglot_rules.SQLGLOT_DIALECT": sqlglot_rules.SQLGLOT_DIALECT,
        "completion.STATEMENT_KEYWORDS": completion.STATEMENT_KEYWORDS,
        "completion.CLAUSE_KEYWORDS": completion.CLAUSE_KEYWORDS,
    }
    missing = [name for name, table in tables.items() if dialect not in table]
    assert not missing, f"{dialect} has no entry in: {missing}"


def test_utf16_round_trip_with_astral_characters():
    text = "a😀b中😀c"  # 😀 is outside the BMP (2 UTF-16 units)
    index = Utf16Index(text)
    assert [index.to_utf16(i) for i in range(len(text) + 1)] == [0, 1, 3, 4, 5, 7, 8]
    for cp in range(len(text) + 1):
        assert index.from_utf16(index.to_utf16(cp)) == cp
    assert index.from_utf16(2) == 1  # middle of a surrogate pair clamps to the character start
    assert index.from_utf16(99) == len(text)
