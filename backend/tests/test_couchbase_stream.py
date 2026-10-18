import json
import random
import time

import pytest

from dbaguy_backend.adapters.couchbase.stream import (
    EndEvent, FieldEvent, ProtocolError, ResponseParser, ResultsEnd, ResultsStart, RowEvent, RowField, json_kind,
    split_object,
)
from tests.support import scaled

FULL = (
    '{"requestID":"r1","clientContextID":"dbaguy-x","signature":{"id":"json","*":"*"},"results":[\n'
    '{"id":"e1","d":{"name":"中文😀","n":9007199254740993,"pi":3.141592653589793238462643383279,"tags":["a"]}},\n'
    '{"id":"e2","d":{"name":"b","x":[1,{"y":1e400}]}},\n'
    '{"id":"e3","d":"just a string"}\n'
    '],"errors":[{"code":1080,"msg":"Timeout 3s exceeded","retry":true}],"status":"fatal",'
    '"metrics":{"elapsedTime":"3.1s","resultCount":3},"unknownField":{"a":[1,2]}}\n'
).encode()
RAW = (b'{"signature":"json","results":[1e400, -0.0, 12345678901234567890, "x", null, true, [1, 2], {"a": {}}],'
       b'"status":"success"}')
EMPTY = b'{"requestID":"r2","signature":{"$1":"number"},"results":[],"status":"success","metrics":{"resultCount":0}}'
NO_RESULTS = (b'{"requestID":"r3","errors":[{"code":3000,"msg":"syntax error - line 1, column 8","line":1,"column":8}],'
              b'"status":"fatal"}')
# Escapes cut anywhere (`\u` + 4 hex digits, a surrogate pair, `\"` and `\\`), strings holding the characters that end a
# value elsewhere, numbers whose next character could go on (`1E+3`, `-0`, `1.10`), empty containers.
ESCAPES = (
    rb'{"signature":{"*":"*"},"results":[{"k\"ey":"a, b] c} \"q\" \\ \u00e9 \ud83d\ude00 \/","n":[-0,1.10,1E+3,'
    rb'-1.5e-7,0]},{"":{},"e":[],"t":true,"f":false,"z":null},"tail\\"],"status":"success"}'
)
# What `pretty=true` would send: whitespace between every token.
PRETTY = (
    b'{\n  "signature" : {\n    "a" : "json"\n  } ,\n  "results" : [\n    {\n      "a" : 1.5 ,\n      "b" : [ 1 , 2 ]\n'
    b'    } ,\n    7 \n  ] ,\n  "status" : "success"\n}\n'
)


def parse(data: bytes, cuts: list[int]) -> list:
    parser, events, last = ResponseParser(), [], 0
    for cut in cuts:
        events += parser.feed(data[last:cut])
        last = cut
    events += parser.feed(data[last:])
    return events + parser.finish()


def parse_until_failure(data: bytes, cuts: list[int]) -> tuple[list, ProtocolError]:
    """The events that came out before the parser failed, and the failure (every later call raises it again)."""
    parser, events, last = ResponseParser(), [], 0
    try:
        for cut in [*cuts, len(data)]:
            events += parser.feed(data[last:cut])
            last = cut
        events += parser.finish()
    except ProtocolError as error:
        for later in (lambda: parser.feed(b"{}"), parser.finish):
            with pytest.raises(ProtocolError) as again:
                later()
            assert again.value is error
        return events, error
    with pytest.raises(ProtocolError) as error:  # a failure met by the last call that also returned events
        parser.finish()
    return events, error.value


@pytest.mark.parametrize("data", [FULL, RAW, EMPTY, NO_RESULTS, ESCAPES, PRETTY],
                         ids=["full", "raw", "empty", "no-results", "escapes", "pretty"])
def test_every_split_position_gives_the_same_events(data):
    whole = parse(data, [])
    for cut in range(len(data) + 1):  # inside multi-byte UTF-8, inside numbers, between a value and its comma …
        assert parse(data, [cut]) == whole, cut


def test_one_byte_at_a_time():
    assert parse(FULL, list(range(1, len(FULL)))) == parse(FULL, [])
    assert parse(ESCAPES, list(range(1, len(ESCAPES)))) == parse(ESCAPES, [])


