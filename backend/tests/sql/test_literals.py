"""Literals of grid edits (spec ⑤ §3.3.3, D-05-2, D-05-13): typed values, keys and original values per dialect × kind,
and the dialect's transaction frame, all-default row and script text."""

import pytest

from dbaguy_backend.domain.edits import MAX_VALUE_CHARS, CellInput, EditColumn
from dbaguy_backend.domain.errors import ValidationFailed
from dbaguy_backend.sql.dialects import (
    Dialect, TransactionFrame, datetime_literal, insert_defaults, insert_head, json_literal, quote_literal, script_text,
    transaction_frame, update_head,
)
from dbaguy_backend.sql.literals import (
    LiteralStyle, ValueProblem, cell_text, input_literal, match_condition, unmatched_reason,
)

PG, MY, TS, LITE = Dialect.POSTGRES, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLITE
PLAIN = LiteralStyle()
NO_BACKSLASH = LiteralStyle(backslash_escapes=False)
VARCHAR = LiteralStyle(national=False)
BYTES = {"$type": "bytes", "size": 2, "preview": "00ff"}
PARTIAL_BYTES = {"$type": "bytes", "size": 100, "preview": "00" * 64}
CUT_TEXT = {"$type": "text", "preview": "abc", "size": 70_000, "truncated": True}
JSON_NUL = {"$type": "json", "text": '"a\x00"'}


def col(kind="text", compare="equals", name="c", **fields) -> EditColumn:
    return EditColumn(name=name, type_name=kind, kind=kind, nullable=True, has_default=False, writable=True,
                      compare=compare, **fields)


def value(text: str) -> CellInput:
    # model_construct: a lone surrogate never passes the model (ruling E19); the builder still refuses it itself
    return CellInput.model_construct(kind="value", text=text)


@pytest.mark.parametrize(("dialect", "kind", "text", "style", "expected"), [
    (PG, "number", " 42 ", PLAIN, "42"),
    (PG, "number", "9007199254740993", PLAIN, "9007199254740993"),
    (MY, "number", "1.10", PLAIN, "1.10"),
    (TS, "number", "-1e3", PLAIN, "-1000"),  # an integer with an exponent would be a float literal: plain digits
    (PG, "number", "NaN", PLAIN, "'NaN'"),
    (PG, "number", "-infinity", PLAIN, "'-Infinity'"),
    (PG, "text", "it's", PLAIN, "'it''s'"),
    (MY, "text", "C:\\tmp\\", PLAIN, "'C:\\\\tmp\\\\'"),
    (MY, "text", "C:\\tmp\\", NO_BACKSLASH, "'C:\\tmp\\'"),
    (TS, "text", "50% done", PLAIN, "N'50% done'"),
    (TS, "text", "k42", VARCHAR, "'k42'"),
    (TS, "text", "café", VARCHAR, "N'café'"),
    (LITE, "text", "line 1\nline 2 😀", PLAIN, "'line 1\nline 2 😀'"),
    (PG, "bool", " Yes ", PLAIN, "TRUE"),
    (MY, "bool", "F", PLAIN, "FALSE"),
    (TS, "bool", "1", PLAIN, "1"),
    (LITE, "bool", "false", PLAIN, "0"),
    (PG, "binary", "0xABcd", PLAIN, "'\\xabcd'::bytea"),
    (MY, "binary", "00ff", PLAIN, "X'00ff'"),
    (TS, "binary", "0x", PLAIN, "0x"),
    (LITE, "binary", "", PLAIN, "X''"),
    (PG, "json", '{"a": [1, 2.50]}', PLAIN, "'{\"a\": [1, 2.50]}'"),
    (MY, "json", '{"a": 1}', PLAIN, "'{\"a\": 1}'"),
    (TS, "json", '{"a": 1}', PLAIN, "N'{\"a\": 1}'"),
    (TS, "datetime", "2026-10-19 12:34:56.123", PLAIN, "N'2026-10-19T12:34:56.123'"),
    (MY, "datetime", "0000-00-00", PLAIN, "'0000-00-00'"),
    (PG, "datetime", "2026-10-19 12:34:56+08", PLAIN, "'2026-10-19 12:34:56+08'"),
    (LITE, "other", " 42 ", PLAIN, "' 42 '"),  # stored as typed: bare, the spaces would be lost (05a review M10)
    (LITE, "other", "x42", PLAIN, "'x42'"),
    (PG, "other", "{1,2}", PLAIN, "'{1,2}'"),
    (MY, "text", "a\x00b", PLAIN, "'a\\0b'"),
    (TS, "text", "a\x00", PLAIN, "N'a' + CAST(NCHAR(0) AS NVARCHAR(MAX)) + N''"),
    # T-SQL drops a backslash and the line break after it from a literal: the literal ends after the backslash (Task 5b)
    (TS, "text", "a\\\nb", PLAIN, "CONCAT(CAST(N'' AS NVARCHAR(MAX)), N'a\\', N'\nb')"),
    (TS, "text", "k\\\r\n1", VARCHAR, "CONCAT(CAST('' AS VARCHAR(MAX)), 'k\\', '\r\n1')"),  # still no N: a varchar key
    (PG, "text", "a\\\nb", PLAIN, "'a\\\nb'"),
    (LITE, "text", "a\\\r\nb", PLAIN, "'a\\\r\nb'"),
    (MY, "text", "a\\\nb", PLAIN, "'a\\\\\nb'"),
    (LITE, "text", "a\x00", PLAIN, "'a' || char(0) || ''"),
])
def test_input_literals(dialect, kind, text, style, expected):  # Review Focus 1
    assert input_literal(dialect, col(kind), value(text), style) == expected


