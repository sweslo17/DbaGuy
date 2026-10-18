import pytest

from dbaguy_backend.adapters.couchbase.rows import (
    OVERFLOW_NAME, build_shape, has_bare_star, needs_first_page, value_cell,
)
from dbaguy_backend.adapters.couchbase.stream import ResponseParser, RowEvent
from dbaguy_backend.adapters.values import MISSING
from dbaguy_backend.adapters.values import JsonText as J
from dbaguy_backend.domain.column_category import ColumnCategory as C


def rows(*texts):
    """RawRows exactly as the parser makes them."""
    parser = ResponseParser()
    events = parser.feed(('{"results":[' + ",".join(texts) + "]}").encode()) + parser.finish()
    return [e.row for e in events if isinstance(e, RowEvent)]


def names(shape):
    return [c.name for c in shape.columns]


def shape_of(signature, page=(), bare_star=False, plan=False):
    return build_shape(signature, plan=plan, bare_star=bare_star, first_page=list(page))


@pytest.mark.parametrize("text, cell", [
    ("42", 42), ("-7", -7), ("9007199254740991", 9007199254740991), ("9007199254740993", J("9007199254740993")),
    ("-9007199254740991", -9007199254740991), ("-9007199254740993", J("-9007199254740993")),
    ("-9223372036854775808", J("-9223372036854775808")), ("18446744073709551615", J("18446744073709551615")),
    ("12345678901234567890", J("12345678901234567890")), ("1" * 5000, J("1" * 5000)), ("-0", J("-0")),
    ("0.5", 0.5), ("-0.0", -0.0), ("3.141592653589793", 3.141592653589793), ("1.10", J("1.10")),
    ("3.141592653589793238462643383279", J("3.141592653589793238462643383279")), ("1E+3", J("1E+3")),
    ("1e400", J("1e400")), ("1e-400", J("1e-400")),
    ('"x"', "x"), ('"[1]"', "[1]"), ("true", True), ("false", False), ("null", None),
    ("[1, 2]", J("[1, 2]")), ('{"a":1}', J('{"a":1}')),
])
def test_number_cells_follow_the_literal(text, cell):  # Review Focus 1
    [row] = rows(text)
    got = value_cell(row.text, row.value)
    assert (type(got), got) == (type(cell), cell)


def test_the_four_literals_a_float_or_int_would_change():  # R-04a-C13: exactly these, in rows and inside objects
    texts = ["-0", "1.10", "1E+3", "9007199254740993"]
    assert [value_cell(r.text, r.value) for r in rows(*texts)] == [J(t) for t in texts]
    shape = shape_of({"a": "json"})
    assert [shape.cells(r) for r in rows(*(f'{{"a":{t}}}' for t in texts))] == [[J(t)] for t in texts]
    inside = rows('{"a":{"n":-0,"m":[1.10]}}')
    assert shape.cells(inside[0]) == [J('{"n":-0,"m":[1.10]}')]


def test_no_signature_has_no_columns():
    assert shape_of(None) is None


def test_raw_results_are_one_value_column():
    shape = shape_of("number")
    assert [(c.name, c.type_name, c.category) for c in shape.columns] == [("value", "number", C.NUMBER)]
    assert [shape.cells(r) for r in rows("1", "9007199254740993", '"x"')] == [[1], [J("9007199254740993")], ["x"]]


@pytest.mark.parametrize("signature, category", [
    ("string", C.TEXT), ("boolean", C.BOOL), ("json", C.MIXED), ("object", C.MIXED), ("array", C.MIXED),
])
def test_value_column_categories(signature, category):
    shape = shape_of(signature)
    assert [(c.name, c.type_name, c.category) for c in shape.columns] == [("value", signature, category)]
    assert shape.cells(rows('{"a":1}')[0]) == [J('{"a":1}')]  # an object row under any signature stays whole


def test_an_object_signature_keeps_its_order_and_types():
    shape = shape_of({"id": "json", "n": "number", "s": "string", "b": "boolean"})
    assert [(c.name, c.type_name, c.category) for c in shape.columns] == [
        ("id", "json", C.MIXED), ("n", "number", C.NUMBER), ("s", "string", C.TEXT), ("b", "boolean", C.BOOL),
    ]
    assert shape.cells(rows('{"n":1,"id":"k","b":true}')[0]) == ["k", 1, MISSING, True]


