import json
import random
import re
import time
import tracemalloc

import pytest

from dbaguy_backend.domain.couchbase import ChangeCounts, DocumentChange
from dbaguy_backend.sql import jsontext
from dbaguy_backend.sql.jsontext import (
    CHANGE_TEXT_LIMIT, CHANGES_LISTED, CHANGES_PER_KIND, EXCERPT_LEAD, KEYS_SHOWN, MAX_DOCUMENT_DEPTH, DocumentDiff,
    JsonProblem, NotJson, diff, json_path, json_problem, number_paths, one_line, value_end, without_strings,
)

COMPACT_FROM = jsontext._COMPACT_FROM


@pytest.fixture(autouse=True, params=["lists", "arrays"])
def offset_containers(request, monkeypatch):
    """Objects under COMPACT_FROM characters remember offsets in lists, bigger ones in arrays: run every test with each
    kind everywhere, since a document's small objects and its big ones must give the same answer."""
    monkeypatch.setattr(jsontext, "_COMPACT_FROM", 0 if request.param == "arrays" else 1 << 40)


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


def test_json_problem_names_too_deep_nesting_instead_of_raising():  # far past what the C decoder itself accepts
    deep = "[" * 100_000 + "]" * 100_000
    assert json_problem(deep) == JsonProblem(0, "Nested deeper than 100 levels")


@pytest.mark.parametrize("text, reason", [
    ('{"a":"x\ty"}', "Invalid control character"), ('{"a":"abc', "Unterminated string"),
    ('["\\x"]', "Invalid \\escape"), ("[1,]", "Illegal trailing comma before end of array"),
    ('{"a" 1}', "Expecting ':' delimiter"),
])
def test_json_problem_reasons_do_not_end_in_the_word_at(text, reason):  # "…: Unterminated string starting at"
    assert json_problem(text).reason == reason


@pytest.mark.parametrize("open_, close", [("[", "]"), ('{"a":', "}"), ('[{"a":', "}]")])
def test_json_problem_accepts_the_deepest_allowed_nesting_and_refuses_one_level_more(open_, close):
    per_group = open_.count("[") + open_.count("{")
    groups = MAX_DOCUMENT_DEPTH // per_group  # 100 levels exactly for each of these shapes
    assert groups * per_group == MAX_DOCUMENT_DEPTH
    assert json_problem(open_ * groups + "1" + close * groups) is None
    assert json_problem("[" + open_ * groups + "1" + close * groups + "]") == JsonProblem(
        0, "Nested deeper than 100 levels"
    )
    assert json_problem("  " + "[" + open_ * groups + "1" + close * groups + "]  ") == JsonProblem(
        2, "Nested deeper than 100 levels"
    )


def test_brackets_inside_strings_are_not_levels_of_nesting():
    text = json.dumps({"a": "[" * 500 + "{" * 500, "b": ['"[[[[', "\\[[[[", "[\\\"[[["], "c" + "[" * 300: "}" * 300})
    assert text.count("[") + text.count("{") > MAX_DOCUMENT_DEPTH
    assert json_problem(text) is None


def test_without_strings_keeps_numbers_and_structure():
    assert without_strings('{"a]":"x\\"y",  "b":[1,"2",1e400]}') == '{"":"",  "":[1,"",1e400]}'


def test_json_path_is_written_like_the_value_viewer():
    assert json_path(()) == "$"
    assert json_path(("a", 0, "b c", "$x", "中", "\ud800")) == '$.a[0]["b c"].$x["中"]["\\ud800"]'
    assert json_path(("a\n",)) == '$["a\\n"]'  # a plain key ends at its end: `$` alone would let a newline pass


def test_number_paths_finds_numbers_by_their_literal():
    text = '{"a":1,"b":{"c":[1, 123456789012345678901,{"d":2}]},"odd key":1e400,"s":"1e400","n":[1e400]}'
    wanted = lambda literal: literal.endswith("e400") or len(literal) > 18  # noqa: E731
    assert number_paths(text, wanted, 50) == (["$.b.c[1]", '$["odd key"]', "$.n[0]"], 3)
    assert number_paths(text, wanted, 1) == (["$.b.c[1]"], 3)
    assert number_paths('"just a string"', wanted, 50) == ([], 0)


def test_number_paths_of_a_bare_number_and_of_empty_containers():
    assert number_paths(" 1e400 ", lambda literal: True, 5) == (["$"], 1)
    assert number_paths('{"a":{},"b":[],"c":[[],{}]}', lambda literal: True, 5) == ([], 0)


