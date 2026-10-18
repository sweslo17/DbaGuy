import pytest

from dbaguy_backend.sql.dialects import Dialect, force_quote_ident, quote_literal
from tests.support import couchbase_query, requires_couchbase

pytestmark = requires_couchbase
SQ = Dialect.SQLPP


@pytest.mark.parametrize("value", [
    "it's", 'say "hi"', "back\\slash", "tab\tnew\nline\rcr", "\b\f", "\x00\x1f", "/", "中文😀", "",
])
def test_string_literals_round_trip(value):  # A4
    assert couchbase_query(f"SELECT RAW {quote_literal(SQ, value)}")["results"] == [value]


@pytest.mark.parametrize("name", ["a\\b", "a`b", "x.y", "value", "中文"])
def test_quoted_names_round_trip(name):  # A5: `\\` is one backslash, `` is one back-tick
    statement = f"SELECT RAW o.{force_quote_ident(SQ, name)} FROM [{{{quote_literal(SQ, name)}: 1}}] AS o"
    assert couchbase_query(statement)["results"] == [1]
