import re

import pytest

from dbaguy_backend.sql.dialects import (
    Dialect, bool_literal, bytes_literal, datetime_literal, force_quote_ident, in_list, is_reserved, json_literal,
    keyspace_path, needs_quote, qualified, quote_ident, quote_literal, zero_date_as_text,
)
from dbaguy_backend.sql.lexer import INSIGNIFICANT, TokenKind, tokenize
from dbaguy_backend.sql.offsets import Utf16Index, utf16_offset
from dbaguy_backend.sql.sqlglot_rules import parse

PG, LITE, MY, TS, SQ = Dialect.POSTGRES, Dialect.SQLITE, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLPP


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
        (SQ, "users", "`users`"),  # USERS is reserved
        (SQ, "orders", "orders"),
        (SQ, "travel-sample", "`travel-sample`"),
        (SQ, "value", "`value`"),
        (SQ, "Name", "Name"),  # SQL++ names keep their case
        (SQ, "a$b", "`a$b`"),
        (SQ, "中文", "`中文`"),
        (SQ, "_index_key", "`_index_key`"),
        (SQ, "a`b", "`a``b`"),
        (SQ, "a\\b", "`a\\\\b`"),
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


LF, CR = "\n", "\r"
MAX_N, MAX_PLAIN = "CAST(N'' AS NVARCHAR(MAX))", "CAST('' AS VARCHAR(MAX))"


@pytest.mark.parametrize("text, expected", [  # the server drops a backslash and the line break after it (probed live)
    (f"a\\{LF}b", f"CONCAT({MAX_N}, N'a\\', N'{LF}b')"),
    (f"a\\{CR}{LF}b", f"CONCAT({MAX_N}, N'a\\', N'{CR}{LF}b')"),
    (f"a\\{CR}b", f"CONCAT({MAX_N}, N'a\\', N'{CR}b')"),  # a lone CR keeps itself and loses the backslash
    (f"a\\\\{LF}b", f"CONCAT({MAX_N}, N'a\\\\', N'{LF}b')"),  # only the backslash next to the break counts: one split
    (f"a\\{LF}b\\{CR}{LF}c", f"CONCAT({MAX_N}, N'a\\', N'{LF}b\\', N'{CR}{LF}c')"),
    (f"\\{LF}", f"CONCAT({MAX_N}, N'\\', N'{LF}')"),
    (f"it's\\{LF}", f"CONCAT({MAX_N}, N'it''s\\', N'{LF}')"),
    (f"a\\{LF}{LF}b", f"CONCAT({MAX_N}, N'a\\', N'{LF}{LF}b')"),  # the second break has no backslash before it
    (f"a\x00\\{LF}b", f"CONCAT({MAX_N}, N'a' + CAST(NCHAR(0) AS NVARCHAR(MAX)) + N'\\', N'{LF}b')"),
])
def test_tsql_backslash_before_a_line_break_ends_its_literal(text, expected):
    literal = quote_literal(TS, text)
    assert literal == expected
    tokens = [t for t in tokenize(literal, TS) if t.kind not in INSIGNIFICANT]
    assert all(t.terminated for t in tokens)  # no string runs on, and none begins right after a backslash and a break
    assert not any(t.kind == TokenKind.STRING and ("\\" + LF in t.text or "\\" + CR in t.text) for t in tokens)
    [statement] = parse(f"SELECT {literal}", TS)  # one valid expression, not a Command fallback
    assert type(statement).__name__ == "Select"


@pytest.mark.parametrize("text", [
    "a\\", f"a\\ {LF}b", f"a\\\t{LF}", f"a{LF}\\b", f"a{LF}b", f"{LF}\\", "C:\\tmp\\", "it's \\ ok", "",
])
def test_tsql_text_without_a_backslash_right_before_a_break_is_one_plain_literal(text):
    assert quote_literal(TS, text) == "N'" + text.replace("'", "''") + "'"  # nothing to guard against: nothing changes


def test_tsql_non_unicode_literal_keeps_its_type_across_the_split():
    # A8: `N'…'` turns a varchar key's index seek into a scan, so the empty value is VARCHAR(MAX) and no piece gets an N
    literal = quote_literal(TS, f"k\\{LF}1", national=False)
    assert literal == f"CONCAT({MAX_PLAIN}, 'k\\', '{LF}1')"
    assert "N'" not in literal and "NVARCHAR" not in literal
    assert quote_literal(TS, "k42", national=False) == "'k42'"
    assert json_literal(TS, f'"a\\{LF}"', national=False).startswith(f"CONCAT({MAX_PLAIN}, ")


def concat_calls(literal: str) -> list[int]:
    """The number of arguments of every CONCAT( … ) in a T-SQL expression, outermost first (read from its tokens)."""
    counts: list[int] = []
    open_calls: list[int | None] = []  # None: some other parenthesis (a CAST); else the index in `counts`
    previous = None
    for token in (t for t in tokenize(literal, TS) if t.kind not in INSIGNIFICANT):
        if token.kind == TokenKind.LPAREN:
            if previous is not None and previous.text.upper() == "CONCAT":
                counts.append(1)
                open_calls.append(len(counts) - 1)
            else:
                open_calls.append(None)
        elif token.kind == TokenKind.RPAREN:
            open_calls.pop()
        elif token.kind == TokenKind.COMMA and open_calls and open_calls[-1] is not None:
            counts[open_calls[-1]] += 1
        previous = token
    return counts