def test_events_of_a_full_response():
    events = parse(FULL, [])
    assert events[:4] == [
        FieldEvent("requestID", "r1"), FieldEvent("clientContextID", "dbaguy-x"),
        FieldEvent("signature", {"id": "json", "*": "*"}), ResultsStart(),
    ]
    rows = [e.row for e in events if isinstance(e, RowEvent)]
    assert [r.fields[0].text for r in rows] == ['"e1"', '"e2"', '"e3"']
    assert rows[0].fields[1].text == (
        '{"name":"中文😀","n":9007199254740993,"pi":3.141592653589793238462643383279,"tags":["a"]}'
    )
    assert rows[0].text.startswith('{"id":"e1","d":{') and rows[0].value is None
    assert rows[2].fields[1].value == "just a string"
    assert events[-6:] == [
        ResultsEnd(), FieldEvent("errors", [{"code": 1080, "msg": "Timeout 3s exceeded", "retry": True}]),
        FieldEvent("status", "fatal"), FieldEvent("metrics", {"elapsedTime": "3.1s", "resultCount": 3}),
        FieldEvent("unknownField", {"a": [1, 2]}), EndEvent(),
    ]


def test_errors_after_rows_come_after_them():  # Review Focus 2
    events = parse(FULL, [])
    last_row = max(i for i, e in enumerate(events) if isinstance(e, RowEvent))
    errors = next(i for i, e in enumerate(events) if isinstance(e, FieldEvent) and e.name == "errors")
    assert errors > last_row


def test_scalar_rows_keep_their_text():
    rows = [e.row for e in parse(RAW, []) if isinstance(e, RowEvent)]
    assert [r.text for r in rows] == ["1e400", "-0.0", "12345678901234567890", '"x"', "null", "true", "[1, 2]", '{"a": {}}']
    assert (rows[3].value, rows[4].value, rows[5].value, rows[0].fields) == ("x", None, True, None)
    assert rows[7].fields == (RowField("a", "{}", {}),)


def test_numbers_in_rows_never_pass_through_a_python_number():  # R-04a-C13: the cell decision (Task 8) gets the text
    data = b'{"results":[-0, 1.10, 1E+3, 9007199254740993, -9007199254740993, {"a":-0,"b":1.10}],"status":"success"}'
    rows = [e.row for e in parse(data, []) if isinstance(e, RowEvent)]
    assert [(r.text, r.value) for r in rows[:5]] == [
        ("-0", "-0"), ("1.10", "1.10"), ("1E+3", "1E+3"), ("9007199254740993", "9007199254740993"),
        ("-9007199254740993", "-9007199254740993"),
    ]
    assert rows[5].fields == (RowField("a", "-0", "-0"), RowField("b", "1.10", "1.10"))


def test_escapes_and_odd_keys_keep_their_text():
    rows = [e.row for e in parse(ESCAPES, []) if isinstance(e, RowEvent)]
    first = rows[0].fields
    assert first[0].key == 'k"ey' and first[0].value == 'a, b] c} "q" \\ é 😀 /'
    assert first[0].text == r'"a, b] c} \"q\" \\ \u00e9 \ud83d\ude00 \/"'
    assert first[1].text == "[-0,1.10,1E+3,-1.5e-7,0]"
    assert [(f.key, f.text) for f in rows[1].fields] == [("", "{}"), ("e", "[]"), ("t", "true"), ("f", "false"),
                                                         ("z", "null")]
    assert (rows[2].text, rows[2].value) == ('"tail\\\\"', "tail\\")


@pytest.mark.parametrize("data, truncated", [
    (b"\xff", False), (b"<html>bad gateway</html>", False), (b'{"a":1} x', False), (b'{"results":[NaN]}', False),
    (FULL[:-10], True), (b"", True),
])
def test_malformed_or_cut_responses(data, truncated):
    with pytest.raises(ProtocolError) as error:
        parse(data, [])
    assert error.value.truncated == truncated