@pytest.mark.parametrize(("dialect", "text", "expected"), [
    (MY, "9007199254740993e0", "9007199254740993"),
    (MY, "1e3", "1000"),
    (LITE, "1e2", "100"),
    (TS, "10E-1", "1"),  # integral once scaled
    (MY, "1e37", "1" + "0" * 37),  # 38 digits
    (MY, "1e38", "1e38"),  # 39 digits: as typed, for the server to judge
    (MY, "1e999999999", "1e999999999"),  # never written out in full
    # a fraction keeps the typed form (ruling G6): a float column reads it best, MySQL may cut a long decimal
    (MY, "1e-5", "1e-5"),
    (MY, "1.5e-35", "1.5e-35"),
    (TS, "1.5E-3", "1.5E-3"),
    (LITE, "15e-1", "15e-1"),
    # beyond Decimal's exponent limits (InvalidOperation): as typed, never an error (review N1)
    (MY, "1e1000000000000000000", "1e1000000000000000000"),
    (TS, "1e-9999999999999999999999", "1e-9999999999999999999999"),
    (LITE, "-1e1000000000000000000", "-1e1000000000000000000"),
    (PG, "9007199254740993e0", "9007199254740993e0"),  # PostgreSQL reads it as an exact numeric
])
def test_an_integer_with_an_exponent_is_sent_exactly(dialect, text, expected):  # review F5, ruling G6: not a double
    assert input_literal(dialect, col("number"), value(text), PLAIN) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("42", "42"), (" -7 ", "' -7 '"), ("0", "0"), ("1.5", "1.5"), ("-9223372036854775808", "-9223372036854775808"),
    ("007", "'007'"), ("0912345678", "'0912345678'"), ("1.10", "'1.10'"), ("-0", "'-0'"), ("1e5", "'1e5'"),
    ("12345678901234567890", "'12345678901234567890'"), ("9223372036854775808", "'9223372036854775808'"),
])
def test_an_untyped_sqlite_value_is_stored_as_typed(text, expected):  # ruling G1: bare only when it reads back so
    assert input_literal(LITE, col("other"), value(text), PLAIN) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("2026-10-05", "N'20261005'"),
    (" 2026-10-05 12:00:00", "N'2026-10-05T12:00:00'"),
    ("2026-10-05  12:00:00", "N'2026-10-05T12:00:00'"),
    ("2026-10-05 1:02:03", "N'2026-10-05T1:02:03'"),  # the server refuses it rather than misreading it
    ("2026-10-05T12:00:00", "N'2026-10-05T12:00:00'"),
    ("12:34:56", "N'12:34:56'"),
])
def test_tsql_dates_never_depend_on_dateformat(text, expected):  # review F1: datetime reads yyyy-mm-dd as ydm in dmy
    assert input_literal(TS, col("datetime"), value(text), PLAIN) == expected
    assert input_literal(TS, col("datetime"), value(text), VARCHAR) == expected  # datetimes keep N (ruling E17)
    assert datetime_literal(LITE, " 2026-10-05") == "' 2026-10-05'"  # SQLite stores the text: never stripped


def test_null_is_null_and_default_has_no_literal():
    assert input_literal(PG, col("number"), CellInput(kind="null"), PLAIN) == "NULL"
    with pytest.raises(ValueError):
        input_literal(PG, col("number"), CellInput(kind="default"), PLAIN)


