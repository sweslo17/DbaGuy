import pytest

from dbaguy_backend.sql.dialects import Dialect, needs_quote, qualified, quote_ident, quote_literal
from dbaguy_backend.sql.offsets import Utf16Index

PG, LITE = Dialect.POSTGRES, Dialect.SQLITE


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
    ],
)
def test_quote_ident(dialect, name, expected):
    assert quote_ident(dialect, name) == expected
    assert needs_quote(dialect, name) == (expected != name)


def test_qualified_and_literal():
    assert qualified(PG, "sales", "orders") == "sales.orders"
    assert qualified(PG, None, "orders") == "orders"
    assert qualified(LITE, "main", "my table") == 'main."my table"'
    assert quote_literal("it's") == "'it''s'"


def test_utf16_round_trip_with_astral_characters():
    text = "a😀b中😀c"  # 😀 is outside the BMP (2 UTF-16 units)
    index = Utf16Index(text)
    assert [index.to_utf16(i) for i in range(len(text) + 1)] == [0, 1, 3, 4, 5, 7, 8]
    for cp in range(len(text) + 1):
        assert index.from_utf16(index.to_utf16(cp)) == cp
    assert index.from_utf16(2) == 1  # middle of a surrogate pair clamps to the character start
    assert index.from_utf16(99) == len(text)
