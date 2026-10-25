import pytest

from dbaguy_backend.domain.errors import ValidationFailed
from dbaguy_backend.domain.metadata import PathSegment
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.select_builder import SortKey, build_browse_select, relation_from_path

PG, LITE, MY, TS = Dialect.POSTGRES, Dialect.SQLITE, Dialect.MYSQL, Dialect.TSQL


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
TS_PATH = path(("database", "dbaguy"), ("schema", "dbo"), ("folder", "tables"), ("table", "Order Items"))
PG_BASE = 'SELECT * FROM sales."Order Items"\nWHERE '
LITE_BASE = "SELECT * FROM main.t\nWHERE "
MY_BASE = "SELECT * FROM dbaguy.`Order Items`\nWHERE "
TS_BASE = "SELECT * FROM dbo.[Order Items]\nWHERE "
PATHS = {PG: PG_PATH, LITE: LITE_PATH, MY: MY_PATH, TS: TS_PATH}
BASES = {PG: PG_BASE, LITE: LITE_BASE, MY: MY_BASE, TS: TS_BASE}


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
        # T-SQL: [names] with `;`, N'' strings, nested comments; CASE … ELSE … END and subqueries stay one expression
        (TS, "[a;b] = 1 AND n = N'x;y'", "[a;b] = 1 AND n = N'x;y'"),
        (TS, "x = 1 /* a /* nested ; */ b */ AND y = 2", "x = 1   AND y = 2"),
        (TS, "CASE WHEN a = 1 THEN 1 ELSE 0 END = 1", "CASE WHEN a = 1 THEN 1 ELSE 0 END = 1"),
        (TS, "a IN (SELECT b FROM u)", "a IN (SELECT b FROM u)"),
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
        # T-SQL needs no `;` before a second statement
        (TS, "x = 1; DROP TABLE t"),
        (TS, "x = 1 DELETE FROM t"),
        (TS, "x = 1\nGO\nDROP TABLE t"),
        (TS, "(x = 1) WAITFOR DELAY '00:00:10'"),
        (TS, "x = 1 IF 1 = 1 DISABLE TRIGGER tr ON t"),
        (TS, "x = a\u00a0DELETE FROM t"),  # SQL Server reads U+00A0 as a space
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
        (TS, "x = N'abc"),
        (TS, "x = [abc"),
        (TS, "x = 1 /* a /* b */"),
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


def test_a_tsql_filter_cannot_append_a_statement():  # Review Focus 1
    with pytest.raises(ValidationFailed, match="single expression"):
        build_browse_select(TS, TS_PATH, "id = 1 DELETE FROM dbo.job_log", [])


def test_tsql_sort_keys_are_bracket_quoted():
    assert build_browse_select(TS, TS_PATH, None, [SortKey(column="a]b")]) == (
        "SELECT * FROM dbo.[Order Items]\nORDER BY [a]]b] ASC"
    )


def test_sqlpp_browse_select_reads_the_collection_with_its_meta_id():  # D-04-11, Review Focus 5
    def path(*parts):
        kinds = ("bucket", "scope", "collection", "index")
        return [PathSegment(kind=kind, name=name) for kind, name in zip(kinds, parts)]

    assert build_browse_select(Dialect.SQLPP, path("travel-sample", "inventory", "airline"), None, []) == (
        "SELECT META(d).id, *\nFROM airline AS d")
    assert build_browse_select(Dialect.SQLPP, path("dbaguy", "app", "value"), None, []).endswith("FROM `value` AS d")
    assert build_browse_select(Dialect.SQLPP, path("dbaguy", "app", "odd-name"), None, []).endswith(
        "FROM `odd-name` AS d")
    assert build_browse_select(Dialect.SQLPP, path("dbaguy", "_default", "_default"), None, []).endswith(
        "FROM _default AS d")
    assert build_browse_select(Dialect.SQLPP, path("dbaguy", "app", "orders", "#primary"), None, []).endswith(
        "FROM orders AS d")
    for where, order, count in [("x = 1", [], False), (None, [SortKey(column="x")], False), (None, [], True)]:
        with pytest.raises(ValidationFailed, match="SQL\\+\\+ in a query tab"):
            build_browse_select(Dialect.SQLPP, path("dbaguy", "app", "orders"), where, order, count)