@pytest.mark.parametrize(("dialect", "kind", "text", "style", "code"), [
    (PG, "number", "12a", PLAIN, "invalid_number"),
    (MY, "number", "NaN", PLAIN, "invalid_number"),
    (TS, "number", "", PLAIN, "invalid_number"),
    (PG, "bool", "maybe", PLAIN, "invalid_bool"),
    (MY, "binary", "abc", PLAIN, "invalid_hex"),
    (TS, "binary", "0xzz", PLAIN, "invalid_hex"),
    (PG, "json", "[1,", PLAIN, "invalid_json"),
    (PG, "text", "a\x00b", PLAIN, "invalid_text"),
    (MY, "text", "a\x00b", NO_BACKSLASH, "invalid_text"),
    (LITE, "text", "half \ud83d", PLAIN, "invalid_text"),
    (TS, "datetime", "\udfff", PLAIN, "invalid_text"),
])
def test_text_that_cannot_be_sent_is_a_problem(dialect, kind, text, style, code):  # Review Focus 1
    with pytest.raises(ValueProblem) as raised:
        input_literal(dialect, col(kind), value(text), style)
    assert raised.value.code == code


@pytest.mark.parametrize("kind", ["number", "bool", "binary", "json"])
def test_a_problem_never_quotes_the_value(kind):  # spec §4: DbaGuy's own texts never echo values
    with pytest.raises(ValueProblem) as raised:
        input_literal(PG, col(kind), value("zz-secret-7"), PLAIN)
    assert "zz-secret" not in raised.value.message


def test_invalid_json_says_where():
    with pytest.raises(ValueProblem) as raised:
        input_literal(PG, col("json"), value('{\n  "a": 1,\n}'), PLAIN)
    assert raised.value.message == "Not valid JSON at line 2, column 9: Illegal trailing comma before end of object"


def test_a_value_beyond_the_limit_is_too_large():
    with pytest.raises(ValueProblem) as raised:
        input_literal(PG, col("text"), value("x" * (MAX_VALUE_CHARS + 1)), PLAIN)
    assert raised.value.code == "value_too_large"


