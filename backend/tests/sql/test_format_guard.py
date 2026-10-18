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


@pytest.mark.parametrize("original, formatted", [
    ("INSERT INTO Users (KEY, VALUE) VALUES ('k', 1)", "INSERT INTO USERS (KEY, VALUE) VALUES ('k', 1)"),
    ("SELECT o.user FROM ks o", "SELECT o.USER FROM ks o"),  # reserved, yet a plain name to the server
    ("CREATE INDEX i ON Orders(amount)", "CREATE INDEX i ON ORDERS(amount)"),
    ("SELECT b.f(1) FROM ks b", "SELECT b.F(1) FROM ks b"),  # after a dot: a path, not a function
])
def test_sqlpp_names_before_a_paren_or_reserved_like_names_keep_their_case(original, formatted):
    assert first_difference(SQ, original, formatted) is not None


def test_sqlpp_function_names_are_case_free():
    assert first_difference(SQ, "select lower(d.a), count(*) from ks d", "SELECT LOWER(d.a), COUNT(*) FROM ks d") is None


def test_the_guard_is_linear_in_the_statement_size():
    import time
    fields = ", ".join(f"d.f{i} AS g{i}" for i in range(20000))
    text = f"SELECT {fields} FROM ks d"
    started = time.monotonic()
    assert first_difference(SQ, text, text.replace("SELECT", "select", 1)) is None
    assert time.monotonic() - started < 3.0  # was ~11 s (a slice per word)
