import io
import json
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest

from dbaguy_backend.adapters.couchbase.jsontext import one_line
from dbaguy_backend.adapters.values import MISSING, JsonText as J
from dbaguy_backend.domain.errors import UnsupportedFormat, ValidationFailed
from dbaguy_backend.domain.execution import ColumnCategory as C
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.formatting import (
    FormatColumn, FormatKind as F, RowFormatOptions, format_rows, open_raw_json_writer, open_writer,
    unsupported_reason, value_condition,
)

PG, LITE, MY, TS = Dialect.POSTGRES, Dialect.SQLITE, Dialect.MYSQL, Dialect.TSQL
COLS = [FormatColumn(name="id", category=C.NUMBER), FormatColumn(name="note", category=C.TEXT),
        FormatColumn(name="ok", category=C.BOOL)]
ROWS = [[1, "a\tb", True], ["9007199254740993", None, False]]


def fmt(kind, rows=ROWS, cols=COLS, dialect=PG, source="encoded", **options):
    return format_rows(kind, cols, rows, RowFormatOptions(**options), dialect, source)


def test_tsv_and_csv():
    assert fmt(F.TSV).text == 'id\tnote\tok\n1\t"a\tb"\ttrue\n9007199254740993\t\tfalse'
    assert fmt(F.TSV, header=False, null_text="NULL").text.splitlines()[1] == "9007199254740993\tNULL\tfalse"
    assert fmt(F.CSV).text == 'id,note,ok\n1,a\tb,true\n9007199254740993,,false\n'


def test_json_jsonl_markdown():
    assert '"note": null' in fmt(F.JSON).text
    assert fmt(F.JSONL).text.splitlines()[0] == '{"id": 1, "note": "a\\tb", "ok": true}'
    md = fmt(F.MARKDOWN, rows=[[1, "x|y\nz", True]]).text.splitlines()
    assert md[0] == "| id | note | ok |" and md[2] == "| 1 | x\\|y z | true |"


def test_insert_per_dialect_and_truncation_warning():
    rows = [[1, "it's", True], [2, {"$type": "text", "preview": "abc", "size": 70000, "truncated": True}, None]]
    result = fmt(F.INSERT, rows=rows, table_schema="sales", table_name="Order Items")
    assert result.text.splitlines() == [
        "INSERT INTO sales.\"Order Items\" (id, note, ok) VALUES (1, 'it''s', TRUE);",
        'INSERT INTO sales."Order Items" (id, note, ok) VALUES (2, NULL, NULL);',
    ]
    assert result.warnings and "truncated" in result.warnings[0]
    lite = fmt(F.INSERT, rows=[[1, "x", False]], dialect=LITE)
    assert lite.text == "INSERT INTO table_name (id, note, ok) VALUES (1, 'x', 0);"


def test_insert_in_mysql():
    rows = [[1, "it's \\ ok", True], [2, None, False]]
    result = fmt(F.INSERT, rows=rows, dialect=MY, table_schema="dbaguy", table_name="Order Items")
    assert result.text.splitlines() == [
        r"INSERT INTO dbaguy.`Order Items` (id, note, ok) VALUES (1, 'it''s \\ ok', TRUE);",
        "INSERT INTO dbaguy.`Order Items` (id, note, ok) VALUES (2, NULL, FALSE);",
    ]
    cols = [FormatColumn(name="b", category=C.BINARY)]
    assert fmt(F.INSERT, rows=[[b"\xde\xad"]], cols=cols, source="raw", dialect=MY).text.endswith("(X'dead');")


def test_insert_in_tsql():
    rows = [[1, "it's 中文", True], [2, None, False]]
    result = fmt(F.INSERT, rows=rows, dialect=TS, table_schema="dbo", table_name="Order Items")
    assert result.text.splitlines() == [
        "INSERT INTO dbo.[Order Items] (id, note, ok) VALUES (1, N'it''s 中文', 1);",
        "INSERT INTO dbo.[Order Items] (id, note, ok) VALUES (2, NULL, 0);",
    ]
    cols = [FormatColumn(name="b", category=C.BINARY)]
    assert fmt(F.INSERT, rows=[[b"\xde\xad"]], cols=cols, source="raw", dialect=TS).text.endswith("(0xdead);")
    seen = [FormatColumn(name="seen", category=C.DATETIME)]
    assert fmt(F.INSERT, rows=[["2026-10-18 12:34:56.123"]], cols=seen, dialect=TS).text.endswith(
        "(N'2026-10-18T12:34:56.123');")  # R-03b-P10