MATCH_ROWS = [
    (PG, col("number", "as_text", "id"), 42, True, PLAIN, "id = 42"),
    (PG, col("number", "as_text", "id"), "9007199254740993", True, PLAIN, "id = 9007199254740993"),
    (PG, col("number", "as_text", "n"), "1.1000000000", False, PLAIN, "n::text = '1.1000000000'"),
    (PG, col("bool", "as_text", "b"), True, False, PLAIN, "b::text = 'true'"),
    (PG, col("binary", "as_text", "by"), BYTES, False, PLAIN, "by::text = '\\x00ff'"),
    (PG, col("json", "as_text", "j"), '{"b": 1,  "a": 2}', False, PLAIN, "j::text = '{\"b\": 1,  \"a\": 2}'"),
    (PG, col("text", "equals", "code"), "ab   ", False, PLAIN, "code = 'ab   '"),
    (PG, col("text", "as_text", "order"), "x", False, PLAIN, "\"order\"::text = 'x'"),
    (PG, col("number", "none", "f"), None, False, PLAIN, "f IS NULL"),
    (PG, col("number", "none", "f"), 1.5, False, PLAIN, None),
    (PG, col("text", "as_text", "t"), CUT_TEXT, False, PLAIN, None),
    (PG, col("binary", "as_text", "by"), PARTIAL_BYTES, True, PLAIN, None),
    (PG, col("text", "as_text", "t"), "half \ud83d", True, PLAIN, None),
    (MY, col("json", "equals", "doc"), '{"a": 1}', False, PLAIN, "doc = CAST('{\"a\": 1}' AS JSON)"),
    (MY, col("datetime", "equals", "d"), "0000-00-00", False, PLAIN, "CAST(d AS CHAR) = '0000-00-00'"),
    (MY, col("number", "equals", "amount"), "1.10", False, PLAIN, "amount = 1.10"),
    (MY, col("text", "equals", "path"), "C:\\x", False, NO_BACKSLASH, "path = 'C:\\x'"),
    (MY, col("text", "equals", "order"), "x", True, PLAIN, "`order` = 'x'"),
    (MY, col("text", "equals", "t"), "a\x00", False, NO_BACKSLASH, None),
    (TS, col("text", "equals", "vc", unicode=False), "k42", True, VARCHAR, "vc = 'k42'"),
    (TS, col("text", "equals", "nv"), "k42", True, PLAIN, "nv = N'k42'"),
    (TS, col("text", "equals", "nv"), "a\\\nb", False, PLAIN, "nv = CONCAT(CAST(N'' AS NVARCHAR(MAX)), N'a\\', N'\nb')"),
    (TS, col("text", "equals", "vc", unicode=False), "k\\\n1", True, VARCHAR,
     "vc = CONCAT(CAST('' AS VARCHAR(MAX)), 'k\\', '\n1')"),  # a varchar key keeps its type, so its seek
    (TS, col("text", "equals", "vc", unicode=False), "é", True, VARCHAR, "vc = N'é'"),
    (TS, col("datetime", "equals", "dt"), "2026-10-19 12:34:56.123", False, PLAIN, "dt = N'2026-10-19T12:34:56.123'"),
    (TS, col("bool", "equals", "flag"), True, False, PLAIN, "flag = 1"),
    (TS, col("binary", "equals", "vb"), BYTES, True, PLAIN, "vb = 0x00ff"),
    (TS, col("number", "equals", "order"), 7, True, PLAIN, "[order] = 7"),
    (LITE, col("number", "equals", "i"), 42, True, PLAIN, "i = 42"),
    (LITE, col("number", "equals", "i"), "9007199254740993", True, PLAIN, "i = 9007199254740993"),
    (LITE, col("text", "equals", "t"), "42", False, PLAIN, "t = '42'"),
    (LITE, col("other", "equals", "o"), "42", False, PLAIN, "o = '42'"),
    (LITE, col("other", "equals", "o"), 42, False, PLAIN, "o = 42"),
    (LITE, col("other", "equals", "o"), "9007199254740993", True, PLAIN, "o IN (9007199254740993, '9007199254740993')"),
    (LITE, col("other", "equals", "o"), "9" * 5000, False, PLAIN, "o = '" + "9" * 5000 + "'"),  # text, not int64
    (LITE, col("other", "equals", "o"), "9999999999999999999", True, PLAIN, "o = '9999999999999999999'"),  # > 2⁶³−1
    (LITE, col("other", "equals", "o"), "-0009007199254740993", True, PLAIN, "o = '-0009007199254740993'"),
    (LITE, col("other", "equals", "o"), "-9223372036854775808", True, PLAIN,
     "o IN (-9223372036854775808, '-9223372036854775808')"),
    (TS, col("datetime", "equals", "d"), "2026-10-05", True, PLAIN, "d = N'20261005'"),
    (MY, col("json", "equals", "doc"), JSON_NUL, False, NO_BACKSLASH, None),
    (MY, col("json", "equals", "doc"), JSON_NUL, False, PLAIN, "doc = CAST('\"a\\0\"' AS JSON)"),
    (PG, col("json", "as_text", "j"), {"$type": "json", "text": '"\ud83d"'}, False, PLAIN, None),
    (PG, col("text", "as_text", "t"), "a\x00", False, PLAIN, None),
    (LITE, col("number", "none", "r"), 1.5, False, PLAIN, None),
]


@pytest.mark.parametrize(("dialect", "column", "cell", "key", "style", "expected"), MATCH_ROWS)
def test_match_conditions(dialect, column, cell, key, style, expected):  # Review Focus 2, 3
    assert match_condition(dialect, column, cell, style, key=key) == expected


@pytest.mark.parametrize(("dialect", "column", "cell", "key", "style", "expected"), MATCH_ROWS)
def test_no_term_is_explained_by_the_cell_or_the_columns_compare(dialect, column, cell, key, style, expected):
    """What the plan builder relies on to say why (review F3, G2): no term iff the cell is not NULL (`c IS NULL` always
    is one) and it has an unmatched reason or it is an original whose column compares `none`."""
    explained = cell is not None and (
        unmatched_reason(dialect, cell, style) is not None or (not key and column.compare == "none"))
    assert (expected is None) == explained


