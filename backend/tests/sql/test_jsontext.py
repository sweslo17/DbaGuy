import json

import pytest

from dbaguy_backend.sql.jsontext import NotJson, value_end


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
