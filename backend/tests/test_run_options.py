import pytest

from dbaguy_backend.adapters.base import AdapterCapabilities, RunOptions, StatementError
from dbaguy_backend.domain.execution import ExecuteCommand, ExportCommand, QueryContext, StatementParams
from dbaguy_backend.sessions.options import run_options
from tests.test_session_worker import make_worker
from tests.test_sessions import finished

CB = AdapterCapabilities(query_context=True, parameters=True)
CONTEXT = QueryContext(bucket="dbaguy", scope="app")


def execute(**fields):
    return ExecuteCommand(**{"request_id": "r", "sql": "SELECT 1", **fields})


def export(**fields):
    return ExportCommand(**{"request_id": "e", "sql": "SELECT 1", "format": "csv", "path": "/tmp/x.csv", **fields})


def test_plain_requests_take_no_options():
    assert run_options(execute(), AdapterCapabilities()) == RunOptions()
    assert run_options(execute(params=StatementParams()), AdapterCapabilities()) == RunOptions()  # empty = none


@pytest.mark.parametrize("fields, words", [
    ({"query_context": CONTEXT}, "query context"),
    ({"params": StatementParams(named={"a": "1"})}, "parameters"),
    ({"profile_plan": True}, "executed plan"),
])
def test_options_the_engine_does_not_declare_are_refused(fields, words):
    with pytest.raises(StatementError, match=words) as error:
        run_options(execute(**fields), AdapterCapabilities())
    assert error.value.code == "unsupported_option"


@pytest.mark.parametrize("params, label, value", [
    (StatementParams(named={"1x": "1"}), "$1x", None),
    (StatementParams(named={"n": "abc"}), "$n", "abc"),
    (StatementParams(named={"n": "NaN"}), "$n", "NaN"),
    (StatementParams(named={"n": '"\ud800"'}), "$n", None),
    (StatementParams(positional=["1", "[secret"]), "$2", "secret"),
    (StatementParams(named={"n": '"' + "x" * (1024 * 1024) + '"'}), "$n", "xxxx"),
])
def test_invalid_params_name_the_parameter_never_the_value(params, label, value):
    with pytest.raises(StatementError) as error:
        run_options(execute(params=params), CB)
    assert error.value.code == "invalid_params" and label in error.value.message
    assert value is None or value not in error.value.message


def test_valid_params_pass_unchanged():
    params = StatementParams(
        named={"n": "9007199254740993", "big": "1" * 308, "s": '"中文"', "huge": "1.7976931348623157e308",
               "tiny": "-1.5E-99999999999999999999999999", "text": '"1e400"'},  # all within a double, or text
        positional=['[1, {"a": null}]', " true "],
    )
    assert run_options(execute(params=params, query_context=CONTEXT), CB) == RunOptions(query_context=CONTEXT, params=params)


@pytest.mark.parametrize("command", [execute, export])
@pytest.mark.parametrize("params, label", [
    (StatementParams(named={"n": "1e400"}), "$n"),
    (StatementParams(named={"big": "1" * 5000}), "$big"),
    (StatementParams(named={"huge": "-1e99999999999999999999999999"}), "$huge"),
    (StatementParams(positional=["1", '[0, {"a": 1.7976931348623159e308}]']), "$2"),  # inside an array and an object
])
def test_a_number_beyond_a_double_is_refused_by_name(command, params, label):  # B46: the server's 1040 quotes it
    with pytest.raises(StatementError) as error:
        run_options(command(params=params), CB)
    assert error.value.code == "invalid_params"
    text = f"Parameter {label}: a number in it is too large for the query service (beyond a double)"
    assert error.value.message == text
    assert not any(digits in error.value.message for digits in ("1e400", "1111", "e999", "e308"))


@pytest.mark.parametrize("kind", ["execute", "export"])
def test_a_number_beyond_a_double_is_never_sent(kind, tmp_path):  # B46, through the worker
    worker, conn, rec = make_worker(parameters=True)
    target = tmp_path / "out.csv"
    try:
        params = StatementParams(named={"u": "1e400"})
        command = (execute(params=params, sql="rows 1") if kind == "execute"
                   else export(params=params, sql="rows 1", path=str(target)))
        worker.submit(command)
        rec.wait_for(finished(command.request_id))
        [error] = rec.of(command.request_id, "error")
        assert error.code == "invalid_params" and error.message.startswith("Parameter $u:")
        assert "1e400" not in error.message and conn.runs == [] and not target.exists()
    finally:
        worker.close()


def test_an_export_takes_the_same_context_and_params():
    assert run_options(export(query_context=CONTEXT), CB).query_context == CONTEXT


def test_parameter_edges():
    exact = '"' + "x" * (1024 * 1024 - 2) + '"'  # exactly 1 MiB
    assert run_options(execute(params=StatementParams(named={"n": exact})), CB).params is not None
    for literal in ("[" * 100_000 + "]" * 100_000, "", "1 2", "'text'", "Infinity", "-Infinity"):
        with pytest.raises(StatementError) as error:
            run_options(execute(params=StatementParams(positional=[literal])), CB)
        assert error.value.code == "invalid_params"
    with pytest.raises(StatementError, match="parameter name") as error:  # a name the server could not read as $name
        run_options(execute(params=StatementParams(named={"a-b": "1"})), CB)
    assert error.value.code == "invalid_params"


def test_an_unsupported_option_is_named_before_bad_parameters():
    params = StatementParams(positional=["not json"])
    with pytest.raises(StatementError) as error:
        run_options(execute(params=params), AdapterCapabilities(query_context=True))
    assert error.value.code == "unsupported_option"
