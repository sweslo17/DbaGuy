import pytest

from dbaguy_backend.domain.errors import ValidationFailed
from dbaguy_backend.domain.metadata import PathSegment
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.select_builder import SortKey, build_browse_select, relation_from_path

PG, LITE, MY = Dialect.POSTGRES, Dialect.SQLITE, Dialect.MYSQL


def path(*pairs):
    return [PathSegment(kind=k, name=n) for k, n in pairs]


PG_PATH = path(("database", "app"), ("schema", "sales"), ("folder", "tables"), ("table", "Order Items"))


def test_relation_from_path():
    assert relation_from_path(PG_PATH) == ("sales", "Order Items")
    assert relation_from_path(path(("schema", "main"), ("folder", "views"), ("view", "v"))) == ("main", "v")
    with pytest.raises(ValidationFailed):
        relation_from_path(path(("schema", "main"), ("folder", "tables")))


def test_build_browse_select():
    assert build_browse_select(PG, PG_PATH, None, []) == 'SELECT * FROM sales."Order Items"'
    sql = build_browse_select(PG, PG_PATH, " status = 'a;b'; ", [SortKey(column="Select", descending=True), SortKey(column="id")])
    assert sql == "SELECT * FROM sales.\"Order Items\"\nWHERE status = 'a;b'\nORDER BY \"Select\" DESC, id ASC"
    assert build_browse_select(LITE, path(("schema", "main"), ("folder", "tables"), ("table", "t")), "x > 1", [], count=True) == (
        "SELECT count(*) FROM main.t\nWHERE x > 1"
    )
    with pytest.raises(ValidationFailed):
        build_browse_select(PG, PG_PATH, "1=1; DROP TABLE t", [])


def test_blank_filter_means_no_where_and_count_ignores_order():
    assert build_browse_select(PG, PG_PATH, "  ;  ", []) == 'SELECT * FROM sales."Order Items"'
    assert build_browse_select(PG, PG_PATH, None, [SortKey(column="id")], count=True) == (
        'SELECT count(*) FROM sales."Order Items"'
    )


def test_partition_path():
    partition = path(("schema", "public"), ("table", "events"), ("folder", "partitions"), ("partition", "events_2026"))
    assert relation_from_path(partition) == ("public", "events_2026")


# --- the filter is user text appended to our SELECT: it must stay one expression, whatever the database's lexing ---

LITE_PATH = path(("schema", "main"), ("folder", "tables"), ("table", "t"))
MY_PATH = path(("schema", "dbaguy"), ("folder", "tables"), ("table", "Order Items"))
PG_BASE = 'SELECT * FROM sales."Order Items"\nWHERE '
LITE_BASE = "SELECT * FROM main.t\nWHERE "
MY_BASE = "SELECT * FROM dbaguy.`Order Items`\nWHERE "
PATHS = {PG: PG_PATH, LITE: LITE_PATH, MY: MY_PATH}
BASES = {PG: PG_BASE, LITE: LITE_BASE, MY: MY_BASE}


@pytest.mark.parametrize(
    "dialect, filter_text, expected",
    [
        # a `;` that is data, not syntax, is fine
        (PG, "s = 'a;b' AND t = 1", "s = 'a;b' AND t = 1"),
        (PG, 'x = 1 AND "a;b" = 2', 'x = 1 AND "a;b" = 2'),
        (PG, "body = $$a;b$$ OR body = $t$ ; $t$", "body = $$a;b$$ OR body = $t$ ; $t$"),
        (PG, r"s = E'\';' AND t = 1", r"s = E'\';' AND t = 1"),
        (LITE, "[a;b] = 1 AND `c;d` = 2", "[a;b] = 1 AND `c;d` = 2"),
        # comments never reach the database: each becomes one space, so the result cannot depend on where it ends
        (PG, "x = 1 /* ; DROP TABLE t */ AND y = 2", "x = 1   AND y = 2"),
        (PG, "x = 1 -- ; DROP TABLE t\n AND y = 2", "x = 1  \n AND y = 2"),
        (PG, "x = 1 -- trailing", "x = 1"),
        (PG, "x = 1 /* a /* nested ; */ b */", "x = 1"),
        (LITE, "x = 1 -- c\r; DROP TABLE t\n AND y = 2", "x = 1  \n AND y = 2"),  # SQLite: the comment runs to LF
        # MySQL: backslash escapes, double-quoted strings, backtick names and `#` comments
        (MY, r"s = 'a\';b' AND t = 1", r"s = 'a\';b' AND t = 1"),
        (MY, 'n = "x;y"', 'n = "x;y"'),
        (MY, "x = 1 # ; DROP TABLE t\n AND y = 2", "x = 1  \n AND y = 2"),
        (MY, "`a;b` = 1", "`a;b` = 1"),
    ],
)
def test_filter_with_semicolons_that_are_not_statement_ends(dialect, filter_text, expected):
    assert build_browse_select(dialect, PATHS[dialect], filter_text, []) == BASES[dialect] + expected