@pytest.mark.parametrize(("dialect", "cell", "style", "reason"), [
    (PG, None, PLAIN, None),
    (PG, 42, PLAIN, None),
    (PG, BYTES, PLAIN, None),
    (PG, CUT_TEXT, PLAIN, "preview_only"),
    (TS, PARTIAL_BYTES, PLAIN, "preview_only"),
    (LITE, "half \ud83d", PLAIN, "unsendable"),
    (PG, "a\x00", PLAIN, "unsendable"),
    (MY, "a\x00", NO_BACKSLASH, "unsendable"),
    (MY, "a\x00", PLAIN, None),
    (TS, "a\x00", VARCHAR, None),
    (LITE, "a\x00", PLAIN, None),
    (MY, JSON_NUL, NO_BACKSLASH, "unsendable"),
])
def test_unmatched_reason(dialect, cell, style, reason):
    assert unmatched_reason(dialect, cell, style) == reason


def test_a_cell_that_is_not_a_grid_cell_is_refused():
    with pytest.raises(ValidationFailed):
        match_condition(PG, col(), {"$type": "nope"}, PLAIN, key=True)
    with pytest.raises(ValidationFailed):
        unmatched_reason(PG, {"$type": "nope"}, PLAIN)


def test_cell_text_shows_a_key_as_a_message_would():
    assert [cell_text(None, "number"), cell_text(42, "number"), cell_text("9007199254740993", "number")] == [
        "NULL", "42", "9007199254740993"]
    assert [cell_text("it's", "text"), cell_text(BYTES, "binary"), cell_text(True, "bool")] == ["'it's'", "0x00ff", "true"]
    assert cell_text("x" * 100, "text") == "'" + "x" * 60 + "…'"


def test_a_cut_value_shows_one_ellipsis():
    assert cell_text({"$type": "text", "preview": "x" * 100, "size": 200, "truncated": True}, "text") == (
        "'" + "x" * 60 + "…'")
    assert cell_text(CUT_TEXT, "text") == "'abc…'"
    assert cell_text(PARTIAL_BYTES, "binary") == "0x" + "0" * 60 + "…"
    assert cell_text({"$type": "bytes", "size": 2, "preview": "00"}, "binary") == "0x00…"


def test_quote_literal_options():  # D-05-2 (A8), D-05-13, ruling E16
    assert quote_literal(TS, "k42", national=False) == "'k42'"
    assert quote_literal(MY, "a\\b", backslash_escapes=False) == "'a\\b'"
    assert quote_literal(MY, "a\\b") == "'a\\\\b'"
    with pytest.raises(ValueError):
        quote_literal(MY, "a\x00", backslash_escapes=False)
    assert json_literal(MY, '{"p": 1}', backslash_escapes=False) == "CAST('{\"p\": 1}' AS JSON)"
    assert datetime_literal(TS, "2026-10-19 01:02:03") == "N'2026-10-19T01:02:03'"


def test_transaction_frames_defaults_and_scripts():  # D-05-4, spec §3.4
    assert transaction_frame(PG) == TransactionFrame("BEGIN", "COMMIT")
    assert transaction_frame(MY) == TransactionFrame("START TRANSACTION", "COMMIT")
    assert transaction_frame(TS) == TransactionFrame("SET NOCOUNT OFF;\nBEGIN TRANSACTION", "COMMIT TRANSACTION")
    assert transaction_frame(LITE) == TransactionFrame("BEGIN IMMEDIATE", "COMMIT")
    with pytest.raises(ValueError):
        transaction_frame(Dialect.SQLPP)
    assert insert_defaults(PG, "public.t") == "INSERT INTO public.t DEFAULT VALUES"
    assert insert_defaults(MY, "dbaguy.t") == "INSERT INTO dbaguy.t () VALUES ()"
    assert insert_defaults(LITE, "main.t") == "INSERT OR ABORT INTO main.t DEFAULT VALUES"  # 05a final review I1
    assert [update_head(d, "s.t") for d in (PG, MY, TS, LITE)] == ["UPDATE s.t"] * 3 + ["UPDATE OR ABORT s.t"]
    assert [insert_head(d, "s.t") for d in (PG, MY, TS, LITE)] == ["INSERT INTO s.t"] * 3 + ["INSERT OR ABORT INTO s.t"]
    assert script_text(PG, ["BEGIN", "DELETE FROM t WHERE id = 1", "COMMIT"]) == (
        "BEGIN;\nDELETE FROM t WHERE id = 1;\nCOMMIT;")
    assert script_text(TS, ["SET NOCOUNT OFF;\nBEGIN TRANSACTION", "DELETE FROM t WHERE id = 1", "COMMIT TRANSACTION"]) == (
        "SET NOCOUNT OFF;\nBEGIN TRANSACTION\nGO\nDELETE FROM t WHERE id = 1\nGO\nCOMMIT TRANSACTION\nGO")
