"""The shape of grid-edit requests (spec ⑤ §3.3.1, D-05-11, ruling E8): refused as a whole (422) when malformed."""

import json

import pytest
from pydantic import ValidationError

from dbaguy_backend.domain.column_category import ColumnCategory
from dbaguy_backend.domain.edits import (
    COMMIT_TOO_LARGE, MAX_CHANGES, MAX_COMMIT_CHARS, VALUE_ACCEPTED_CHARS, CellInput, EditChanges, edit_kind,
    kind_category,
)
from dbaguy_backend.domain.metadata import PathSegment, path_database


def test_a_cell_input_has_text_only_as_a_value():
    assert CellInput(kind="value", text="").text == ""
    for bad in ({"kind": "value"}, {"kind": "null", "text": "x"}, {"kind": "default", "text": ""}):
        with pytest.raises(ValidationError):
            CellInput.model_validate(bad)


def test_an_update_sets_a_column_and_gives_each_its_original():
    ok = {"id": "u:1", "key": {"id": 1}, "set": {"a": {"kind": "null"}}, "original": {"a": "x"}}
    EditChanges.model_validate({"updates": [ok]})
    for bad in ({**ok, "set": {}}, {**ok, "original": {}}):
        with pytest.raises(ValidationError):
            EditChanges.model_validate({"updates": [bad]})


def test_changes_are_counted_unique_and_not_empty():
    deletes = [{"id": f"d:{i}", "key": {"id": i}} for i in range(MAX_CHANGES)]
    assert len(EditChanges.model_validate({"deletes": deletes}).deletes) == MAX_CHANGES
    for bad in ({"deletes": [*deletes, {"id": "d:x", "key": {"id": -1}}]},
                {"deletes": [deletes[0], deletes[0]]}, {}):
        with pytest.raises(ValidationError):
            EditChanges.model_validate(bad)


BIG = "x" * VALUE_ACCEPTED_CHARS  # one value of the most the request accepts; shared, so building the cases is cheap
FULL = MAX_COMMIT_CHARS // VALUE_ACCEPTED_CHARS  # this many of them are exactly the most a commit may carry


def insert(i: int, text: str) -> dict:
    return {"id": f"i:{i}", "values": {"a": {"kind": "value", "text": text}}}


def refusal(changes: dict) -> str:
    """Why the request is refused. Not `str(error)`: that writes out the input, megabytes of it."""
    with pytest.raises(ValidationError) as raised:
        EditChanges.model_validate(changes)
    (error,) = raised.value.errors(include_input=False)
    return error["msg"]


def test_a_commit_is_bounded_in_the_text_of_its_values():  # pre-flight L15: 10,000 changes of 4 MiB are not one request
    full = [insert(i, BIG) for i in range(FULL)]
    assert len(EditChanges.model_validate({"inserts": full}).inserts) == FULL  # exactly 64 MiB
    assert refusal({"inserts": [*full, insert(FULL, "x")]}) == f"Value error, {COMMIT_TOO_LARGE}"  # one more character
    assert COMMIT_TOO_LARGE == "The changes are too large: at most 64 MiB of values per commit"


@pytest.mark.parametrize("change", [
    pytest.param({"updates": [{"id": "u:1", "key": {"id": 1}, "set": {f"c{i}": {"kind": "value", "text": BIG}
                                                                        for i in range(FULL + 1)},
                               "original": {f"c{i}": None for i in range(FULL + 1)}}]}, id="set"),
    pytest.param({"updates": [{"id": "u:1", "key": {"id": 1}, "set": {"a": {"kind": "null"}},
                               "original": {"a": [BIG] * (FULL + 1)}}]}, id="original"),
    pytest.param({"updates": [{"id": "u:1", "key": {"id": [BIG] * (FULL + 1)}, "set": {"a": {"kind": "null"}},
                               "original": {"a": None}}]}, id="update key"),
    pytest.param({"deletes": [{"id": "d:1", "key": {"id": {"k": [BIG] * (FULL + 1)}}}]}, id="delete key"),
    pytest.param({"deletes": [{"id": f"d:{i}", "key": {"id": BIG}} for i in range(FULL + 1)]}, id="many keys"),
])
def test_every_kind_of_cell_counts_toward_the_bound(change):  # L15: values, keys and original cells
    assert refusal(change) == f"Value error, {COMMIT_TOO_LARGE}"


def test_a_nested_cell_is_counted_whole():  # L15: lists and objects count every string inside, as deep as pydantic goes
    cell: object = [BIG] * (FULL // 2)  # 32 MiB
    for _ in range(100):
        cell = [{"k": cell}]
    one = {"deletes": [{"id": "d:1", "key": {"id": cell}}]}
    assert len(EditChanges.model_validate(one).deletes) == 1
    two = {"deletes": [*one["deletes"], {"id": "d:2", "key": {"id": cell}}]}  # 64 MiB and the 200 names above it
    assert refusal(two) == f"Value error, {COMMIT_TOO_LARGE}"


def test_a_lone_surrogate_is_refused_on_its_cell():  # ruling E19: pydantic refuses it in any str field
    bad = {"inserts": [{"id": "i:1", "values": {"label": {"kind": "value", "text": "half \ud83d"}}}]}
    with pytest.raises(ValidationError) as raised:
        EditChanges.model_validate(bad)
    assert raised.value.errors()[0]["loc"] == ("inserts", 0, "values", "label", "text")


@pytest.mark.parametrize(("change", "loc"), [
    ({"deletes": [{"id": "d:1", "key": {"half \ud83d": 1}}]}, ("deletes", 0, "key")),
    ({"updates": [{"id": "u:1", "key": {"id": 1}, "set": {"\ud83d": {"kind": "null"}}, "original": {"\ud83d": "x"}}]},
     ("updates", 0, "set")),
    ({"inserts": [{"id": "i:1", "values": {"": {"kind": "null"}}}]}, ("inserts", 0, "values")),
    ({"inserts": [{"id": "i:1", "values": {"c" * 1025: {"kind": "null"}}}]}, ("inserts", 0, "values")),
])
def test_a_column_name_is_valid_text(change, loc):  # review F4: a 422 whose body can be sent, never a 500
    with pytest.raises(ValidationError) as raised:
        EditChanges.model_validate(change)
    errors = raised.value.errors(include_input=False)
    assert errors[0]["loc"][:3] == loc
    json.dumps(errors, ensure_ascii=False).encode()  # what the 422 handler sends: no lone surrogate left in it


def test_an_edit_kind_comes_from_the_grid_category():
    assert [edit_kind(c) for c in (ColumnCategory.NUMBER, ColumnCategory.JSON, ColumnCategory.MIXED)] == [
        "number", "json", "other"]
    assert kind_category("other") == ColumnCategory.OTHER and kind_category("datetime") == ColumnCategory.DATETIME


def test_path_database_is_the_first_database_segment():
    database = PathSegment(kind="database", name="dbaguy")
    schema = PathSegment(kind="schema", name="app")
    assert (path_database([database, schema]), path_database([schema]), path_database([])) == ("dbaguy", None, None)
