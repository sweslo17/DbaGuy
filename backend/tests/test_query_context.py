import pytest

from dbaguy_backend.domain.execution import QueryContext as Q
from dbaguy_backend.sql.dialects import Dialect, force_quote_ident


@pytest.mark.parametrize("text, expected", [
    ("travel-sample", Q(bucket="travel-sample")),
    ("travel-sample.inventory", Q(bucket="travel-sample", scope="inventory")),
    ("my.bucket.app", Q(bucket="my.bucket", scope="app")),  # the last dot: scope names hold no dot
    ("`my.bucket`", Q(bucket="my.bucket")),
    ("`my.bucket`.app", Q(bucket="my.bucket", scope="app")),
    ("`b`.`s`", Q(bucket="b", scope="s")),
    ("default:dbaguy.app", Q(bucket="dbaguy", scope="app")),
    ("DEFAULT:`a``b`", Q(bucket="a`b")),
    ("  dbaguy  ", Q(bucket="dbaguy")),
    ("", None), (None, None), ("   ", None),
])
def test_parse(text, expected):
    assert Q.parse(text) == expected


@pytest.mark.parametrize("text", ["`a", "a.", ".a", "`a`b", "a.b.`c", "`a`.`b`.`c`", "a..b"])
def test_malformed(text):
    with pytest.raises(ValueError):
        Q.parse(text)


@pytest.mark.parametrize("context, text", [
    (Q(bucket="travel-sample", scope="inventory"), "travel-sample.inventory"),
    (Q(bucket="dbaguy"), "dbaguy"),
    (Q(bucket="my.bucket"), "`my.bucket`"),
    (Q(bucket="my.bucket", scope="app"), "my.bucket.app"),
    (Q(bucket="a`b", scope="s"), "`a``b`.s"),
])
def test_text_round_trips(context, text):
    assert context.text() == text and Q.parse(text) == context


_NAMES = ["a", "travel-sample", "a.b", "a.", ".a", "a..b", "a`b", "a\\b", "a`\\b", " a", "a ", "default:x", "DEFAULT:"]


@pytest.mark.parametrize("bucket", _NAMES)
@pytest.mark.parametrize("scope", [None, *_NAMES])
def test_every_context_reads_back(bucket, scope):
    """What the history keeps must parse back to the same context, whatever the names hold."""
    context = Q(bucket=bucket, scope=scope)
    assert Q.parse(context.text()) == context


@pytest.mark.parametrize("bucket", ["a.b", "a`b", "a`\\b", "x`.y"])
def test_a_quoted_name_is_quoted_as_sqlpp_quotes_it(bucket):
    """The domain cannot import sql/, so `text()` quotes by itself: the same way as the dialect."""
    assert Q(bucket=bucket).text() == force_quote_ident(Dialect.SQLPP, bucket)

