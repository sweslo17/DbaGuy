"""Parameters the query service would read as a double are flagged by name (R-04-10, spec §3.5.4), and typed literals
reach the server exactly (spec §1)."""

from decimal import Decimal

import pytest

from dbaguy_backend.adapters.base import RunOptions
from dbaguy_backend.adapters.couchbase.params import changes_as_double, parameter_warnings
from dbaguy_backend.domain.execution import ExecuteCommand, MessageSeverity, StatementParams
from tests.support import requires_couchbase
from tests.test_couchbase_adapter import connection  # noqa: F401  (fixture)
from tests.test_couchbase_sessions import cb_worker  # noqa: F401  (fixture)
from tests.test_sessions import finished


@pytest.mark.parametrize("literal, changes", [
    ("9007199254740993", False), ("9223372036854775807", False), ("-9223372036854775808", False), ("-0", False),
    ("9223372036854775808", True), ("18446744073709551615", True), ("1" + "0" * 5000, True),
    ("1.10", False), ("0.1", False), ("1E+3", False), ("-0.0", False),
    ("3.141592653589793238462643383279", True), ("1e400", True), ("1e-400", True),
    ('"18446744073709551615"', False), ('[1, {"a": 18446744073709551615}]', True), ("true", False), ("null", False),
])
def test_changes_as_double(literal, changes):
    assert changes_as_double(literal) is changes


@pytest.mark.parametrize("literal, changes", [
    ("-9223372036854775809", True),  # one below int64
    ("9007199254740993.0", True),  # an integer value, but written as a decimal: a double, which rounds it
    ("1e99999999999999999999999999", True), ("-1.5E-99999999999999999999999999", True),  # past Decimal's exponents
    ("0e-99999999999999999999999999", False),  # zero, whatever its exponent
    (" [0.5, -2.25e3, 7] ", False), ('{"a": "1e400"}', False),
    ("NaN", False), ("[1,", False),  # not JSON: check_params refused them before
    # the 04b Task 4 review, live on 7.6.12: a whole double within ±2^63 is shown as its exact int64 (F1)
    ("1.2345678901234568e18", True), ("4.611686018427388e18", True),  # 1234567890123456768, 4611686018427387904
    ("9.223372036854776e18", True), ("-9.223372036854776e18", True),  # 2^63 overflows; -2^63 shows its digits
    ("1234567890123456768.0", False), ("4611686018427387904.0", False),  # whole doubles, written exactly
    ("10000000000000000000", False), ("100000000000000000000", False), ("1" + "0" * 30, False),  # past int64, exact
    ("9223372036854775807.0", True),  # 2^63 as a double: the int64 it becomes depends on the platform, flagged
])
def test_changes_as_double_at_its_edges(literal, changes):
    assert changes_as_double(literal) is changes


# The review's 43-literal comparison with the server, less those beyond a double (refused before they are sent, B46)
# and 2^63 written as 9223372036854775807.0 (platform-dependent, flagged on purpose)
SERVER_CASES = [
    "9223372036854775807", "-9223372036854775807", "9223372036854775808", "-9223372036854775808",
    "-9223372036854775809", "18446744073709551615", "9007199254740993", "9007199254740993.0", "9007199254740993e0",
    "123456789012345678e0", "1.10", "0.1", "1e3", "1E+3", "1e20", "1e23", "3.141592653589793238462643383279",
    "1e-400", "-1e-400", "-0", "-0.0", "0.1000000000000000055511151231257827", "5e-324", "2e-324", "4.9e-324",
    "100000000000000000000", "1" + "0" * 30, "9.223372036854776e18", "9223372036854776000", "9223372036854776833",
    "-9223372036854776000", "-9.223372036854776e18", "10000000000000000000", "123456789012345678901234567890",
    "1.2345678901234568e18", "1234567890123456768.0", "4.611686018427388e18", "4611686018427387904.0",
    "1.8446744073709552e19", "0.5", "2.5e-3",
]


@requires_couchbase
def test_the_warning_rule_agrees_with_the_server(connection):  # F1: a warning exactly when the value shown differs
    disagree = []
    for literal in SERVER_CASES:
        result = connection.run("SELECT RAW $p", RunOptions(params=StatementParams(named={"p": literal})))
        try:
            shown = result.fetch_raw(2)
        finally:
            result.close()
        changed = len(shown) != 1 or Decimal(shown[0]) != Decimal(literal)
        if changes_as_double(literal) is not changed:
            disagree.append((literal, shown))
    assert disagree == []


def test_warnings_name_the_parameter_and_never_its_value():  # Review Focus 4
    params = StatementParams(named={"big": "18446744073709551615", "ok": "1"},
                             positional=["3.141592653589793238462643383279", '"x"'])
    warnings = parameter_warnings(params)
    assert [w.severity for w in warnings] == [MessageSeverity.WARNING, MessageSeverity.WARNING]
    assert warnings[0].text.startswith("Parameter $big:") and warnings[1].text.startswith("Parameter $1:")
    assert not any("18446744073709551615" in w.text or "3.14159" in w.text for w in warnings)
    assert parameter_warnings(None) == []


@requires_couchbase
def test_parameters_reach_the_server_exactly(cb_worker):  # spec §1: $n = 9007199254740993 → 9007199254740994
    worker, rec = cb_worker
    params = StatementParams(named={"n": "9007199254740993"}, positional=['"first"', "[1, 2]"])
    worker.submit(ExecuteCommand(request_id="p", sql="SELECT $n + 1 AS a1, @n AS a2, ? AS a3, $2 AS a4",
                                 params=params))
    rec.wait_for(finished("p"), timeout=30)
    assert rec.of("p", "rows")[0].rows == [[
        {"$type": "json", "text": "9007199254740994"}, {"$type": "json", "text": "9007199254740993"}, "first",
        {"$type": "json", "text": "[1,2]"},
    ]]
    assert not [m for m in rec.of("p", "message") if m.severity == MessageSeverity.WARNING]


@requires_couchbase
def test_a_number_the_server_reads_as_a_double_is_flagged(cb_worker):
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="u", sql="SELECT $u AS u",
                                 params=StatementParams(named={"u": "18446744073709551615"})))
    rec.wait_for(finished("u"), timeout=30)
    assert rec.of("u", "rows")[0].rows == [[{"$type": "json", "text": "18446744073709552000"}]]  # A26
    warnings = [m.text for m in rec.of("u", "message") if m.severity == MessageSeverity.WARNING]
    assert len(warnings) == 1 and warnings[0].startswith("Parameter $u:") and "1844674" not in warnings[0]