@pytest.mark.parametrize("dialect, column, value, category, expected", [
    (PG, "note", None, C.TEXT, "note IS NULL"),
    (PG, "Select", "it's", C.TEXT, "\"Select\" = 'it''s'"),
    (PG, "ok", True, C.BOOL, "ok = TRUE"),
    (LITE, "ok", False, C.BOOL, "ok = 0"),
    (MY, "flag", True, C.BOOL, "flag = TRUE"),
    (MY, "order", 42, C.NUMBER, "`order` = 42"),
    (MY, "ratio", 1.5, C.NUMBER, "ratio = 1.5"),
    (MY, "amount", "12345678901234567890.1234567890", C.NUMBER, "amount = 12345678901234567890.1234567890"),
    (MY, "code", "1e3; DROP TABLE t", C.NUMBER, "code = '1e3; DROP TABLE t'"),  # not a number: a string
    (MY, "note", "it's \\ back", C.TEXT, r"note = 'it''s \\ back'"),
    (MY, "a`b", "x", C.TEXT, "`a``b` = 'x'"),
    (MY, "data", {"$type": "bytes", "size": 2, "preview": "dead"}, C.BINARY, "data = X'dead'"),
    (MY, "data", {"$type": "bytes", "size": 100, "preview": "dead"}, C.BINARY, None),  # only a preview
    (MY, "body", {"$type": "text", "preview": "x", "size": 70000, "truncated": True}, C.TEXT, None),
    # MySQL compares a JSON column with a plain string as a JSON string scalar: the text is cast to JSON instead
    (MY, "doc", '{"a": [1, 2], "b": null}', C.JSON, """doc = CAST('{"a": [1, 2], "b": null}' AS JSON)"""),
    (MY, "doc", '{"q": "it\'s \\\\ x"}', C.JSON, r"""doc = CAST('{"q": "it''s \\\\ x"}' AS JSON)"""),
    (MY, "doc", None, C.JSON, "doc IS NULL"),
    (PG, "doc", '{"a": 1}', C.JSON, """doc = '{"a": 1}'"""),
    # MySQL keeps dates with a zero part that its default sql_mode rejects as literals (1525): compare the text
    (MY, "zero_dt", "0000-00-00 00:00:00", C.DATETIME, "CAST(zero_dt AS CHAR) = '0000-00-00 00:00:00'"),
    (MY, "born", "0000-00-00", C.DATETIME, "CAST(born AS CHAR) = '0000-00-00'"),
    (MY, "part_zero", "2026-00-15", C.DATETIME, "CAST(part_zero AS CHAR) = '2026-00-15'"),
    (MY, "Due Day", "2026-10-00 08:00:00.5", C.DATETIME, "CAST(`Due Day` AS CHAR) = '2026-10-00 08:00:00.5'"),
    (MY, "seen", "2026-10-17 00:00:00", C.DATETIME, "seen = '2026-10-17 00:00:00'"),  # a real date keeps its index
    (MY, "lasted", "-00:30:00", C.DATETIME, "lasted = '-00:30:00'"),  # TIME
    (MY, "yr", "2000", C.DATETIME, "yr = '2000'"),
    (PG, "born", "0000-00-00", C.DATETIME, "born = '0000-00-00'"),  # only MySQL has zero dates
    (LITE, "doc", '{"a": 1}', C.JSON, """doc = '{"a": 1}'"""),
    (TS, "flag", True, C.BOOL, "flag = 1"),
    (TS, "order", 42, C.NUMBER, "[order] = 42"),
    (TS, "note", "it's", C.TEXT, "note = N'it''s'"),
    (TS, "seen", "2026-10-18 12:34:56.123", C.DATETIME, "seen = N'2026-10-18T12:34:56.123'"),
    (TS, "born", "2026-10-18", C.DATETIME, "born = N'2026-10-18'"),
    (MY, "seen", "2026-10-18 12:34:56", C.DATETIME, "seen = '2026-10-18 12:34:56'"),
    (TS, "data", {"$type": "bytes", "size": 2, "preview": "dead"}, C.BINARY, "data = 0xdead"),
])
def test_value_condition(dialect, column, value, category, expected):
    assert value_condition(dialect, column, value, category) == expected