def test_number_paths_and_diff_still_raise_for_nesting_deeper_than_python_allows():  # ruling C27: past json_problem
    deep = '{"a":' * 1100 + "12345678901234567890" + "}" * 1100
    assert json_problem(deep) == JsonProblem(0, "Nested deeper than 100 levels")
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
        ("$", "other", "3 elements", "2 elements"), ("$[0]", "number", "1.0", "1"),
        ("$[1]", "key_order", '"b", "a"', '"a", "b"'),
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


def test_a_value_change_is_listed_however_many_respellings_come_first():  # review: 150 × 1.0 then a lost digit
    edited = '{"a":[' + ",".join(["1.0"] * 150) + '],"pi":3.141592653589793238462}'
    server = '{"a":[' + ",".join(["1"] * 150) + '],"pi":3.141592653589793}'
    found = diff(edited, server)
    assert found.counts == ChangeCounts(number=151)
    assert [c.path for c in found.changes if c.value_changed] == ["$.pi"]
    assert sum(not c.value_changed for c in found.changes) == CHANGES_PER_KIND


@pytest.mark.parametrize("seed", range(12))
def test_every_value_change_up_to_its_cap_is_listed_among_any_number_of_respellings(seed):
    rng = random.Random(seed)
    kinds = (
        ["respelled"] * rng.randrange(0, 400) + ["lost"] * rng.randrange(0, 150) + ["reordered"] * rng.randrange(0, 150)
    )
    rng.shuffle(kinds)
    edited, server = [], []
    for kind in kinds:
        edited.append({"respelled": "1.0", "lost": "3.141592653589793238462", "reordered": '{"b":1,"a":2}'}[kind])
        server.append({"respelled": "1", "lost": "3.141592653589793", "reordered": '{"a":2,"b":1}'}[kind])
    found = diff("[" + ",".join(edited) + "]", "[" + ",".join(server) + "]")
    paths = {kind: [f"$[{i}]" for i, k in enumerate(kinds) if k == kind] for kind in ("respelled", "lost", "reordered")}
    assert found.counts == ChangeCounts(
        number=len(paths["respelled"]) + len(paths["lost"]), key_order=len(paths["reordered"])
    )
    expected = (  # value changes first, then the rest; each kind kept to CHANGES_PER_KIND; the list to CHANGES_LISTED
        paths["lost"][:CHANGES_PER_KIND] + paths["respelled"][:CHANGES_PER_KIND] + paths["reordered"][:CHANGES_PER_KIND]
    )[:CHANGES_LISTED]
    assert [c.path for c in found.changes] == expected
    assert [c.path for c in found.changes if c.value_changed] == paths["lost"][:CHANGES_PER_KIND]


def test_a_long_string_that_differs_only_far_in_is_cut_around_the_difference():
    edited = '{"s":"' + "a" * 1000 + "X" + "b" * 1000 + '"}'
    server = '{"s":"' + "a" * 1000 + "Y" + "b" * 1000 + '"}'
    (found,) = diff(edited, server).changes
    middle = "a" * EXCERPT_LEAD + "{}" + "b" * (CHANGE_TEXT_LIMIT - EXCERPT_LEAD - 1)
    assert found.before == "…" + middle.format("X") + "…" and found.after == "…" + middle.format("Y") + "…"


def test_a_long_string_that_differs_after_the_first_chunk_is_still_cut_around_the_difference():
    prefix = "é" * 100_000  # the escape spelling of the server's side differs from the start and is not the difference
    edited = '{"s":"' + prefix + "X" + '"}'
    server = '{"s":"' + prefix + "Y" + '"}'
    (found,) = diff(edited.replace("é", "\\u00e9", 1), server).changes
    assert found.before.startswith("…") and found.before.endswith("éX\"") and found.after.endswith("éY\"")
    assert len(found.before) == len(found.after) <= CHANGE_TEXT_LIMIT + 1


def test_a_long_string_with_a_lone_surrogate_is_cut_into_text_that_encodes():
    (found,) = diff('{"s":"\\ud800' + "a" * 300 + '"}', '{"s":"\\ufffd' + "a" * 300 + '"}').changes
    found.before.encode("utf-8"), found.after.encode("utf-8")
    assert found.before.startswith('"\\ud800aaa') and found.after.startswith('"\ufffdaaa')


