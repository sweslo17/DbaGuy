import pytest

from dbaguy_backend.adapters.couchbase.messages import metrics_message, statement_error, warning_messages
from dbaguy_backend.domain.execution import ErrorPosition, MessageSeverity, ServerMessage
from dbaguy_backend.sql.offsets import utf16_column


def test_error_text_code_and_utf16_position():
    sql = "SELECT '中文😀' AS x WHER"  # WHER is code point 19, UTF-16 unit 20 (survey §3.3)
    error = statement_error([{"code": 3000, "msg": "syntax error - at: WHER", "line": 1, "column": 19}], sql)
    assert (error.code, error.message, error.position) == ("3000", "syntax error - at: WHER", ErrorPosition(line=1, column=20))


def test_reason_missing_role_and_further_errors():
    errors = [
        {"code": 13014, "msg": "User does not have credentials to run SELECT queries",
         "reason": {"message": "No index available", "missing_role": "query_use_sequential_scans"}},
        {"code": 5000, "msg": "second"},
    ]
    error = statement_error(errors, "SELECT 1")
    assert error.message == (
        "User does not have credentials to run SELECT queries\nReason: No index available\n"
        "Missing role: query_use_sequential_scans\n5000: second"
    )
    assert (error.code, error.position) == ("13014", None)


def test_positions_on_later_lines_and_past_the_end():
    sql = "SELECT 1\n  FROM 😀x WHER"
    assert statement_error([{"code": 3000, "msg": "m", "line": 2, "column": 11}], sql).position == ErrorPosition(line=2, column=12)
    assert utf16_column("ab😀", 1, 6) == 7  # beyond the line: the extra columns count as they are
    assert utf16_column("a", 3, 4) == 4  # beyond the text: unchanged
    assert statement_error([{"code": 1, "msg": "m", "line": True, "column": 1}], "x").position is None


@pytest.mark.parametrize("sql, line, column, expected", [
    ("SELECT 1\rFROM 😀x WHER", 1, 18, 19),  # live (7.6.12): a lone \r is a character of line 1 (WHER: code point 18)
    ("SELECT 1\r\nFROM x WHER", 2, 6, 6),  # \r\n: the \r ends line 1, the position is on line 2
    ("SELECT 1\u2028FROM 😀x WHER", 1, 18, 19),  # U+2028 and form feed are characters too
    ("😀😀😀 x", 1, 5, 8),
    ("a\n😀b", 2, 2, 3),
])
def test_lines_end_at_newline_only(sql, line, column, expected):
    assert utf16_column(sql, line, column) == expected


@pytest.mark.parametrize("error", [
    {"code": 1, "msg": "m", "line": 0, "column": 1}, {"code": 1, "msg": "m", "line": 1, "column": 0},
    {"code": 1, "msg": "m", "line": "1", "column": 1}, {"code": 1, "msg": "m", "line": 1}, {"code": 1, "msg": "m"},
])
def test_no_position_without_a_usable_line_and_column(error):
    assert statement_error([error], "SELECT 1").position is None


def test_odd_errors_still_say_something():
    assert statement_error([], "x").message == "The query failed" and statement_error([], "x").code is None
    assert statement_error(["plain text"], "x").message == "plain text"
    assert statement_error([{"code": 1}], "x").message == "The query failed" and statement_error([{"code": 1}], "x").code == "1"
    error = statement_error([{"code": 1, "msg": "m", "reason": "because"}, "text", {"msg": "no code"}], "x")
    assert error.message == "m\nReason: because\ntext\nno code"


def test_warnings_and_metrics():
    assert warning_messages([{"code": 4038, "msg": "a warning"}]) == [
        ServerMessage(severity=MessageSeverity.WARNING, text="4038: a warning"),
    ]
    message = metrics_message({"elapsedTime": "12.96s", "executionTime": "12.95s", "resultCount": 1000,
                               "resultSize": 2202010, "mutationCount": 3, "warningCount": 1})
    assert message == ServerMessage(
        severity=MessageSeverity.INFO, text="Elapsed 12.96s · execution 12.95s · 1,000 results · 2.1 MB · 3 mutations · 1 warning",
    )
    assert metrics_message({"resultCount": 1}).text == "1 result"
    assert metrics_message({}) is None


@pytest.mark.parametrize("size, text", [(0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"), (1536, "1.5 KB"),
                                        (5 * 1024**3, "5.0 GB"), (3 * 1024**4, "3.0 TB"), (2048 * 1024**4, "2048.0 TB")])
def test_sizes(size, text):
    assert metrics_message({"resultSize": size}).text == text


def test_metrics_skip_what_is_not_a_number_or_a_duration():
    message = metrics_message({"elapsedTime": 5, "executionTime": "", "resultCount": True, "resultSize": "2", "mutationCount": 0})
    assert message.text == "0 mutations"
    assert metrics_message({"elapsedTime": None, "resultCount": 1.5}) is None


def test_warnings_without_a_code_or_an_object():
    assert [m.text for m in warning_messages([{"msg": "only text"}, "plain", {"code": 1, "msg": ""}])] == [
        "only text", "plain", "1: ",
    ]