def test_a_dumps_count_and_its_filter_check():  # 06a Task 9: a table with no column a dump writes, a filter up front
    from dbaguy_backend.sql.select_builder import build_count_select, check_filter

    assert build_count_select(Dialect.POSTGRES, "s", "t", "id < 3", only=True) == (
        "SELECT COUNT(*) FROM ONLY s.t\nWHERE id < 3")
    assert build_count_select(Dialect.TSQL, "dbo", "o t") == "SELECT COUNT(*) FROM dbo.[o t]"
    assert check_filter(Dialect.MYSQL, "  -- nothing\n") is None and check_filter(Dialect.MYSQL, "a = 1;") == "a = 1"
    with pytest.raises(ValidationFailed):
        check_filter(Dialect.TSQL, "id = 1 DELETE FROM t")


# Task 9 fix round 2/3 (M2 gap 2): a filter is one condition. Each dialect's clause words are exactly those its own
# server parses after a WHERE condition (probed), so a word another dialect reserves is never refused here.
_ENDS = {
    PG: ["UNION", "INTERSECT", "EXCEPT", "FOR", "ORDER", "LIMIT", "OFFSET", "FETCH", "GROUP", "HAVING", "WINDOW"],
    MY: ["UNION", "INTERSECT", "EXCEPT", "FOR", "ORDER", "LIMIT", "GROUP", "HAVING", "WINDOW", "LOCK", "INTO"],
    TS: ["UNION", "INTERSECT", "EXCEPT", "ORDER", "GROUP", "HAVING", "OPTION", "FOR"],
    LITE: ["UNION", "INTERSECT", "EXCEPT", "ORDER", "LIMIT", "GROUP", "HAVING"],
}


@pytest.mark.parametrize("dialect", [PG, MY, TS, LITE])
def test_a_word_that_ends_the_where_clause_is_refused_outside_parentheses(dialect):
    from dbaguy_backend.sql.select_builder import FILTER_NOT_A_CONDITION, check_filter

    for word in _ENDS[dialect]:
        for shape in (f"1=1 {word} x", f"(a = 1)\n{word} 1"):
            with pytest.raises(ValidationFailed) as refused:
                check_filter(dialect, shape)
            # T-SQL: a word that starts a statement (FOR …) is a second statement to its cutter first
            assert refused.value.message == FILTER_NOT_A_CONDITION or (
                dialect is TS and "another T-SQL statement" in refused.value.message), shape
    for shape in ("x) UNION ALL SELECT 1 WHERE (1", "a = 1) OR (b = 2", ")"):  # a ")" it did not open: never
        with pytest.raises(ValidationFailed):
            check_filter(dialect, shape)
    with pytest.raises(ValidationFailed):
        build_browse_select(dialect, PATHS[dialect], "1=1 UNION ALL SELECT 99, 'fake'", [])  # one check, browsing too


@pytest.mark.parametrize("dialect, word", [  # not a clause starter in this dialect: a plain column name is fine
    (PG, "qualify"), (PG, "returning"), (PG, "into"), (TS, "limit"), (TS, "offset"), (TS, "window"),
    (TS, "fetch"), (TS, "returning"), (TS, "qualify"), (LITE, "window"), (LITE, "offset"), (LITE, "fetch"),
    (LITE, "for"), (MY, "offset"), (MY, "qualify"), (MY, "returning"),
])
def test_a_word_this_dialect_does_not_start_a_clause_with_is_a_plain_column(dialect, word):
    from dbaguy_backend.sql.select_builder import check_filter

    assert check_filter(dialect, f"{word} = 1") == f"{word} = 1"


@pytest.mark.parametrize("dialect, condition", [  # Task 9 fix round 3: a number glued to a keyword (SQL Server)
    (TS, "id=0x1UNION ALL SELECT 99"), (TS, "id=0xUNION ALL SELECT 99"), (TS, "id=0x1FUNION ALL SELECT 99"),
    (TS, "id=1eUNION ALL SELECT 99"), (TS, "id=1.eUNION ALL SELECT 99"), (TS, "id=.5eUNION ALL SELECT 99"),
    (TS, "id=1e+UNION ALL SELECT 99"), (TS, "id=$1UNION ALL SELECT 99"), (TS, "id=0x1ORDER BY id"),
    (TS, "id=0x1OPTION (MAXDOP 1)"),
])
def test_a_number_glued_to_a_clause_word_is_refused_on_sql_server(dialect, condition):
    from dbaguy_backend.sql.select_builder import check_filter

    with pytest.raises(ValidationFailed):
        check_filter(dialect, condition)


