"""Which key identifies a row (spec ⑤ R-05-2, ruling E4): the engine-neutral choice among an engine's candidates."""

from dbaguy_backend.adapters.editing import NO_KEY, KeyCandidate, build_target
from dbaguy_backend.domain.edits import EditColumn
from dbaguy_backend.domain.metadata import PathSegment

PATH = [PathSegment(kind="schema", name="s"), PathSegment(kind="folder", name="tables"), PathSegment(kind="table", name="t")]


def column(name, nullable=False, compare="equals", type_name="int") -> EditColumn:
    return EditColumn(name=name, type_name=type_name, kind="number", nullable=nullable, has_default=False,
                      writable=True, compare=compare)


def test_the_primary_key_comes_first():
    columns = [column("id"), column("code")]
    target = build_target(PATH, columns, [KeyCandidate("u_code", ("code",), False), KeyCandidate("pk", ("id",), True)])
    assert (target.editable, target.key, target.key_source, target.columns) == (True, ["id"], "primary_key", columns)


def test_else_the_unique_key_on_not_null_columns_with_the_fewest_columns_then_by_name():
    columns = [column("a"), column("b"), column("c"), column("d", nullable=True)]
    candidates = [KeyCandidate("u_abc", ("a", "b", "c"), False), KeyCandidate("u_d", ("d",), False),
                  KeyCandidate("u_cb", ("c", "b"), False), KeyCandidate("u_ab", ("a", "b"), False)]
    target = build_target(PATH, columns, candidates)
    assert (target.key, target.key_source) == (["a", "b"], "unique")


def test_a_key_that_cannot_be_matched_exactly_gives_way_or_says_why():
    floaty = column("f", compare="none", type_name="double precision")
    assert build_target(PATH, [floaty, column("n")], [KeyCandidate("pk", ("f",), True),
                                                      KeyCandidate("u_n", ("n",), False)]).key == ["n"]
    refused = build_target(PATH, [floaty], [KeyCandidate("pk", ("f",), True)])
    assert not refused.editable and refused.reason == (
        "The key column f (double precision) cannot be matched exactly: a row cannot be identified")


def test_a_primary_key_may_hold_null_and_no_candidate_means_no_key():
    assert build_target(PATH, [column("code", nullable=True)], [KeyCandidate("pk", ("code",), True)]).key == ["code"]
    assert build_target(PATH, [column("a", nullable=True)], [KeyCandidate("u", ("a",), False)]).reason == NO_KEY
    assert build_target(PATH, [column("a")], [KeyCandidate("u", ("gone",), False)]).reason == NO_KEY
    target = build_target(PATH, [column("a")], [], transactional=False, backslash_escapes=False)
    assert (target.editable, target.reason, target.transactional, target.backslash_escapes) == (False, NO_KEY, False, False)
