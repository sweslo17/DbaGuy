"""Run with profile (R-04-13, A16): the executed plan arrives after the rows as a `plan` result set; plan cells are
sent whole up to PLAN_TEXT_LIMIT."""

import json

import pytest

from dbaguy_backend.adapters.base import ReadAheadState, RunOptions, StatementError
from dbaguy_backend.adapters.couchbase.result import NO_PROFILE, EventStream, row_cost, start_result
from dbaguy_backend.adapters.couchbase.stream import FieldEvent, ResponseParser
from dbaguy_backend.adapters.values import PLAN_TEXT_LIMIT, TEXT_LIMIT, JsonText, encode_row
from dbaguy_backend.domain.column_category import ColumnCategory
from dbaguy_backend.domain.execution import (
    ColumnMeta, ExecuteCommand, FetchMoreCommand, MessageSeverity, StatementParams,
)
from tests.couchbase_fakes import FakeResponse, Reply
from tests.fake_db import FakeConnection, FakeResult
from tests.fake_ssh import wait_until
from tests.support import requires_couchbase
from tests.test_couchbase_adapter import STREAM
from tests.test_couchbase_read_ahead import chunked
from tests.test_couchbase_result import OK_ONE, fake_connection, sent
from tests.test_couchbase_sessions import APP, cb_worker, page_through  # noqa: F401  (fixture)
from tests.test_session_worker import execute, make_worker
from tests.test_sessions import finished, paused

PROFILE = '{"phaseTimes":{"run":"1.10ms"},"executionTimings":{"#operator":"Sequence","~children":[]}}'
ORDERS_TOP3 = "SELECT o.amount FROM orders AS o WHERE o.amount > 400 ORDER BY o.amount DESC LIMIT 3"


def profiled(body: bytes, profile: bool = True):
    stream = EventStream(FakeResponse([body]), cancelled=lambda: False, node="n:8093")
    return start_result(stream, "SELECT a FROM ks", plan=False, bare_star=False, dml=False, on_done=lambda: None,
                        profile=profile)


def test_field_events_carry_their_exact_text():
    events = ResponseParser().feed(b'{"metrics":{"a": 1.10 },"status":"success"}')
    assert [(e.name, e.text) for e in events if isinstance(e, FieldEvent)][0] == ("metrics", '{"a": 1.10 }')


def test_the_profile_becomes_a_plan_result_set():
    body = ('{"signature":{"a":"json"},"results":[{"a":1}],"status":"success","metrics":{"resultCount":1},'
            f'"profile":{PROFILE}}}').encode()
    result = profiled(body)
    result.columns()
    assert result.fetch(10) == [[1]] and result.next_result() is True
    assert result.columns() == [ColumnMeta(name="plan", type_name="json", category=ColumnCategory.MIXED)]
    assert result.presentation() == "plan"
    assert result.fetch(10) == [[JsonText(PROFILE)]] and result.fetch(10) == []
    assert result.next_result() is False


def test_no_profile_in_the_answer_is_said_once():
    result = profiled(b'{"signature":{"a":"json"},"results":[{"a":1}],"status":"success"}')
    result.columns()
    result.fetch(10)
    assert result.next_result() is False
    assert [m.text for m in result.take_messages()].count(
        "The server sent no execution profile (Community Edition does not give one)") == 1


def test_a_plan_cell_is_sent_whole_up_to_its_limit():
    big = JsonText('{"t":"' + "x" * 100_000 + '"}')
    assert encode_row([big], PLAN_TEXT_LIMIT) == [{"$type": "json", "text": big.text}]
    assert encode_row([big])[0]["truncated"] is True  # any other set keeps the 64 KiB preview

    class BigPlan(FakeResult):
        def columns(self):
            return [ColumnMeta(name="plan", type_name="json", category=ColumnCategory.MIXED)]

        def fetch(self, n):
            rows, self._rows = ([[big]] if self._rows else []), []
            return rows

        def presentation(self):
            return "plan"

    class BigPlanConnection(FakeConnection):
        def statement(self, sql, options):
            super().statement(sql, options)
            return BigPlan(1)

    worker, _, rec = make_worker(BigPlanConnection())
    try:
        execute(worker, "x", "anything")
        rec.wait_for(finished("x"))
        assert rec.of("x", "rows")[0].rows == [[{"$type": "json", "text": big.text}]]
    finally:
        worker.close()