@pytest.mark.parametrize("data", [
    b'{"a":1,}', b'{,"a":1}', b'{"a":1 "b":2}', b'{"a":1,,"b":2}', b'{"results":[1 2]}', b'{"results":[,1]}',
    b'{"results":[1,]}', b'{"results":[1,,2]}', b'{"results":[{"a":1,}]}', b'{"results":[{"a" 1}]}',
    b'{"results":[tru]}', b'{"results":[1x]}', b'{"a":"b"c,"d":1}', b'{"a":Infinity}', b'["results"]',
    b'{"a":1}{"b":2}', b'{1:2}', b'{"a":1}\xe4\xb8',
])
def test_malformed_structure_is_reported(data):
    for cut in range(len(data) + 1):
        events, error = parse_until_failure(data, [cut])
        assert not error.truncated, cut


@pytest.mark.parametrize("data, rows", [
    (FULL[:FULL.index("中".encode()) + 1], 0),  # cut inside a multi-byte character
    (FULL[:FULL.index(b"e400") + 2], 1),  # cut inside a number (`1e4`)
    (ESCAPES[:ESCAPES.index(rb"\ude00") + 3], 0),  # cut inside a \u escape
    (FULL[:FULL.index(b"],\"errors\"")], 3),  # every row, but not the end of the array
    (FULL[:FULL.index(b'"status"') + 4], 3),  # inside a key
])
def test_a_cut_response_keeps_what_came_and_is_truncated(data, rows):
    for cut in range(len(data) + 1):
        events, error = parse_until_failure(data, [cut])
        assert error.truncated, cut
        assert len([e for e in events if isinstance(e, RowEvent)]) == rows, cut


@pytest.mark.parametrize("data", [
    b'{"results":[1,23,1e400,-0.0,12345678901234567890,7],"status":"success"}', RAW, FULL, ESCAPES,
], ids=["numbers", "raw", "full", "escapes"])
def test_a_body_cut_anywhere_never_shows_a_wrong_row(data):  # a cut inside `23` must not give a row `2`
    whole = [e.row for e in parse(data, []) if isinstance(e, RowEvent)]
    for cut in range(len(data.rstrip())):  # every cut before the object closes
        events, error = parse_until_failure(data[:cut], [])
        rows = [e.row for e in events if isinstance(e, RowEvent)]
        assert rows == whole[:len(rows)], cut
        assert error.truncated, cut


def test_long_whitespace_is_read_once():  # what `pretty=true` or a padding proxy could send
    run = b" " * (8 * 1024 * 1024)
    data = b'{"results":[1,' + run + b'2]' + run + b',"status":"success"}'
    parser, events = ResponseParser(), []
    started = time.monotonic()
    for i in range(0, len(data), 64 * 1024):
        events += parser.feed(data[i:i + 64 * 1024])
    events += parser.finish()
    assert time.monotonic() - started < scaled(0.5)
    assert [e.row.text for e in events if isinstance(e, RowEvent)] == ["1", "2"] and events[-1] == EndEvent()


@pytest.mark.parametrize("bad", [b"\xff", b"x", b"]]"])
def test_rows_before_a_bad_spot_are_not_lost(bad):
    data = b'{"results":[{"a":1},{"a":2},' + bad + b'{"a":3}],"status":"success"}'
    whole_rows = None
    for cut in range(len(data) + 1):
        events, error = parse_until_failure(data, [cut])
        assert not error.truncated, cut
        rows = [e.row.text for e in events if isinstance(e, RowEvent)]
        assert rows == ['{"a":1}', '{"a":2}'], cut
        whole_rows = rows
    assert whole_rows


def test_a_failure_after_events_is_raised_by_the_next_call():
    parser = ResponseParser()
    events = parser.feed(b'{"results":[1,2,x')
    assert [e.row.text for e in events if isinstance(e, RowEvent)] == ["1", "2"]
    with pytest.raises(ProtocolError) as error:
        parser.feed(b"]}")
    assert error.value.field == "results" and not error.value.truncated
    assert error.value.offset == len('{"results":[1,2,')


_NUMBERS = ["0", "-0", "7", "-0.0", "1.10", "1E+3", "1e400", "-1.5e-7", "9007199254740993", "12345678901234567890",
            "3.141592653589793238462643383279"]
_CHARACTERS = 'ab ,]}[{:"\\/\n\t\u0001éß中😀\u2028'