def test_value_condition_rejects_an_object_that_is_not_a_cell():
    with pytest.raises(ValidationFailed):
        value_condition(MY, "x", {"$type": "nope"}, C.TEXT)


def test_raw_values_for_export():
    cols = [FormatColumn(name="b", category=C.BINARY), FormatColumn(name="d", category=C.DATETIME),
            FormatColumn(name="n", category=C.NUMBER)]
    rows = [[b"\xde\xad", date(2026, 1, 2), Decimal("12345678901234567890.5")]]
    assert fmt(F.CSV, rows=rows, cols=cols, source="raw").text == "b,d,n\n0xdead,2026-01-02,12345678901234567890.5\n"
    pg_insert = fmt(F.INSERT, rows=rows, cols=cols, source="raw").text
    assert "'\\xdead'::bytea" in pg_insert and "'2026-01-02'" in pg_insert and "12345678901234567890.5)" in pg_insert
    assert "X'dead'" in fmt(F.INSERT, rows=rows, cols=cols, source="raw", dialect=LITE).text


def test_in_list():
    col = [FormatColumn(name="id", category=C.NUMBER)]
    assert fmt(F.IN_LIST, rows=[[1], [2], [1]], cols=col).text == "(1, 2)"
    assert fmt(F.IN_LIST, rows=[["a"], [None]], cols=[FormatColumn(name="s", category=C.TEXT)]).text == "('a', NULL)"
    with pytest.raises(ValidationFailed):
        fmt(F.IN_LIST)


def test_streaming_writers():
    buffer = io.StringIO()
    writer = open_writer(F.JSON, buffer, COLS, RowFormatOptions(), PG)
    writer.write([[1, "x", True]])
    writer.write([[2, "y", False]])
    writer.close()
    assert buffer.getvalue().startswith("[\n") and buffer.getvalue().rstrip().endswith("]")
    assert [r["id"] for r in json.loads(buffer.getvalue())] == [1, 2]
    with pytest.raises(ValueError):
        open_writer(F.IN_LIST, io.StringIO(), COLS, RowFormatOptions(), PG)


BINARY = [FormatColumn(name="b", category=C.BINARY)]
COMPLETE = {"$type": "bytes", "size": 2, "preview": "dead"}
CUT = {"$type": "bytes", "size": 100, "preview": "ab" * 64}


def test_encoded_binary_that_fits_the_preview_is_complete():
    assert fmt(F.CSV, rows=[[COMPLETE], [CUT]], cols=BINARY).text == "b\n0xdead\n0x" + "ab" * 64 + "…\n"
    pg = fmt(F.INSERT, rows=[[COMPLETE], [CUT]], cols=BINARY, table_name="t")
    assert pg.text.splitlines() == ["INSERT INTO t (b) VALUES ('\\xdead'::bytea);", "INSERT INTO t (b) VALUES (NULL);"]
    assert pg.warnings == [
        "1 value(s) were truncated in the grid and written as NULL; export the full result for complete values"
    ]
    lite = fmt(F.INSERT, rows=[[COMPLETE]], cols=BINARY, table_name="t", dialect=LITE)
    assert lite.text == "INSERT INTO t (b) VALUES (X'dead');" and lite.warnings == []


def test_truncated_text_keeps_preview_in_text_formats():
    cols = [FormatColumn(name="t", category=C.TEXT)]
    cell = {"$type": "text", "preview": "abc", "size": 70000, "truncated": True}
    assert fmt(F.JSONL, rows=[[cell]], cols=cols).text == '{"t": "abc…"}'
    assert fmt(F.IN_LIST, rows=[[cell]], cols=cols).warnings != []


def test_non_numeric_text_in_a_number_column_is_quoted():
    cols = [FormatColumn(name="n", category=C.NUMBER)]
    assert fmt(F.IN_LIST, rows=[["1.5e-3"], ["NaN"], ["1; DROP"]], cols=cols).text == "(1.5e-3, 'NaN', '1; DROP')"