@requires_couchbase
def test_run_with_profile_adds_a_plan_result_set_after_the_rows(cb_worker):  # A16
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="p", sql=ORDERS_TOP3, query_context=APP, profile_plan=True))
    rec.wait_for(finished("p"), timeout=30)
    sets = rec.of("p", "result_set")
    assert [(s.index, s.presentation, [c.name for c in s.columns]) for s in sets] == [
        (0, "grid", ["amount"]), (1, "plan", ["plan"]),
    ]
    profile = json.loads(rec.of("p", "rows")[-1].rows[0][0]["text"])
    assert profile["executionTimings"]["#operator"] == "Authorize" and "phaseTimes" in profile


@requires_couchbase
def test_a_profile_follows_the_last_page(cb_worker):
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="e", sql="SELECT META(d).id FROM events AS d", query_context=APP,
                                 page_size=5000, profile_plan=True))
    assert page_through(worker, rec, "e") == 20_001  # 20,000 rows and the profile's one
    assert [s.presentation for s in rec.of("e", "result_set")] == ["grid", "plan"]


@requires_couchbase
def test_a_result_closed_early_has_no_profile(cb_worker):
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="s", sql=STREAM, page_size=500, profile_plan=True))
    rec.wait_for(paused("s", 500), timeout=30)
    worker.cancel("s")  # a discard
    rec.wait_for(finished("s"), timeout=10)
    assert [s.presentation for s in rec.of("s", "result_set")] == ["grid"]


# ---- beyond the brief: the profile across the read-ahead, errors, the request, and the plan text limit ----

PLAN_COLUMNS = [ColumnMeta(name="plan", type_name="json", category=ColumnCategory.MIXED)]
# About 1 MB, written as a server would not (spaces, `1.10`, a u64): the set must show this text, not a re-encoding
BIG_PROFILE = ('{ "phaseTimes" : {"run": "1.10ms"} , "n": 1.10, "u": 18446744073709551615, "pad": "'
               + "x" * 1_000_000 + '" }')


def numbers_with_profile(count: int, profile: str = BIG_PROFILE, status: str = '"status":"success"') -> bytes:
    """`count` rows, then `status` (and what goes with it), the metrics and the profile, as the server orders them."""
    rows = ",".join(f'{{"n":{i}}}' for i in range(count))
    return (f'{{"signature":{{"n":"number"}},"results":[{rows}],{status},"metrics":{{"resultCount":{count}}},'
            f'"profile":{profile}}}').encode()


def profiled_in(chunks: list[bytes], profile: bool = True):
    stream = EventStream(FakeResponse(chunks), cancelled=lambda: False, node="n:8093")
    return start_result(stream, "SELECT n FROM ks", plan=False, bare_star=False, dml=False, on_done=lambda: None,
                        profile=profile)


class SetsResult(FakeResult):
    """Result sets read in order, each `(presentation, rows)`."""

    def __init__(self, *sets):
        super().__init__(0)
        self.sets = list(sets)

    def columns(self):
        return PLAN_COLUMNS

    def presentation(self):
        return self.sets[0][0]

    def fetch(self, n):
        presentation, rows = self.sets[0]
        self.sets[0] = presentation, rows[n:]
        return rows[:n]

    def next_result(self):
        self.sets.pop(0)
        return bool(self.sets)


def sets_worker(*sets):
    """A worker whose every statement gives `SetsResult(*sets)`."""

    class Connection(FakeConnection):
        def statement(self, sql, options):
            super().statement(sql, options)
            return SetsResult(*sets)

    return make_worker(Connection())


def ends(rec, rid):
    return [(e.index, e.row_count, e.has_more) for e in rec.of(rid, "result_set_end")]


def step_until_settled(result, budget: int) -> list[ReadAheadState]:
    states = [result.read_ahead(budget).state]
    while states[-1] is ReadAheadState.READING:
        states.append(result.read_ahead(budget).state)
    return states


@pytest.mark.parametrize("budget_left", [10**12, 0], ids=["roomy", "the-last-row-fills-it"])
def test_a_profile_read_ahead_to_the_end_keeps_its_text_and_takes_no_budget(budget_left):
    result = profiled_in(chunked(numbers_with_profile(50), 64 * 1024))
    result.columns()
    assert [row[0] for row in result.fetch(10)] == list(range(10))
    rows_cost = sum(row_cost(f'{{"n":{i}}}') for i in range(10, 50))
    states = step_until_settled(result, rows_cost + budget_left)  # the profile alone is far past either budget
    assert states[-1] is ReadAheadState.DONE and ReadAheadState.FULL not in states and len(states) > 2
    assert result.held == rows_cost  # rows only
    assert [row[0] for row in result.fetch(100)] == list(range(10, 50))
    assert result.next_result() is True and result.presentation() == "plan" and result.columns() == PLAN_COLUMNS
    assert result.read_ahead(10**12).state is ReadAheadState.DONE
    assert result.fetch(10) == [[JsonText(BIG_PROFILE)]] and result.fetch(10) == []
    assert result.affected_rows() is None and result.next_result() is False


