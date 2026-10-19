"""Literals of grid edits (spec ⑤ §3.3.3, D-05-2, D-05-13): typed values, keys and original values per dialect × kind,
and the dialect's transaction frame, all-default row and script text."""

import pytest

from dbaguy_backend.domain.edits import MAX_VALUE_CHARS, CellInput, EditColumn
from dbaguy_backend.domain.errors import ValidationFailed
from dbaguy_backend.sql.dialects import (
    Dialect, TransactionFrame, datetime_literal, insert_defaults, json_literal, quote_literal, script_text,
    transaction_frame,
)
from dbaguy_backend.sql.literals import LiteralStyle, ValueProblem, cell_text, input_literal, match_condition

PG, MY, TS, LITE = Dialect.POSTGRES, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLITE
PLAIN = LiteralStyle()
NO_BACKSLASH = LiteralStyle(backslash_escapes=False)
VARCHAR = LiteralStyle(national=False)
BYTES = {"$type": "bytes", "size": 2, "preview": "00ff"}
PARTIAL_BYTES = {"$type": "bytes", "size": 100, "preview": "00" * 64}
CUT_TEXT = {"$type": "text", "preview": "abc", "size": 70_000, "truncated": True}


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
    (TS, "number", "-1e3", PLAIN, "-1e3"),
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
    (LITE, "other", " 42 ", PLAIN, "42"),
    (LITE, "other", "x42", PLAIN, "'x42'"),
    (PG, "other", "{1,2}", PLAIN, "'{1,2}'"),
    (MY, "text", "a\x00b", PLAIN, "'a\\0b'"),
    (TS, "text", "a\x00", PLAIN, "N'a' + CAST(NCHAR(0) AS NVARCHAR(MAX)) + N''"),
    (LITE, "text", "a\x00", PLAIN, "'a' || char(0) || ''"),
])
def test_input_literals(dialect, kind, text, style, expected):  # Review Focus 1
    assert input_literal(dialect, col(kind), value(text), style) == expected


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


@pytest.mark.parametrize(("dialect", "column", "cell", "key", "style", "expected"), [
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
    (LITE, col("number", "none", "r"), 1.5, False, PLAIN, None),
])
def test_match_conditions(dialect, column, cell, key, style, expected):  # Review Focus 2, 3
    assert match_condition(dialect, column, cell, style, key=key) == expected


def test_a_cell_that_is_not_a_grid_cell_is_refused():
    with pytest.raises(ValidationFailed):
        match_condition(PG, col(), {"$type": "nope"}, PLAIN, key=True)


def test_cell_text_shows_a_key_as_a_message_would():
    assert [cell_text(None, "number"), cell_text(42, "number"), cell_text("9007199254740993", "number")] == [
        "NULL", "42", "9007199254740993"]
    assert [cell_text("it's", "text"), cell_text(BYTES, "binary"), cell_text(True, "bool")] == ["'it's'", "0x00ff", "true"]
    assert cell_text("x" * 100, "text") == "'" + "x" * 60 + "…'"


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
    assert script_text(PG, ["BEGIN", "DELETE FROM t WHERE id = 1", "COMMIT"]) == (
        "BEGIN;\nDELETE FROM t WHERE id = 1;\nCOMMIT;")
    assert script_text(TS, ["SET NOCOUNT OFF;\nBEGIN TRANSACTION", "DELETE FROM t WHERE id = 1", "COMMIT TRANSACTION"]) == (
        "SET NOCOUNT OFF;\nBEGIN TRANSACTION\nGO\nDELETE FROM t WHERE id = 1\nGO\nCOMMIT TRANSACTION\nGO")
