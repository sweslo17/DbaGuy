import io
import json
from datetime import date
from decimal import Decimal

import pytest

from dbaguy_backend.domain.errors import ValidationFailed
from dbaguy_backend.domain.execution import ColumnCategory as C
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.formatting import FormatColumn, FormatKind as F, RowFormatOptions, format_rows, open_writer

PG, LITE = Dialect.POSTGRES, Dialect.SQLITE
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