def test_null_is_not_missing():
    shape = shape_of({"a": "json", "b": "json"})
    assert shape.cells(rows('{"a":null}')[0]) == [None, MISSING]


def test_union_rows_have_only_their_own_keys():
    shape = shape_of({"a": "number", "b": "number"})
    assert [shape.cells(r) for r in rows('{"a":1}', '{"b":2}')] == [[1, MISSING], [MISSING, 2]]
    assert shape.take_warning() is None


def test_keys_outside_a_plain_signature_warn_once():
    shape = shape_of({"a": "json"})
    assert [shape.cells(r) for r in rows('{"a":1}', '{"a":2,"z":3}', '{"a":4,"y":5}', "7")] == [[1], [2], [4], [MISSING]]
    assert shape.take_warning() == "Some rows have keys outside the columns (from row 2); export as JSON to see every key"
    assert shape.take_warning() is None


def test_the_warning_is_not_raised_again_after_it_was_taken():
    shape = shape_of({"a": "json"})
    shape.cells(rows('{"a":1,"z":3}')[0])
    assert shape.take_warning() is not None
    shape.cells(rows('{"a":1,"y":3}')[0])
    assert shape.take_warning() is None


def test_a_row_that_is_not_an_object_warns_too():
    shape = shape_of({"a": "json"})
    assert shape.cells(rows('"text"')[0]) == [MISSING]
    assert "from row 1" in shape.take_warning()


@pytest.mark.parametrize("signature", [{"plan": "json", "text": "json"}, "json"])
def test_a_plan_is_one_cell_of_the_whole_row(signature):  # A15: either signature shape
    [row] = rows('{"plan":{"#operator":"Sequence"},"text":"SELECT 1"}')
    shape = shape_of(signature, plan=True)
    assert [(c.name, c.category) for c in shape.columns] == [("plan", C.MIXED)] and shape.cells(row) == [J(row.text)]


def test_star_columns_follow_the_first_page():
    page = rows('{"name":"a","age":1}', '{"age":null,"city":"x"}', '{"name":"b","tags":[1]}')
    shape = shape_of({"*": "*"}, page)
    assert [(c.name, c.type_name) for c in shape.columns] == [
        ("name", "string"), ("age", "number|null"), ("city", "string"), ("tags", "array"), (OVERFLOW_NAME, "object"),
    ]
    assert {c.category for c in shape.columns} == {C.MIXED}
    assert shape.cells(page[0]) == ["a", 1, MISSING, MISSING, MISSING]


def test_star_keys_after_the_first_page_go_to_the_overflow():  # Review Focus 4
    shape = shape_of({"id": "json", "*": "*", "amount": "json"}, rows('{"id":"k","name":"a","amount":1}'))
    assert names(shape) == ["id", "name", "amount", OVERFLOW_NAME]
    later = rows('{"id":"k","name":"b","late":{"x":1e400},"amount":1.10}', '"not an object"')
    assert shape.cells(later[0]) == ["k", "b", J("1.10"), J('{"late":{"x":1e400}}')]
    assert shape.cells(later[1]) == [MISSING, MISSING, MISSING, J('"not an object"')]


def test_the_columns_never_change_after_the_first_page():  # Review Focus 4
    shape = shape_of({"*": "*"}, rows('{"a":1}'))
    before = [c.model_copy() for c in shape.columns]
    for row in rows('{"b":1}', '{"a":2,"c":3}', "5", '{"d":{"e":1}}'):
        assert len(shape.cells(row)) == len(shape.columns)
    assert shape.columns == before