def test_a_profile_after_a_full_read_ahead_follows_the_loaded_rows():
    result = profiled_in(chunked(numbers_with_profile(5000), 4096))
    result.columns()
    assert step_until_settled(result, 50_000)[-1] is ReadAheadState.FULL
    assert [row[0] for row in result.fetch(10_000)] == list(range(5000))  # held, then the stream, then the end
    assert result.next_result() is True and result.columns() == PLAN_COLUMNS
    assert result.fetch_raw(10) == [BIG_PROFILE] and result.fetch_raw(10) == []
    assert result.next_result() is False
    assert NO_PROFILE not in [m.text for m in result.take_messages()]


def test_an_error_in_a_profiled_answer_comes_instead_of_the_profile():
    body = numbers_with_profile(3, '{"executionTimings":{}}',
                                status='"errors":[{"code":5010,"msg":"boom"}],"status":"fatal"')
    result = profiled_in([body])
    result.columns()
    assert [row[0] for row in result.fetch(10)] == [0, 1, 2]
    with pytest.raises(StatementError, match="boom"):
        result.next_result()
    assert result.next_result() is False and NO_PROFILE not in [m.text for m in result.take_messages()]


def test_a_profile_nobody_asked_for_is_not_shown():  # a node whose own `profile` setting is "timings"
    result = profiled_in([numbers_with_profile(2, PROFILE)], profile=False)
    result.columns()
    assert [row[0] for row in result.fetch(10)] == [0, 1]
    assert result.next_result() is False and NO_PROFILE not in [m.text for m in result.take_messages()]


def test_rows_skipped_by_next_result_never_reach_the_profile_set():  # next_result() drops rows nobody asked for
    result = profiled_in(chunked(numbers_with_profile(50, PROFILE), 256))
    result.columns()
    assert [row[0] for row in result.fetch(1)] == [0]
    assert step_until_settled(result, 10**12)[-1] is ReadAheadState.DONE and result.held > 0
    assert result.next_result() is True and result.held == 0
    assert result.fetch(100) == [[JsonText(PROFILE)]]


def test_a_profiled_dml_counts_its_mutations_then_shows_the_profile():
    body = f'{{"results":[],"status":"success","metrics":{{"mutationCount":3}},"profile":{PROFILE}}}'.encode()
    stream = EventStream(FakeResponse([body]), cancelled=lambda: False, node="n:8093")
    result = start_result(stream, "UPDATE ks SET a = 1", plan=False, bare_star=False, dml=True,
                          on_done=lambda: None, profile=True)
    assert result.columns() is None and result.affected_rows() == 3
    assert result.next_result() is True and result.columns() == PLAN_COLUMNS and result.affected_rows() is None
    assert result.fetch(10) == [[JsonText(PROFILE)]]


def test_a_closed_result_has_no_profile_set():
    result = profiled_in([numbers_with_profile(2, PROFILE)])
    result.columns()
    assert [row[0] for row in result.fetch(10)] == [0, 1]  # the answer has been read to its end, profile and all
    result.close()
    assert result.next_result() is False


def test_the_worker_passes_on_that_no_profile_came():  # the line is made before next_result(), which loses messages
    server, connection = fake_connection([OK_ONE])
    worker, _, rec = make_worker(connection, executed_plan=True)
    try:
        worker.submit(ExecuteCommand(request_id="n", sql="SELECT a FROM ks", profile_plan=True))
        rec.wait_for(finished("n"))
        assert [s.presentation for s in rec.of("n", "result_set")] == ["grid"]
        assert [m.text for m in rec.of("n", "message")].count(NO_PROFILE) == 1
    finally:
        worker.close()
        server.close()


def test_a_run_with_profile_asks_for_timings_and_warns_about_a_parameter_first():
    answer = ('{"signature":{"u":"json"},"results":[{"u":18446744073709552000}],"status":"success",'
              f'"metrics":{{"resultCount":1}},"profile":{PROFILE}}}').encode()
    server, connection = fake_connection([Reply(answer), OK_ONE])
    try:
        params = StatementParams(named={"u": "18446744073709551615"})
        result = connection.run("SELECT $u AS u", RunOptions(params=params, profile_plan=True))
        messages = result.take_messages()
        assert [m.severity for m in messages] == [MessageSeverity.WARNING]
        assert messages[0].text.startswith("Parameter $u:") and "1844674" not in messages[0].text
        assert result.fetch(10) == [[JsonText("18446744073709552000")]] and result.next_result() is True
        assert result.fetch(10) == [[JsonText(PROFILE)]]
        result.close()
        connection.run("SELECT 1").close()
        first, second = sent(server)
        assert first["profile"] == "timings" and "profile" not in second
    finally:
        connection.close()
        server.close()


