import pytest

from dbaguy_backend.sql.dialects import (
    Dialect, bool_literal, bytes_literal, force_quote_ident, json_literal, needs_quote, qualified, quote_ident,
    quote_literal,
)
from dbaguy_backend.sql.offsets import Utf16Index

PG, LITE, MY = Dialect.POSTGRES, Dialect.SQLITE, Dialect.MYSQL


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


@pytest.mark.parametrize("dialect", list(Dialect))
def test_every_dialect_has_an_entry_in_every_dialect_table(dialect):  # tripwire for 03b's T-SQL
    from dbaguy_backend.sql import completion, dialects, sqlglot_rules

    tables = {
        "dialects.RULES": dialects.RULES,
        "dialects._IDENT_QUOTES": dialects._IDENT_QUOTES,
        "dialects._SIMPLE_IDENT": dialects._SIMPLE_IDENT,
        "dialects._RESERVED": dialects._RESERVED,
        "dialects._BOOL_LITERALS": dialects._BOOL_LITERALS,
        "dialects._BYTES_LITERALS": dialects._BYTES_LITERALS,
        "dialects._JSON_LITERALS": dialects._JSON_LITERALS,
        "dialects._ZERO_DATE_AS_TEXT": dialects._ZERO_DATE_AS_TEXT,
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