def test_duplicate_column_names_do_not_lose_json_data():
    cols = [FormatColumn(name="id", category=C.NUMBER)] * 2 + [FormatColumn(name="id_2", category=C.NUMBER)]
    assert fmt(F.JSONL, rows=[[1, 2, 3]], cols=cols).text == '{"id": 1, "id_3": 2, "id_2": 3}'


def test_csv_header_survives_zero_rows_and_ragged_rows_are_rejected():
    assert fmt(F.CSV, rows=[]).text == "id,note,ok\n"
    assert fmt(F.JSON, rows=[]).text == "[\n]"
    with pytest.raises(ValidationFailed):
        fmt(F.TSV, rows=[[1, "a"]])


NUMBERS = [FormatColumn(name=name, category=C.NUMBER) for name in ("big", "dec", "exp", "lead", "odd")]


def test_json_writes_number_columns_as_bare_numbers_with_their_exact_text():
    # PG loads bigint and numeric as text; "12.50" must stay 12.50, never become the float 12.5
    rows = [["9007199254740993", "12.50", "-1.5E+3", "007", "NaN"]]
    expected = '{"big": 9007199254740993, "dec": 12.50, "exp": -1.5E+3, "lead": "007", "odd": "NaN"}'  # 007 is not JSON
    assert fmt(F.JSONL, rows=rows, cols=NUMBERS).text == expected
    parsed = json.loads(fmt(F.JSON, rows=rows, cols=NUMBERS).text)
    assert parsed == [{"big": 9007199254740993, "dec": 12.5, "exp": -1500.0, "lead": "007", "odd": "NaN"}]
    text_column = [FormatColumn(name="t", category=C.TEXT)]
    assert fmt(F.JSONL, rows=[["42"]], cols=text_column).text == '{"t": "42"}'


def test_json_number_columns_from_raw_values_keep_every_digit():
    rows = [[2**60, Decimal("12345678901234567890.50"), Decimal("1E+2"), 1, Decimal("NaN")]]
    assert fmt(F.JSONL, rows=rows, cols=NUMBERS, source="raw").text == (
        '{"big": 1152921504606846976, "dec": 12345678901234567890.50, "exp": 1E+2, "lead": 1, "odd": "NaN"}'
    )


def test_json_columns_are_embedded_as_json_and_invalid_ones_stay_strings():
    cols = [FormatColumn(name="j", category=C.JSON)]
    cut = {"$type": "text", "preview": '{"k":', "size": 70000, "truncated": True}
    rows = [['{"k":[1,"x"]}'], ['{\n  "a": 1\n}'], ["not json"], ["NaN"], ['{"k": 1} x'], ["123"], ['"s"'], [None], [cut]]
    assert fmt(F.JSONL, rows=rows, cols=cols).text.splitlines() == [
        '{"j": {"k": [1, "x"]}}', '{"j": {"a": 1}}', '{"j": "not json"}', '{"j": "NaN"}', '{"j": "{\\"k\\": 1} x"}',
        '{"j": 123}', '{"j": "s"}', '{"j": null}', '{"j": "{\\"k\\":…"}',
    ]
    assert json.loads(fmt(F.JSON, rows=rows[:1], cols=cols).text) == [{"j": {"k": [1, "x"]}}]


def test_json_columns_keep_the_exact_numbers_inside():
    cols = [FormatColumn(name="j", category=C.JSON)]
    row = [['{"n": 12345678901234567890.123456789, "e": 1e2, "l": [0.10, 5]}']]
    assert fmt(F.JSONL, rows=row, cols=cols).text == '{"j": {"n": 12345678901234567890.123456789, "e": 1E+2, "l": [0.10, 5]}}'
    assert fmt(F.JSONL, rows=[[{"k": [1]}]], cols=cols, source="raw").text == '{"j": {"k": [1]}}'  # a driver-parsed dict


def test_typed_json_does_not_change_the_other_formats():
    cols = [FormatColumn(name="j", category=C.JSON), FormatColumn(name="n", category=C.NUMBER)]
    assert fmt(F.CSV, rows=[['{"k":1}', "12.50"]], cols=cols).text == 'j,n\n"{""k"":1}",12.50\n'
    assert fmt(F.INSERT, rows=[['{"k":1}', "12.50"]], cols=cols, table_name="t").text == (
        "INSERT INTO t (j, n) VALUES ('{\"k\":1}', 12.50);"
    )