class _Writer:
    """Random JSON texts as a server might send them: number literals verbatim, random whitespace between tokens,
    strings escaped either way (`\\u` escapes with surrogate pairs, or raw UTF-8)."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)

    def space(self) -> str:
        return self.rng.choice(["", "", "", " ", "\n  ", "\t"])

    def string(self) -> str:
        text = "".join(self.rng.choice(_CHARACTERS) for _ in range(self.rng.randrange(6)))
        return json.dumps(text, ensure_ascii=self.rng.random() < 0.5)

    def value(self, depth: int = 0) -> str:
        kinds = ["number", "string", "literal", "array", "object"] if depth < 3 else ["number", "string"]
        kind = self.rng.choice(kinds)
        if kind == "number":
            return self.rng.choice(_NUMBERS)
        if kind == "string":
            return self.string()
        if kind == "literal":
            return self.rng.choice(["true", "false", "null"])
        if kind == "array":
            items = [self.value(depth + 1) for _ in range(self.rng.randrange(4))]
            return "[" + self.space() + ("," + self.space()).join(items) + self.space() + "]"
        return self.object(depth)[0]

    def object(self, depth: int = 0) -> tuple[str, list[tuple[str, str]]]:
        members = [(self.string(), self.value(depth + 1)) for _ in range(self.rng.randrange(4))]
        parts = [self.space() + key + self.space() + ":" + self.space() + text + self.space() for key, text in members]
        return "{" + ",".join(parts) + self.space() + "}", [(json.loads(key), text) for key, text in members]


def test_random_responses_keep_every_text_at_any_split():
    for seed in range(150):
        writer = _Writer(seed)
        rows = [writer.object() if writer.rng.random() < 0.7 else (writer.value(), None) for _ in range(6)]
        data = ('{"requestID":"r","signature":{"*":"*"},"results":[' + writer.space()
                + ("," + writer.space()).join(text for text, _ in rows) + writer.space()
                + '],"status":"success","metrics":{"resultCount":6}}').encode()
        whole = parse(data, [])
        got = [e.row for e in whole if isinstance(e, RowEvent)]
        assert [r.text for r in got] == [text for text, _ in rows], seed
        for row, (_, members) in zip(got, rows):
            if members is not None:
                assert [(f.key, f.text) for f in row.fields] == members, seed
            elif row.text[0] == '"':
                assert row.value == json.loads(row.text), seed
        assert whole[-1] == EndEvent() and FieldEvent("status", "success") in whole
        for _ in range(4):
            cuts = sorted(writer.rng.sample(range(len(data) + 1), 3))
            assert parse(data, cuts) == whole, (seed, cuts)
        size = writer.rng.randrange(1, 8)
        assert parse(data, list(range(size, len(data), size))) == whole, (seed, size)


def test_results_that_are_not_an_array_are_a_field():
    assert FieldEvent("results", None) in parse(b'{"results":null,"status":"success"}', [])


def test_a_big_row_is_parsed_a_few_times_not_once_per_chunk():
    data = ('{"results":[{"blob":"' + "x" * 2_000_000 + '"}],"status":"success"}').encode()
    parser, events = ResponseParser(), []
    for i in range(0, len(data), 1024):  # about 1,950 chunks
        events += parser.feed(data[i:i + 1024])
    events += parser.finish()
    assert len([e for e in events if isinstance(e, RowEvent)]) == 1
    assert parser.attempts < 40


def test_split_object_keeps_member_texts():
    fields = split_object('{ "b" : [ 1 , 2 ] , "a":{"x":1e400},"s":"q\\"","n":-1.10 }')
    assert [(f.key, f.text) for f in fields] == [("b", "[ 1 , 2 ]"), ("a", '{"x":1e400}'), ("s", '"q\\""'), ("n", "-1.10")]
    assert fields[2].value == 'q"'
    for bad in ("[1]", '{"a":1} x', '{"a":', "1"):
        with pytest.raises(ValueError):
            split_object(bad)


def test_json_kind():
    assert [json_kind(t) for t in ('"a"', "{}", "[]", "true", "false", "null", "-1", "1e400")] == [
        "string", "object", "array", "boolean", "boolean", "null", "number", "number",
    ]
