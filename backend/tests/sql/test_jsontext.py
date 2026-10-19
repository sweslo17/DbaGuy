import json
import tracemalloc

import pytest

from dbaguy_backend.domain.couchbase import ChangeCounts, DocumentChange
from dbaguy_backend.sql.jsontext import (
    CHANGE_TEXT_LIMIT, CHANGES_PER_KIND, KEYS_SHOWN, DocumentDiff, NotJson, diff, json_path, json_problem, number_paths,
    one_line, value_end, without_strings,
)


def test_value_end_finds_where_a_value_ends_without_building_it():
    text = '{"a":[1,{"b":"]} \\" ["}],"c":-0.0e+1} tail'
    assert value_end(text, 0) == text.index(" tail")
    assert value_end("  [1, 2]", 2) == 8
    assert (value_end('"x",', 0), value_end("12,", 0), value_end("null]", 0)) == (3, 2, 4)


@pytest.mark.parametrize("text, error", [
    ('{"a":', json.JSONDecodeError), ("[1,]", json.JSONDecodeError), ('{"a":tru}', json.JSONDecodeError),
    ("[NaN]", NotJson), ('{"a":-Infinity}', NotJson),
    ('["\\x"]', json.JSONDecodeError), ('{"a":["b\nc"]}', json.JSONDecodeError),  # a bad escape, a raw line break
])
def test_value_end_refuses_what_is_not_json(text, error):
    with pytest.raises(error):
        value_end(text, 0)


def test_value_end_refuses_nesting_deeper_than_python_allows():  # malformed for the response parser, never "cut short"
    with pytest.raises(RecursionError):
        value_end("[" * 100_000 + "]" * 100_000, 0)


@pytest.mark.parametrize("text, offset, reason", [
    ("", 0, "Expecting value"), ("{", 1, "Expecting property name"), ('{"a":1,}', 6, "Illegal trailing comma"),
    ('{"😀": 1,}', 7, "Illegal trailing comma"), ('{"a":"x\ty"}', 7, "Invalid control character"),
    ('{"a":NaN}', 5, "NaN and Infinity are not JSON"), ('{"a":"NaN","b":-Infinity}', 15, "NaN and Infinity"),
    ("[1] x", 4, "Extra data"), ("\ufeff{}", 0, "Expecting value"),
])
def test_json_problem_points_at_the_first_bad_spot(text, offset, reason):
    problem = json_problem(text)
    assert problem.offset == offset and problem.reason.startswith(reason)


def test_json_problem_accepts_one_value_with_space_around_it():
    assert json_problem('  {"a": [1, 2]}  \n') is None and json_problem('"s"') is None


def test_json_problem_names_too_deep_nesting_instead_of_raising():  # the C decoder passes it, the walkers could not
    deep = "[" * 100_000 + "]" * 100_000
    assert json_problem(deep).reason == "Nested too deeply"


def test_without_strings_keeps_numbers_and_structure():
    assert without_strings('{"a]":"x\\"y",  "b":[1,"2",1e400]}') == '{"":"",  "":[1,"",1e400]}'


def test_json_path_is_written_like_the_value_viewer():
    assert json_path(()) == "$"
    assert json_path(("a", 0, "b c", "$x", "中", "\ud800")) == '$.a[0]["b c"].$x["中"]["\\ud800"]'
    assert json_path(("a\n",)) == '$["a\\n"]'  # a plain key ends at the end of the key: `$` alone would let a newline pass


def test_number_paths_finds_numbers_by_their_literal():
    text = '{"a":1,"b":{"c":[1, 123456789012345678901,{"d":2}]},"odd key":1e400,"s":"1e400","n":[1e400]}'
    wanted = lambda literal: literal.endswith("e400") or len(literal) > 18  # noqa: E731
    assert number_paths(text, wanted, 50) == (["$.b.c[1]", '$["odd key"]', "$.n[0]"], 3)
    assert number_paths(text, wanted, 1) == (["$.b.c[1]"], 3)
    assert number_paths('"just a string"', wanted, 50) == ([], 0)


def test_number_paths_of_a_bare_number_and_of_empty_containers():
    assert number_paths(" 1e400 ", lambda literal: True, 5) == (["$"], 1)
    assert number_paths('{"a":{},"b":[],"c":[[],{}]}', lambda literal: True, 5) == ([], 0)