MALFORMED_CELLS = [
    {"$type": "bytes"},
    {"$type": "bytes", "preview": "ab"},  # no size
    {"$type": "bytes", "preview": 5, "size": "x"},
    {"$type": "bytes", "preview": "ab", "size": None},
    {"$type": "bytes", "preview": "ab", "size": True},  # a bool is not a size
    {"$type": "bytes", "preview": "ab", "size": -1},
    {"$type": "bytes", "preview": "abc", "size": 2},  # not whole bytes
    {"$type": "bytes", "preview": "zz", "size": 1},  # not hex
    {"$type": "text"},
    {"$type": "text", "truncated": True},  # no preview
    {"$type": "text", "preview": "abc"},  # no size
    {"$type": "text", "preview": "abc", "size": None},
    {"$type": "text", "preview": 7, "size": 3},
    {"$type": "text", "preview": "abc", "size": 3, "truncated": "yes"},
    {"$type": "geometry", "preview": "x", "size": 1},
    {"preview": "x", "size": 1},
]
ONE_TEXT_COLUMN = [FormatColumn(name="v", category=C.TEXT)]


@pytest.mark.parametrize("cell", MALFORMED_CELLS)
def test_malformed_encoded_cells_are_validation_errors_in_every_format(cell):
    for kind in F:
        with pytest.raises(ValidationFailed) as info:
            fmt(kind, rows=[["fine"], [cell]], cols=ONE_TEXT_COLUMN)
        assert "Row index 1, column index 0" in info.value.message, kind
        assert info.value.detail == {"row": 1, "column": 0}


def test_a_malformed_cell_error_does_not_echo_the_cell():
    for cell in ({"$type": "bytes", "preview": "leak-marker", "size": 1}, {"$type": "leak-marker"},
                 {"$type": "text", "preview": "leak-marker", "size": "leak-marker"}):
        with pytest.raises(ValidationFailed) as info:
            fmt(F.CSV, rows=[[cell]], cols=ONE_TEXT_COLUMN)
        assert "leak-marker" not in info.value.message and "leak-marker" not in str(info.value.detail)


def test_well_formed_encoded_cells_still_format():
    cells = [{"$type": "text", "preview": "abc", "size": 3}, {"$type": "text", "preview": "abc", "size": 3, "truncated": False},
             {"$type": "bytes", "preview": "", "size": 0}]
    assert fmt(F.CSV, rows=[[c] for c in cells], cols=ONE_TEXT_COLUMN, header=False).text == "abc\nabc\n0x\n"


def test_raw_values_are_not_checked_for_the_encoded_shape():
    # a driver dict that happens to look like an encoded cell is just a JSON value, written as text
    rows = [[{"$type": "bytes"}]]
    assert fmt(F.JSONL, rows=rows, cols=ONE_TEXT_COLUMN, source="raw").text == '{"v": "{\\"$type\\": \\"bytes\\"}"}'


SQ = Dialect.SQLPP
MIX = C.MIXED
DOC_COLUMNS = [FormatColumn(name="doc", category=MIX), FormatColumn(name="n", category=C.NUMBER),
               FormatColumn(name="s", category=MIX)]
RAW_ROWS = [[J('{"z":1,"a":[1e400, 1.10]}'), J("9007199254740993"), "[1] stays a string"], [MISSING, 5, None]]
ENCODED_ROWS = [[{"$type": "json", "text": '{"z":1,"a":[1e400, 1.10]}'}, {"$type": "json", "text": "9007199254740993"},
                 "[1] stays a string"], [{"$type": "missing"}, 5, None]]


@pytest.mark.parametrize("source, rows", [("raw", RAW_ROWS), ("encoded", ENCODED_ROWS)])
def test_json_cells_in_every_writer(source, rows):  # Review Focus 1
    def run(kind, **options):
        return format_rows(kind, DOC_COLUMNS, rows, RowFormatOptions(**options), SQ, source).text

    assert run(F.CSV, null_text="NULL") == (
        'doc,n,s\n"{""z"":1,""a"":[1e400, 1.10]}",9007199254740993,[1] stays a string\n,5,NULL\n'
    )  # MISSING is empty even when NULL has a text
    assert run(F.JSONL).splitlines() == [
        '{"doc": {"z":1,"a":[1e400, 1.10]}, "n": 9007199254740993, "s": "[1] stays a string"}',
        '{"n": 5, "s": null}',  # the missing key is left out
    ]
    assert run(F.TSV).splitlines()[2] == "\t5\t"
    assert run(F.MARKDOWN).splitlines()[3] == "|  | 5 |  |"