@pytest.mark.parametrize("dialect, condition", [
    (PG, "substring(x FOR 3) = 'abc'"), (PG, "substring(x FROM 1 FOR 3) = 'abc'"),
    (PG, "x IN (SELECT y FROM t ORDER BY y LIMIT 1)"), (MY, "x IN (SELECT y FROM t ORDER BY y LIMIT 1)"),
    (LITE, "x IN (SELECT y FROM t ORDER BY y LIMIT 1 OFFSET 2)"), (TS, "x IN (SELECT TOP 1 y FROM t ORDER BY y)"),
    (MY, "`order` = 1"), (PG, '"union" = 1'), (LITE, '"for" = 1'), (TS, "[group] = 1"), (PG, "'for' = v"),
    (MY, "v = 'x UNION SELECT 1'"), (PG, "x = 1 /* ORDER BY y */"),
    (PG, "x IN (SELECT a FROM u UNION SELECT b FROM w)"),
    (PG, "option = 1 AND procedure = 2 AND lock = 3 AND minus = 4"),  # plain names in PostgreSQL
    (LITE, "option = 1 AND lock = 2"),
    # the Task 9 re-review's ordinary filters
    (MY, "REPLACE(v, 'a', 'b') = 'x'"), (MY, "INSERT(v, 1, 1, 'x') = 'y'"), (MY, "FIND_IN_SET('a', v) > 0"),
    (MY, "v LIKE '%update%'"), (MY, "`update` = 1"), (MY, "id IN (SELECT id FROM t2 WHERE x = 1)"),
    (MY, "created > NOW() - INTERVAL 1 DAY"), (MY, "JSON_EXTRACT(j, '$.a') = 1"), (MY, "v REGEXP '^a'"),
    (MY, "MATCH(v) AGAINST ('x')"), (MY, "`delete` IS NULL"), (MY, "`share` = 1"),
    (MY, "v = 'x' COLLATE utf8mb4_bin"), (MY, "CAST(v AS CHAR) = '1'"), (MY, "values_col = 1"), (MY, "`into` = 1"),
    (PG, "replace(v, 'a', 'b') = 'x'"), (PG, "v ~ 'update'"), (PG, '"update" = 1'),
    (PG, "created > now() - interval '1 day'"), (PG, "j->>'a' = '1'"), (PG, "id = ANY(ARRAY[1,2])"),
    (PG, "v ILIKE 'a%'"), (PG, "lower(v) = 'a'"), (PG, "tsv @@ to_tsquery('x')"),
    (PG, "EXISTS (SELECT 1 FROM t2 WHERE t2.id = t.id)"), (PG, "v IS DISTINCT FROM 'a'"), (PG, "coalesce(v, '') = ''"),
    (PG, '"into" = 1'), (PG, '"set_config_note" = 1'), (PG, "v = 'set_config'"),
    (TS, "REPLACE(v, 'a', 'b') = 'x'"), (TS, "v LIKE '%update%'"), (TS, "[update] = 1"),
    (TS, "created > DATEADD(day, -1, GETDATE())"), (TS, "JSON_VALUE(j, '$.a') = '1'"), (TS, "TRY_CAST(v AS int) = 1"),
    (TS, "id IN (SELECT id FROM dbo.t2)"), (TS, "[into] = 1"), (TS, "CONTAINS(v, 'x')"),
    (TS, "v = N'x' COLLATE Latin1_General_BIN"), (TS, "IIF(v = 'a', 1, 0) = 1"),
    (LITE, "replace(v, 'a', 'b') = 'x'"), (LITE, "v GLOB 'a*'"), (LITE, "json_extract(j, '$.a') = 1"),
    (LITE, '"update" = 1'), (LITE, "rowid > 10"), (LITE, "v LIKE 'a%' ESCAPE '\\'"),
    (LITE, "id IN (SELECT id FROM t2)"),
    (LITE, "typeof(v) = 'text'"),
])
def test_an_ordinary_condition_is_never_refused(dialect, condition):
    from dbaguy_backend.services.safety import is_plain_read
    from dbaguy_backend.sql.select_builder import build_count_select, check_filter

    assert check_filter(dialect, condition) is not None
    assert is_plain_read(build_count_select(dialect, "s", "t", condition), dialect)