def test_no_key_and_no_value_is_lost_across_a_mixed_page():  # Review Focus 4
    page = rows('{"a":1}', '{"b":"x"}', '{"a":2,"c":[1,{"d":1.10}],"b":null}', "[1,2]", '"s"', '{"z":9007199254740993}')
    shape = shape_of({"*": "*"}, page[:3])  # columns a, b, c
    for row in page:
        cells = shape.cells(row)
        assert len(cells) == len(shape.columns)
        if row.fields is None:  # not an object: whole, in the overflow column
            assert cells == [MISSING] * 3 + [J(row.text)]
            continue
        shown = {c.name: cell for c, cell in zip(shape.columns, cells) if cell is not MISSING and c.name != OVERFLOW_NAME}
        outside = [f for f in row.fields if f.key not in {"a", "b", "c"}]
        assert set(shown) == {f.key for f in row.fields} - {f.key for f in outside}
        assert cells[-1] == (J("{" + ",".join(f'"{f.key}":{f.text}' for f in outside) + "}") if outside else MISSING)
    assert shape.cells(page[2]) == [2, None, J('[1,{"d":1.10}]'), MISSING]
    assert shape.cells(page[5]) == [MISSING, MISSING, MISSING, J('{"z":9007199254740993}')]


def test_alias_expansion_needs_one_object_alias_on_every_first_page_row():  # Review Focus 4
    sig = {"id": "json", "*": "*"}
    page = rows('{"id":"e1","d":{"zeta":1,"alpha":{"z":1},"id":7}}', '{"id":"e2","d":{"alpha":2,"kind":"x"}}')
    shape = shape_of(sig, page, bare_star=True)
    assert names(shape) == ["id", "zeta", "alpha", "d.id", "kind", OVERFLOW_NAME]  # stored order; d.id: a projected name
    assert shape.cells(page[0]) == ["e1", 1, J('{"z":1}'), 7, MISSING, MISSING]
    assert names(shape_of(sig, page))[:2] == ["id", "d"]  # `d.*`, not a bare star
    mixed = rows('{"id":"e1","d":{"a":1}}', '{"id":"s1","d":"just a string"}')
    assert names(shape_of(sig, mixed, bare_star=True)) == ["id", "d", OVERFLOW_NAME]
    two = rows('{"id":"e1","d":{"a":1},"o":{"b":2}}')
    assert names(shape_of(sig, two, bare_star=True)) == ["id", "d", "o", OVERFLOW_NAME]
    assert names(shape_of(sig, [], bare_star=True)) == ["id", OVERFLOW_NAME]  # an empty result


def test_alias_expansion_needs_the_same_alias_on_every_row():
    sig = {"*": "*"}
    assert names(shape_of(sig, rows('{"a":{"x":1}}', '{"b":{"y":2}}'), bare_star=True)) == ["a", "b", OVERFLOW_NAME]
    assert names(shape_of(sig, rows('{"a":{"x":1}}', "7"), bare_star=True)) == ["a", OVERFLOW_NAME]
    assert names(shape_of(sig, rows('{"a":{"x":1}}', '{}'), bare_star=True)) == ["a", OVERFLOW_NAME]


def test_alias_expansion_keeps_the_stored_order_and_exact_texts():  # Review Focus 1: u_keyorder, u_numbers
    page = rows('{"id":"u","d":{"z":1,"a":1.10,"m":1E+3,"b":-0,"big":9007199254740993,"over":1e400}}')
    shape = shape_of({"id": "json", "*": "*"}, page, bare_star=True)
    assert names(shape) == ["id", "z", "a", "m", "b", "big", "over", OVERFLOW_NAME]
    assert shape.cells(page[0]) == ["u", 1, J("1.10"), J("1E+3"), J("-0"), J("9007199254740993"), J("1e400"), MISSING]


def test_alias_expansion_falls_back_per_row():  # Review Focus 4
    shape = shape_of({"id": "json", "*": "*"}, rows('{"id":"e1","d":{"a":1}}'), bare_star=True)
    later = rows('{"id":"e2","d":{"a":2,"b":3}}', '{"id":"e3","d":[1,2]}', '{"id":"e4","x":{"a":5}}')
    assert [shape.cells(r) for r in later] == [
        ["e2", 2, J('{"b":3}')],  # a member the first page did not show
        ["e3", MISSING, J('{"d":[1,2]}')],  # the alias holds no object: kept whole
        ["e4", MISSING, J('{"x":{"a":5}}')],  # another alias
    ]