@pytest.mark.parametrize(
    "dialect, filter_text",
    [
        (PG, "x = 1; DROP TABLE t"),
        (PG, "x = 1;; DROP TABLE t"),
        (PG, "x = 1 -- note\n; DROP TABLE t"),
        (PG, "x = 1 -- c\r; DROP TABLE t\n AND y = 2"),  # PostgreSQL ends a line comment at CR
        (PG, "x = 1 /* c */; DROP TABLE t"),
        (PG, r"x = '\' ; DROP TABLE t; --"),  # a plain string has no backslash escapes
        (LITE, "x = 1 -- note\n; DROP TABLE t"),
        (LITE, "x = [a] ; DROP TABLE t"),
        (MY, "x = 1; DROP TABLE t"),
        (MY, r"x = 'a\'' ; DROP TABLE t"),
    ],
)
def test_filter_with_a_second_statement_is_rejected(dialect, filter_text):
    with pytest.raises(ValidationFailed, match="single expression"):
        build_browse_select(dialect, PATHS[dialect], filter_text, [])


@pytest.mark.parametrize(
    "dialect, filter_text",
    [
        (PG, "x = '"),
        (PG, "x = 'abc; DROP TABLE t"),
        (PG, "x = $$"),
        (PG, "x = $t$ open"),
        (PG, 'x = "'),
        (PG, 'x = "abc""'),
        (PG, "x = 1 /*"),
        (PG, "x = 1 /* a /* b */"),
        (PG, r"x = E'\'"),
        (LITE, "x = [abc"),
        (LITE, "x = `abc"),
        (LITE, "x = 1 /* open"),
        (MY, r"x = 'a\'"),
        (MY, 'x = "abc'),
        (MY, "x = `abc"),
    ],
)
def test_filter_with_an_unterminated_literal_or_comment_is_rejected(dialect, filter_text):
    # it would swallow whatever is appended after it, such as the ORDER BY built from a sort key
    sort = [SortKey(column="a'; SELECT 2 AS injected; --")]
    with pytest.raises(ValidationFailed, match="unterminated"):
        build_browse_select(dialect, PATHS[dialect], filter_text, sort)


def test_sort_keys_are_quoted_so_quotes_and_semicolons_stay_inside_the_identifier():
    sort = [SortKey(column="a'; SELECT 2 AS injected; --"), SortKey(column='b"; DROP TABLE t; --', descending=True)]
    assert build_browse_select(PG, PG_PATH, "x = 1", sort) == (
        PG_BASE + "x = 1\nORDER BY \"a'; SELECT 2 AS injected; --\" ASC, \"b\"\"; DROP TABLE t; --\" DESC"
    )
    assert build_browse_select(LITE, LITE_PATH, "x = 1", sort[:1]) == (
        LITE_BASE + "x = 1\nORDER BY \"a'; SELECT 2 AS injected; --\" ASC"
    )


@pytest.mark.parametrize("filter_text", ["x = 1 /*! OR 1 = 1 */", "x = 1 /*+ SET_VAR(sql_mode='') */"])
def test_executable_comments_are_not_allowed_in_a_filter(filter_text):
    with pytest.raises(ValidationFailed, match="Executable comments"):
        build_browse_select(MY, MY_PATH, filter_text, [])


def test_mysql_sort_keys_are_backtick_quoted():
    assert build_browse_select(MY, MY_PATH, None, [SortKey(column="a`b")]) == (
        "SELECT * FROM dbaguy.`Order Items`\nORDER BY `a``b` ASC"
    )


def test_a_filter_that_is_only_comments_or_semicolons_means_no_where():
    for text in ("-- nothing", "/* a */ ; /* b */", " ; ; "):
        assert build_browse_select(PG, PG_PATH, text, []) == 'SELECT * FROM sales."Order Items"'