def test_number_paths_and_diff_refuse_nesting_deeper_than_python_allows():  # ruling C27: valid JSON, too deep to walk
    deep = '{"a":' * 1100 + "12345678901234567890" + "}" * 1100
    assert json_problem(deep) is None
    with pytest.raises(RecursionError):
        number_paths(deep, lambda literal: True, 5)
    with pytest.raises(RecursionError):
        diff(deep, deep.replace("12345678901234567890", "1"))


def change(path, kind, before, after, value_changed):
    return DocumentChange(path=path, kind=kind, before=before, after=after, value_changed=value_changed)


@pytest.mark.parametrize("edited, server, expected", [
    ('{"zeta": 1,\n "alpha": {"z": 1, "a": 2}, "ws" :  [ 1 , 2 ], "mid": "a b"}',
     '{"alpha":{"a":2,"z":1},"mid":"a b","ws":[1,2],"zeta":1}',
     [change("$", "key_order", '"zeta", "alpha", "ws", "mid"', '"alpha", "mid", "ws", "zeta"', False),
      change("$.alpha", "key_order", '"z", "a"', '"a", "z"', False)]),
    ('{"a":1,"b":3,"a":2}', '{"a":2,"b":3}', [change("$.a", "duplicate_key", "1", "2", True)]),
    ('{"x":-0,"y":0.0,"pi":3.141592653589793238462643383279,"t":1e-400}',
     '{"pi":3.141592653589793,"t":0,"x":0,"y":0}',
     [change("$.pi", "number", "3.141592653589793238462643383279", "3.141592653589793", True),
      change("$.t", "number", "1e-400", "0", True), change("$.x", "number", "-0", "0", False),
      change("$.y", "number", "0.0", "0", False),
      change("$", "key_order", '"x", "y", "pi", "t"', '"pi", "t", "x", "y"', False)]),
    ('[3,1,{"b":1,"a":2}]', '[3,1,{"a":2,"b":1}]', [change("$[2]", "key_order", '"b", "a"', '"a", "b"', False)]),
    ("42.50", "42.5", [change("$", "number", "42.50", "42.5", False)]),
    ('{"a":"\\ud800"}', '{"a":"\\ufffd"}', [change("$.a", "other", '"\\ud800"', '"\\ufffd"', True)]),
    ('{"a":1}', '{"a":"1"}', [change("$.a", "other", "1", '"1"', True)]),
    ('{"a":1}', '{"b":1}', [change("$", "other", '"a"', '"b"', True)]),
    ("[1,2]", "[1]", [change("$", "other", "2 elements", "1 element", True)]),
])
def test_diff_reports_each_kind_of_change(edited, server, expected):  # Review Focus 1
    assert diff(edited, server).changes == expected


@pytest.mark.parametrize("edited, server", [
    ('{\n  "a": [1, 2],\n  "b": {"c": null}\n}', '{"a":[1,2],"b":{"c":null}}'),  # whitespace only (spec §12)
    ('{"s":"\\u00e9 \\u003c \\/ \\ud83d\\ude00","t":"<&>"}', '{"s":"é < / 😀","t":"\\u003c\\u0026\\u003e"}'),  # A2
    ('"a string"', '"a string"'), ("[]", "[]"),
])
def test_whitespace_and_escape_spelling_are_not_changes(edited, server):
    assert diff(edited, server) == DocumentDiff(changes=[], counts=ChangeCounts())


def test_value_changes_are_listed_before_key_order():  # Review Focus 2: an SDK-written document, one precision loss
    edited = '{"items":[' + ",".join(f'{{"b":{i},"a":{i}}}' for i in range(300)) + '],"pi":3.141592653589793238462}'
    server = '{"items":[' + ",".join(f'{{"a":{i},"b":{i}}}' for i in range(300)) + '],"pi":3.141592653589793}'
    found = diff(edited, server)
    assert found.counts == ChangeCounts(key_order=300, number=1)
    assert (found.changes[0].path, found.changes[0].kind, found.changes[0].value_changed) == ("$.pi", "number", True)
    assert len(found.changes) == 1 + CHANGES_PER_KIND


def test_long_texts_and_key_lists_are_cut():
    keys = [f"k{i:02}" for i in range(12)]
    edited = "{" + ",".join(f'"{k}":1' for k in reversed(keys)) + ',"z":"' + "x" * 300 + '"}'
    server = '{"z":"' + "y" * 300 + '",' + ",".join(f'"{k}":1' for k in keys) + "}"
    other, order = diff(edited, server).changes
    assert other.before == '"' + "x" * 199 + "…" and len(other.after) == CHANGE_TEXT_LIMIT + 1
    assert order.before.endswith(", … (13 keys)") and order.before.count(", ") == KEYS_SHOWN