def test_the_plan_text_limit_is_the_longest_text_sent_whole():
    assert (PLAN_TEXT_LIMIT, TEXT_LIMIT) == (4 * 1024 * 1024, 64 * 1024)  # the binding values
    at, over = "x" * PLAN_TEXT_LIMIT, "x" * (PLAN_TEXT_LIMIT + 1)
    assert encode_row([JsonText(at), at], PLAN_TEXT_LIMIT) == [{"$type": "json", "text": at}, at]
    cut = encode_row([JsonText(over)], PLAN_TEXT_LIMIT)[0]
    assert (cut["size"], len(cut["preview"]), cut["truncated"]) == (PLAN_TEXT_LIMIT + 1, PLAN_TEXT_LIMIT, True)
    assert len(encode_row([over])[0]["preview"]) == TEXT_LIMIT  # the default is unchanged


def test_a_plan_cell_stays_whole_after_a_load_more_and_the_next_set_is_cut_again():  # B37
    big = JsonText('"' + "x" * 100_000 + '"')
    worker, _, rec = sets_worker(("plan", [[big], [big]]), ("grid", [[big]]))
    try:
        execute(worker, "x", "anything", page_size=1)
        rec.wait_for(paused("x", 1))
        worker.submit(FetchMoreCommand(request_id="x", count=10))  # re-enters _pump with the columns already sent
        rec.wait_for(finished("x"))
        cells = [(event.index, row[0]) for event in rec.of("x", "rows") for row in event.rows]
        assert cells[:2] == [(0, {"$type": "json", "text": big.text})] * 2
        assert cells[2][0] == 1 and cells[2][1]["truncated"] is True and len(cells) == 3
    finally:
        worker.close()


@requires_couchbase
def test_a_profile_follows_rows_loaded_after_a_full_read_ahead(cb_worker):
    worker, rec = cb_worker
    sql = "SELECT RAW a * 1000 + b FROM ARRAY_RANGE(0, 200) AS a UNNEST ARRAY_RANGE(0, 1000) AS b"  # past 8 MiB held
    worker.submit(ExecuteCommand(request_id="f", sql=sql, page_size=500, profile_plan=True))
    rec.wait_for(paused("f", 500), timeout=30)
    wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.FULL, 30)
    assert page_through(worker, rec, "f", count=10_000) == 200_001
    assert [s.presentation for s in rec.of("f", "result_set")] == ["grid", "plan"]
    values = [row[0] for event in rec.of("f", "rows") if event.index == 0 for row in event.rows]
    assert values == list(range(200_000))
    profile = json.loads(rec.of("f", "rows")[-1].rows[0][0]["text"])
    assert profile["executionTimings"]["#operator"] == "Authorize" and "phaseTimes" in profile


@pytest.mark.parametrize("second, expected_ends", [
    (("plan", [[JsonText("{}")]]), [(0, 2, False), (1, 1, False)]),  # the profile's one row, in the same page
    (("plan", [[JsonText("{}")]] * 2), [(0, 2, False), (1, 1, True)]),  # its first row, then the page pauses
    (("grid", [[JsonText("{}")]]), [(0, 2, False), (1, 0, True)]),  # any other set pages as before
], ids=["plan", "plan-of-two-rows", "grid"])
def test_a_plan_set_after_a_full_page_comes_with_its_first_row(second, expected_ends):  # B45
    worker, _, rec = sets_worker(("grid", [[1], [2]]), second)
    try:
        execute(worker, "x", "anything", page_size=2)
        rec.wait_for(lambda e: e.type == "result_set_end" and e.request_id == "x" and e.index == 1)
        assert ends(rec, "x") == expected_ends
        second_rows = [row for event in rec.of("x", "rows") if event.index == 1 for row in event.rows]
        assert second_rows == ([[{"$type": "json", "text": "{}"}]] if second[0] == "plan" else [])
    finally:
        worker.close()


@requires_couchbase
def test_a_profile_after_rows_that_fill_the_page_comes_with_its_row(cb_worker):  # B45: LIMIT 500 at page size 500
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="x", sql="SELECT RAW a FROM ARRAY_RANGE(0, 500) AS a", page_size=500,
                                 profile_plan=True))
    rec.wait_for(finished("x"), timeout=30)
    assert ends(rec, "x") == [(0, 500, False), (1, 1, False)]
    profile = json.loads(rec.of("x", "rows")[-1].rows[0][0]["text"])
    assert profile["executionTimings"]["#operator"] == "Authorize"