def test_a_dropped_duplicate_that_differs_only_far_in_is_cut_around_the_difference():  # both sides showed "xxx…"
    first, second = "x" * 300 + "X", "x" * 300 + "Y"
    (found,) = diff('{"a":"' + first + '","a":"' + second + '"}', '{"a":"' + second + '"}').changes
    assert found.kind == "duplicate_key"
    assert found.before == "…" + "x" * EXCERPT_LEAD + 'X"' and found.after == "…" + "x" * EXCERPT_LEAD + 'Y"'


def test_a_dropped_duplicate_object_that_differs_only_far_in_is_cut_around_the_difference():
    inner = '{"k":"' + "x" * 400 + '","n":%s}'
    (found,) = diff('{"a":' + inner % "1" + ',"a":' + inner % "2" + "}", '{"a":' + inner % "2" + "}").changes
    assert found.before.startswith("…") and found.before.endswith('"n":1}') and found.after.endswith('"n":2}')
    assert found.before != found.after and len(found.before) <= CHANGE_TEXT_LIMIT + 2


def test_a_value_of_another_type_is_shown_from_its_start_and_never_copied_whole():
    (found,) = diff('{"a":"' + "y" * 5000 + '"}', '{"a":' + "[" + ",".join(["1"] * 3000) + "]}").changes
    assert found.kind == "other"
    assert found.before == '"' + "y" * (CHANGE_TEXT_LIMIT - 1) + "…" and found.after == "[" + "1," * 99 + "1…"


@pytest.mark.parametrize("shift", range(12))
def test_a_long_string_is_never_cut_inside_an_escape(shift):
    body = ("x" * 9 + "\\n") * 60  # a newline escape every 10 characters; the shift moves the window across them
    edited = '{"s":"' + "q" * shift + body + "X" + body + '"}'
    server = '{"s":"' + "q" * shift + body + "Y" + body + '"}'
    (found,) = diff(edited, server).changes
    for text, mark in ((found.before, "X"), (found.after, "Y")):
        assert text.startswith("…") and text.endswith("…") and mark in text
        json.loads('"' + text[1:-1] + '"')  # whole escapes only: half an escape is not a JSON string


@pytest.mark.parametrize("shift", range(8))
def test_a_cut_of_raw_json_never_falls_inside_an_escape_either(shift):
    body = "".join(f"{i:03}\\u00e9\\\\\\n" for i in range(60))  # unique pieces: a \uXXXX, an escaped backslash, a \n
    one = '{"k":"' + "q" * shift + body + '","n":1}'
    two = '{"k":"' + "q" * shift + body + '","n":2}'
    (found,) = diff('{"a":' + one + ',"a":' + two + "}", '{"a":' + two + "}").changes
    assert found.kind == "duplicate_key" and found.before != found.after
    for shown, source in ((found.before, one), (found.after, two)):
        piece = shown.removeprefix("…").removesuffix("…")
        boundaries = {m.start() for m in re.finditer(r"\\u[0-9a-fA-F]{4}|\\.|.", source, re.S)} | {len(source)}
        at = source.index(piece)
        assert at in boundaries and at + len(piece) in boundaries  # starts and ends between tokens, not inside one


@pytest.mark.parametrize("text, i, expected", [
    ("ab\\ncd", 3, 2), ("ab\\ncd", 2, 2), ("ab\\ncd", 4, 4), ("ab\\\\ncd", 3, 2), ("ab\\\\ncd", 4, 4),
    ("a\\u00e9b", 2, 1), ("a\\u00e9b", 3, 1), ("a\\u00e9b", 5, 1), ("a\\u00e9b", 6, 1), ("a\\u00e9b", 7, 7),
    ("a\\\\u00e9b", 2, 1), ("a\\\\u00e9b", 3, 3), ("a\\\\u00e9b", 4, 4), ("abc", 0, 0), ("abc", 3, 3),
])
def test_escape_start_finds_the_backslash_of_the_escape_a_cut_would_split(text, i, expected):
    assert jsontext._escape_start(text, i) == expected


def test_a_long_number_that_differs_only_far_in_is_cut_around_the_difference():
    (found,) = diff("[" + "1" * 299 + "2]", "[" + "1" * 299 + "3]").changes
    assert (found.before, found.after) == ("…" + "1" * EXCERPT_LEAD + "2", "…" + "1" * EXCERPT_LEAD + "3")
    assert found.value_changed