def test_a_key_list_of_huge_keys_stays_short_and_says_how_many_keys():
    edited = '{"' + "b" * 5000 + '":1,"' + "a" * 5000 + '":1}'
    server = '{"' + "a" * 5000 + '":1,"' + "b" * 5000 + '":1}'
    (order,) = diff(edited, server).changes
    assert order.kind == "key_order" and order.before.endswith(", … (2 keys)")
    assert len(order.before) < 2 * CHANGE_TEXT_LIMIT and len(order.after) < 2 * CHANGE_TEXT_LIMIT


def test_a_lone_surrogate_is_written_as_its_escape_so_every_answer_encodes():
    lone = "\ud800"  # a decoded key / a raw character in the text: neither may reach the answer as a surrogate
    edited = '{"' + lone + 'b":1,"' + lone + 'a":"' + lone + '"}'
    server = '{"' + lone + 'a":"x","' + lone + 'b":1}'
    found = diff(edited, server)
    assert [(c.path, c.kind) for c in found.changes] == [('$["\\ud800a"]', "other"), ("$", "key_order")]
    for item in found.changes:
        for text in (item.path, item.before, item.after):
            text.encode("utf-8")
    assert found.changes[0].before == '"\\ud800"' and found.changes[1].before == '"\\ud800b", "\\ud800a"'


def test_a_value_repeated_more_than_twice_is_reported_for_each_dropped_one():
    found = diff('{"a":1,"a":2,"a":3}', '{"a":3}')
    assert [(c.before, c.after) for c in found.changes] == [("1", "2"), ("2", "3")]
    assert found.counts == ChangeCounts(duplicate_key=2)


def test_arrays_of_different_lengths_are_one_change_and_the_common_part_is_still_compared():
    found = diff('[1.0,{"b":1,"a":1},3]', '[1,{"a":1,"b":1}]')
    assert [(c.path, c.kind, c.before, c.after) for c in found.changes] == [
        ("$", "other", "3 elements", "2 elements"), ("$[0]", "number", "1.0", "1"), ("$[1]", "key_order", '"b", "a"', '"a", "b"'),
    ]


def test_changes_of_another_type_or_of_a_constant_are_other():
    found = diff('{"a":[],"b":true,"c":null,"d":{"x":1}}', '{"a":{},"b":false,"c":0,"d":[1]}')
    assert [(c.path, c.before, c.after) for c in found.changes] == [
        ("$.a", "[]", "{}"), ("$.b", "true", "false"), ("$.c", "null", "0"), ("$.d", '{"x":1}', "[1]"),
    ]
    assert all(c.kind == "other" and c.value_changed for c in found.changes)


def test_numbers_that_decimal_cannot_read_are_value_changes():
    found = diff('{"a":1e999999999999999999999,"b":1E+3}', '{"a":0,"b":1000}')
    assert [(c.path, c.value_changed) for c in found.changes] == [("$.a", True), ("$.b", False)]


def test_a_difference_in_the_last_chunk_of_a_long_value_is_found():  # `_same_text` compares in chunks
    base = '{"k":"' + "x" * 200_000
    found = diff(base + 'a"}', base + 'b"}')
    assert [(c.path, c.kind) for c in found.changes] == [("$.k", "other")]


def test_diff_of_a_big_document_never_builds_a_tree():  # ruling C3; a tree of this text takes about 12×
    edited = '{"items":[' + ",".join(f'{{"name":"n{i}","b":{i},"a":[{i},{{"y":1,"x":2}}]}}' for i in range(5000)) + "]}"
    server = '{"items":[' + ",".join(f'{{"a":[{i},{{"x":2,"y":1}}],"b":{i},"name":"n{i}"}}' for i in range(5000)) + "]}"
    pretty = json.dumps(json.loads(edited), indent=2)
    for text in (edited, pretty):
        tracemalloc.start()
        try:
            found = diff(text, server)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert found.counts.key_order == 10000
        assert peak < 6 * len(text), peak / len(text)  # measured 2.0× (compact) and 4.4× (pretty)


def test_one_line_keeps_every_character_that_is_not_space_between_tokens():
    assert one_line('{"a": "x  y",\n\t"b" : [1 , 2]}') == '{"a":"x  y","b":[1,2]}'
    assert one_line('{"a": "no end') == '{"a":"noend'  # not JSON: only the whitespace outside strings goes
    assert one_line('"a"  "b') == '"a""b'
