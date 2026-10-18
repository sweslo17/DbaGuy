"""Parameters the query service would read as a double are flagged by name (R-04-10, spec §3.5.4), and typed literals
reach the server exactly (spec §1)."""

import pytest

from dbaguy_backend.adapters.couchbase.params import changes_as_double, parameter_warnings
from dbaguy_backend.domain.execution import ExecuteCommand, MessageSeverity, StatementParams
from tests.support import requires_couchbase
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
])
def test_changes_as_double_at_its_edges(literal, changes):
    assert changes_as_double(literal) is changes


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
