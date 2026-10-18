import pytest

from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.format_guard import first_difference

SQ, PG = Dialect.SQLPP, Dialect.POSTGRES


@pytest.mark.parametrize("dialect, original, formatted", [
    (PG, "select a from t where b = 'x'", "SELECT\n  a\nFROM\n  t\nWHERE\n  b = 'x'"),
    (PG, "select Users.a from users", "SELECT USERS.a FROM users"),  # word case is free outside SQL++
    (SQ, "select meta(d).id, d from ks as d use keys $k", "SELECT\n  META(d).id,\n  d\nFROM\n  ks AS d USE KEYS $k"),
    (SQ, "/* a   note */ select 1", "/*\n  a note\n*/\nSELECT\n  1"),  # comments: layout only
])
def test_whitespace_keyword_case_and_comment_layout_are_the_same(dialect, original, formatted):
    assert first_difference(dialect, original, formatted) is None


def test_a_split_string_is_refused_with_its_offset():
    original = "SELECT 'it''s' AS a"
    assert first_difference(SQ, original, "SELECT\n  'it' 's' AS a") == original.index("'it")


def test_sqlpp_identifier_case_matters():
    assert first_difference(SQ, "SELECT d.type FROM ks d", "SELECT d.TYPE FROM ks d") == len("SELECT d.")


def test_extra_or_missing_tokens():
    assert first_difference(PG, "SELECT 1", "SELECT 1;") == len("SELECT 1")
    assert first_difference(PG, "SELECT 1, 2", "SELECT 1") == len("SELECT 1")