@pytest.mark.parametrize("source, rows", [("raw", RAW_ROWS), ("encoded", ENCODED_ROWS)])
def test_json_array_and_text_formats_keep_the_cell_text(source, rows):
    def run(kind, **options):
        return format_rows(kind, DOC_COLUMNS, rows, RowFormatOptions(**options), SQ, source).text

    assert run(F.JSON) == (
        '[\n  {"doc": {"z":1,"a":[1e400, 1.10]}, "n": 9007199254740993, "s": "[1] stays a string"},\n'
        '  {"n": 5, "s": null}\n]'
    )
    assert run(F.TSV).splitlines()[1] == '"{""z"":1,""a"":[1e400, 1.10]}"\t9007199254740993\t[1] stays a string'
    assert run(F.MARKDOWN).splitlines()[2] == '| {"z":1,"a":[1e400, 1.10]} | 9007199254740993 | [1] stays a string |'
    assert run(F.TSV, null_text="NULL").splitlines()[2] == "\t5\tNULL"


def test_a_mixed_columns_string_is_never_read_as_json():
    column = [FormatColumn(name="v", category=MIX)]
    rows = [["[1, 2]"], ['{"a": 1}'], ["123"], [J("123")], [J('{"a": 1}')], ["true"]]
    assert format_rows(F.JSONL, column, rows, RowFormatOptions(), SQ, "raw").text.splitlines() == [
        '{"v": "[1, 2]"}', '{"v": "{\\"a\\": 1}"}', '{"v": "123"}', '{"v": 123}', '{"v": {"a": 1}}', '{"v": "true"}',
    ]


def test_a_json_cell_with_a_line_break_stays_on_one_jsonl_line_and_keeps_its_bytes_in_a_json_array():
    stored = '{"z" :\n 1, "a": [1e400,\r\n 1.10], "s": "a  b"}'
    column = [FormatColumn(name="v", category=MIX)]
    jsonl = format_rows(F.JSONL, column, [[J(stored)], [J('{"z" : 1}')]], RowFormatOptions(), SQ, "raw").text
    assert jsonl.splitlines() == ['{"v": {"z":1,"a":[1e400,1.10],"s":"a  b"}}', '{"v": {"z" : 1}}']
    array = format_rows(F.JSON, column, [[J(stored)]], RowFormatOptions(), SQ, "raw").text
    assert array == "[\n  {\"v\": " + stored + "}\n]"


def test_sqlpp_in_list_and_no_insert():
    column = [FormatColumn(name="v", category=MIX)]
    rows = [[1], ["a"], [True], [None], [MISSING], [J('{"k":1}')], [1]]
    assert format_rows(F.IN_LIST, column, rows, RowFormatOptions(), SQ, "raw").text == '[1, "a", TRUE, NULL, MISSING, {"k":1}]'
    assert unsupported_reason(F.INSERT, SQ) == "INSERT statements are not offered for Couchbase"
    assert unsupported_reason(F.INSERT, PG) is None and unsupported_reason(F.CSV, SQ) is None
    with pytest.raises(UnsupportedFormat):
        format_rows(F.INSERT, column, rows, RowFormatOptions(), SQ, "raw")
    with pytest.raises(UnsupportedFormat):  # export goes through open_writer
        open_writer(F.INSERT, StringIO(), column, RowFormatOptions(), SQ)


def test_other_dialects_keep_their_in_list_and_insert():
    column = [FormatColumn(name="v", category=C.TEXT)]
    assert fmt(F.IN_LIST, rows=[["a"], ["b"]], cols=column, dialect=MY).text == "('a', 'b')"
    assert fmt(F.INSERT, rows=[["a"]], cols=column, dialect=PG, table_name="t").text == "INSERT INTO t (v) VALUES ('a');"