@pytest.mark.parametrize("breaks, calls", [
    (251, [253]), (252, [254]),  # one call takes the MAX value and 253 pieces: 252 breaks are 253 pieces
    (253, [3, 254, 2]),  # 254 pieces: 253 and 1, and a call over the two (each call has its own MAX value)
    (1000, [5, 254, 254, 254, 243]),  # 1001 pieces: three calls of 253 and one of 242
])
def test_tsql_concatenation_of_many_pieces_nests_inside_concats_argument_limit(breaks, calls):
    # a chain of `+` is as deep as it is long: the server stops it at 1,000-2,000 breaks (8631); CONCAT takes 254
    # arguments, so more pieces are nested, and every level starts with the MAX value so none is capped at 4000
    text = f"ab\\{LF}" * breaks
    literal = quote_literal(TS, text)
    found = concat_calls(literal)
    assert found == calls and max(found) <= 254
    assert literal.count(MAX_N) == len(found)
    pieces = re.findall(r"N'((?:[^']|'')*)'", literal)
    assert "".join(pieces) == text  # every piece, in order, once: nothing lost or doubled by the nesting


@pytest.mark.parametrize("dialect, text, expected", [
    (PG, f"a\\{LF}b", f"'a\\{LF}b'"),  # nobody else reads a backslash and a line break as a continuation
    (PG, f"a\\{CR}{LF}b", f"'a\\{CR}{LF}b'"),
    (LITE, f"a\\{LF}b", f"'a\\{LF}b'"),
    (LITE, f"a\\{CR}b", f"'a\\{CR}b'"),
    (MY, f"a\\{LF}b", f"'a\\\\{LF}b'"),  # MySQL doubles the backslash anyway
    (MY, f"a\\{CR}{LF}b", f"'a\\\\{CR}{LF}b'"),
    (SQ, f"a\\{LF}b", '"a\\\\\\nb"'),  # a JSON string
])
def test_no_other_dialect_changes_for_a_backslash_before_a_line_break(dialect, text, expected):
    assert quote_literal(dialect, text) == expected
    assert quote_literal(dialect, text, backslash_escapes=False if dialect is MY else None).count("CONCAT") == 0


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
    assert datetime_literal(TS, "2026-10-18") == "N'20261018'"  # datetime reads 2026-10-18 by DATEFORMAT too
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
        "dialects._LINE_CONTINUATION_GUARD": dialects._LINE_CONTINUATION_GUARD,
        "dialects._JSON_STRINGS": dialects._JSON_STRINGS,
        "dialects._IN_LIST_BRACKETS": dialects._IN_LIST_BRACKETS,
        "classifier._EXPLAIN_KEYWORDS": classifier._EXPLAIN_KEYWORDS,
        "sqlglot_rules.SQLGLOT_DIALECT": sqlglot_rules.SQLGLOT_DIALECT,
        "completion.STATEMENT_KEYWORDS": completion.STATEMENT_KEYWORDS,
        "completion.CLAUSE_KEYWORDS": completion.CLAUSE_KEYWORDS,
    }
    missing = [
        name for name, table in tables.items()
        if dialect not in table and not (name == "sqlglot_rules.SQLGLOT_DIALECT" and dialect in dialects.KEYWORD_ONLY_DIALECTS)
    ]
    assert not missing, f"{dialect} has no entry in: {missing}"


def test_utf16_round_trip_with_astral_characters():
    text = "a😀b中😀c"  # 😀 is outside the BMP (2 UTF-16 units)
    index = Utf16Index(text)
    assert [index.to_utf16(i) for i in range(len(text) + 1)] == [0, 1, 3, 4, 5, 7, 8]
    for cp in range(len(text) + 1):
        assert index.from_utf16(index.to_utf16(cp)) == cp
    assert index.from_utf16(2) == 1  # middle of a surrogate pair clamps to the character start
    assert index.from_utf16(99) == len(text)


def test_sqlpp_literals_and_paths():
    assert quote_literal(SQ, "it's \"中文\"\n\\") == '"it\'s \\"中文\\"\\n\\\\"'  # a JSON string (D-04-15)
    assert quote_literal(SQ, "\x00") == '"\\u0000"'
    assert [bool_literal(SQ, True), bool_literal(SQ, False)] == ["TRUE", "FALSE"]
    assert json_literal(SQ, '{"a": [1, 1e400]}') == '{"a": [1, 1e400]}'  # JSON is SQL++ syntax
    with pytest.raises(ValueError):
        bytes_literal(SQ, "dead")
    assert force_quote_ident(SQ, "a\\b`c") == "`a\\\\b``c`"
    assert keyspace_path("travel-sample", "inventory", "airline") == "`travel-sample`.inventory.airline"
    assert keyspace_path("dbaguy", "app") == "dbaguy.app"
    assert keyspace_path("my.bucket") == "`my.bucket`"
    assert in_list(SQ, ["1", '"a"']) == '[1, "a"]'
    assert in_list(Dialect.POSTGRES, ["1", "'a'"]) == "(1, 'a')"
    assert is_reserved(SQ, "Users") and not is_reserved(SQ, "name")


def test_utf16_offset_counts_astral_characters():
    text = 'a😀b中\U0001F600'
    assert [utf16_offset(text, cp) for cp in range(-1, 7)] == [0, 0, 1, 3, 4, 5, 7, 7]