def test_a_changed_key_set_names_the_keys_on_one_side_only():
    (found,) = diff('{"keep":1,"gone":2,"also gone":3}', '{"keep":1,"new":4}').changes
    assert (found.kind, found.before, found.after) == ("other", '"gone", "also gone"', '"new"')
    (found,) = diff('{"a":1}', '{"a":1,"b":2}').changes
    assert (found.before, found.after) == ("(none)", '"b"')
    (found,) = diff('{"a":1,"b":2}', '{"a":1}').changes
    assert (found.before, found.after) == ('"b"', "(none)")


def test_a_key_set_change_caps_each_side_and_counts_its_keys():
    edited = "{" + ",".join(f'"k{i:02}":1' for i in range(15)) + ',"same":1}'
    (found,) = diff(edited, '{"same":1,"new":1}').changes
    assert found.before.endswith(", … (15 keys)") and found.before.count(", ") == KEYS_SHOWN
    assert found.after == '"new"'


def test_a_dropped_duplicate_key_shows_the_earlier_value_and_the_later_one_the_server_keeps():
    (found,) = diff('{"a":"first","a":"second"}', '{"a":"second"}').changes
    assert (found.kind, found.before, found.after) == ("duplicate_key", '"first"', '"second"')


def test_members_are_found_by_key_not_by_position_when_the_order_differs_in_a_big_object():
    edited = "{" + ",".join(f'"k{i}":{i}' for i in range(2000, 0, -1)) + "}"
    server = "{" + ",".join(f'"k{i}":{i if i != 77 else "7.5"}' for i in range(1, 2001)) + "}"
    found = diff(edited, server)
    assert [(c.path, c.kind, c.before, c.after) for c in found.changes] == [
        ("$.k77", "number", "77", "7.5"),
        ("$", "key_order", ", ".join(f'"k{i}"' for i in range(2000, 1990, -1)) + ", … (2000 keys)",
         ", ".join(f'"k{i}"' for i in range(1, 11)) + ", … (2000 keys)"),
    ]


def test_diff_of_a_wide_object_keeps_offsets_not_objects(monkeypatch):  # 100,000 members; review: 27× before
    monkeypatch.setattr(jsontext, "_COMPACT_FROM", COMPACT_FROM)
    edited = "{" + ",".join(f'"k{i}":{i}' for i in range(100_000)) + "}"  # not sorted: the server may keep this order
    peaks = {}
    changed = {"a value": edited.replace('"k5":5,', '"k5":6,', 1), "a key": edited.replace('"k5"', '"k5x"', 1)}
    for label, server in changed.items():
        tracemalloc.start()
        try:
            found = diff(edited, server)
            peaks[label] = tracemalloc.get_traced_memory()[1] / len(edited)
        finally:
            tracemalloc.stop()
        assert sum(found.counts.model_dump().values()) == 1
    assert peaks["a value"] < 0.5  # members read in step, nothing kept but the one that differs
    assert peaks["a key"] < 14, peaks  # the keys of both sides are compared by name: about 11×, and 27× as objects


def test_a_document_nested_to_the_limit_still_diffs_quickly():  # ruling C38: depth × size is bounded
    payload = "[" + ",".join(["1.5"] * 25_000) + "]"  # 100 KB at the 100th level
    levels = MAX_DOCUMENT_DEPTH - 1  # the payload array is the last level
    edited = '{"a":' * levels + payload + "}" * levels
    server = '{"a":' * levels + payload.replace("1.5", "1.50", 1) + "}" * levels
    assert json_problem(edited) is None
    started = time.perf_counter()
    found = diff(edited, server)
    paths, count = number_paths(edited, lambda literal: literal == "1.5", 3)
    assert time.perf_counter() - started < 10  # about 0.2 s; a loaded machine and a slow one both fit
    where = "$" + ".a" * levels
    assert found.counts == ChangeCounts(number=1) and not found.changes[0].value_changed
    assert found.changes[0].path == where + "[0]" and paths[0] == where + "[0]" and count == 25_000


def test_one_line_keeps_every_character_that_is_not_space_between_tokens():
    assert one_line('{"a": "x  y",\n\t"b" : [1 , 2]}') == '{"a":"x  y","b":[1,2]}'
    assert one_line('{"a": "no end') == '{"a":"noend'  # not JSON: only the whitespace outside strings goes
    assert one_line('"a"  "b') == '"a""b'