@pytest.mark.parametrize("value, category, expected", [
    ({"$type": "missing"}, MIX, "amount IS MISSING"),
    ({"$type": "json", "text": '{"a": 1}'}, MIX, 'amount = {"a": 1}'),
    ("x", MIX, 'amount = "x"'),
    (5, C.NUMBER, "amount = 5"),
    (None, MIX, "amount IS NULL"),
])
def test_sqlpp_value_condition(value, category, expected):
    assert value_condition(SQ, "amount", value, category) == expected


def test_a_truncated_json_cell_has_no_condition_and_no_insert_value():
    cut = {"$type": "text", "preview": '{"k":', "size": 70000, "truncated": True}
    assert value_condition(SQ, "doc", cut, MIX) is None
    result = format_rows(F.IN_LIST, [FormatColumn(name="v", category=MIX)], [[cut]], RowFormatOptions(), SQ, "encoded")
    assert result.text == "[NULL]" and result.warnings


@pytest.mark.parametrize("cell", [{"$type": "json"}, {"$type": "json", "text": 5}, {"$type": "json", "text": None}])
def test_a_malformed_json_cell_is_refused(cell):
    with pytest.raises(ValidationFailed, match="json"):
        format_rows(F.CSV, [FormatColumn(name="d", category=MIX)], [[cell]], RowFormatOptions(), SQ, "encoded")


def test_a_raw_json_cell_dict_is_not_a_cell():
    # a driver value that looks like an encoded cell is a JSON value: only the encoded source reads cells
    rows = [[{"$type": "missing"}]]
    out = format_rows(F.JSONL, [FormatColumn(name="v", category=MIX)], rows, RowFormatOptions(), SQ, "raw").text
    assert out == '{"v": "{\\"$type\\": \\"missing\\"}"}'


def test_raw_json_writer_keeps_row_text():  # Review Focus 1
    stored = '{"zeta": 1,\n "alpha": {"z": 1, "a": 2}, "ws" :  [ 1 , 2 ], "mid": "a b"}'
    rows = [stored, "1e400", '"x\\ny"', '{"a": 1, "b": "c d"}']
    out = StringIO()
    writer = open_raw_json_writer(F.JSONL, out)
    writer.write(rows)
    writer.close()
    assert out.getvalue().splitlines() == [
        '{"zeta":1,"alpha":{"z":1,"a":2},"ws":[1,2],"mid":"a b"}',  # had a line break: squeezed outside strings
        "1e400", '"x\\ny"', '{"a": 1, "b": "c d"}',  # one line already: exactly as sent
    ]
    out = StringIO()
    writer = open_raw_json_writer(F.JSON, out)
    writer.write(rows[:2])
    writer.close()
    assert out.getvalue() == "[\n  " + stored + ",\n  1e400\n]\n"  # a JSON array keeps every byte
    out = StringIO()
    open_raw_json_writer(F.JSON, out).close()
    assert out.getvalue() == "[]\n"
    out = StringIO()
    open_raw_json_writer(F.JSONL, out).close()
    assert out.getvalue() == ""
    with pytest.raises(ValueError):
        open_raw_json_writer(F.CSV, StringIO())


def test_raw_json_writer_writes_in_several_calls_and_survives_big_numbers():
    rows = ['{"n": 9007199254740993, "d": 1.10, "e": 1e400, "k": {"b": 1, "a": 2}}']
    out = StringIO()
    writer = open_raw_json_writer(F.JSON, out)
    writer.write(rows)
    writer.write([])
    writer.write(rows)
    writer.close()
    assert out.getvalue() == "[\n  " + rows[0] + ",\n  " + rows[0] + "\n]\n"
    assert json.loads(out.getvalue(), parse_float=Decimal)[1]["d"] == Decimal("1.10")


@pytest.mark.parametrize("text, expected", [
    ('{ "a" : [ 1 , 2 ] ,\n\t"b" :\r\n 1e400 }', '{"a":[1,2],"b":1e400}'),
    ('{"s": "a  b\\"  c\\\\", "t": " x "}', '{"s":"a  b\\"  c\\\\","t":" x "}'),  # spaces and escapes inside strings stay
    ('"\\u00e9 \\n"', '"\\u00e9 \\n"'),
    ("  1.10  ", "1.10"),
    ('{"中 文": "日 本"}', '{"中 文":"日 本"}'),
    ("", ""),
])
def test_one_line_squeezes_only_the_space_between_tokens(text, expected):
    assert one_line(text) == expected
