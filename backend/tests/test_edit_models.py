"""The shape of grid-edit requests (spec ⑤ §3.3.1, D-05-11, ruling E8): refused as a whole (422) when malformed."""

import json

import pytest
from pydantic import ValidationError

from dbaguy_backend.domain.column_category import ColumnCategory
from dbaguy_backend.domain.edits import MAX_CHANGES, CellInput, EditChanges, edit_kind, kind_category
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