def test_a_repeated_key_in_an_alias_object_is_kept():  # a stored document may repeat a key; `SELECT *` sends it as is
    shape = shape_of({"*": "*"}, rows('{"d":{"a":1}}'), bare_star=True)
    assert shape.cells(rows('{"d":{"a":1,"a":2,"b":3}}')[0]) == [1, J('{"a":2,"b":3}')]


def test_names_that_collide():  # Review Focus 4
    page = rows('{"' + OVERFLOW_NAME + '":1,"a":2}')
    assert names(shape_of({"*": "*"}, page)) == [OVERFLOW_NAME, "a", OVERFLOW_NAME + " 2"]
    alias_page = rows('{"id":"e1","d":{"id":1,"d.id":2}}')
    assert names(shape_of({"id": "json", "*": "*"}, alias_page, bare_star=True)) == ["id", "d.id", "d.id 2", OVERFLOW_NAME]
    taken_twice = rows('{"' + OVERFLOW_NAME + '":1,"' + OVERFLOW_NAME + ' 2":2}')
    assert names(shape_of({"*": "*"}, taken_twice)) == [OVERFLOW_NAME, OVERFLOW_NAME + " 2", OVERFLOW_NAME + " 3"]


def test_a_column_the_user_named_star_is_a_plain_column():  # SELECT 1 AS `*` is signed {"*": "number"} (live)
    shape = shape_of({"*": "number"})
    assert [(c.name, c.category) for c in shape.columns] == [("*", C.NUMBER)]
    assert shape.cells(rows('{"*":1}')[0]) == [1]
    assert not needs_first_page({"*": "number"}, plan=False)


def test_key_text_in_the_overflow_is_json_and_survives_a_lone_surrogate():
    shape = shape_of({"*": "*"}, rows('{"a":1}'))
    cell = shape.cells(rows('{"a":1,"中\\"文":true,"\\ud800":0}')[0])[-1]
    assert cell == J('{"中\\"文":true,"\\ud800":0}')
    cell.text.encode("utf-8")


def test_an_empty_signature_shows_each_row_whole():
    shape = shape_of({})
    assert [(c.name, c.type_name) for c in shape.columns] == [("value", "{}")]
    assert shape.cells(rows('{"a":1}')[0]) == [J('{"a":1}')]


@pytest.mark.parametrize("sql, bare", [
    ("SELECT * FROM ks", True), ("select meta(d).id, * from ks d", True), ("SELECT DISTINCT * FROM ks", True),
    ("SELECT * FROM a UNION SELECT b.* FROM b", True), ("SELECT d.* FROM ks d", False),
    ("SELECT COUNT(*) FROM ks", False), ("SELECT a * 2 FROM ks", False), ("SELECT RAW d FROM ks d", False),
    ("WITH c AS (SELECT * FROM ks) SELECT c.* FROM c", False), ("SELECT (SELECT * FROM x) AS y FROM ks", False),
    ("SELECT `*` FROM ks", False), ("SELECT 1 /* , * */ FROM ks", False),
    ("SELECT 1, 2 * 3 FROM ks", False), ("SELECT ALL * FROM ks", True), ("FROM ks AS d SELECT *", True),
    ("SELECT '*', \"*\" FROM ks", False), ("SELECT d.a, COUNT(*) AS n FROM ks d GROUP BY d.a", False),
    ("SELECT a, -- the rest\n  * FROM ks", True), ("SELECT * FROM ks WHERE a IN (SELECT b.* FROM b)", True),
    ("SELECT a FROM ks WHERE b = (SELECT RAW MAX(c) FROM x) ORDER BY a * 2", False),
    ("INSERT INTO ks (KEY k, VALUE v) SELECT * FROM src", True), ("DELETE FROM ks", False), ("", False),
])
def test_bare_star(sql, bare):
    assert has_bare_star(sql) == bare


def test_only_star_signatures_are_peeked():
    assert needs_first_page({"*": "*"}, plan=False) and needs_first_page({"id": "json", "*": "*"}, plan=False)
    assert not needs_first_page({"*": "*"}, plan=True) and not needs_first_page("json", plan=False)
    assert not needs_first_page({"a": "json"}, plan=False) and not needs_first_page(None, plan=False)
