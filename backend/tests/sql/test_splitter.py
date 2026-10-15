from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.splitter import split

PG, LITE = Dialect.POSTGRES, Dialect.SQLITE


def texts(sql, dialect=PG):
    return [s.text for s in split(sql, dialect)]


def test_simple_and_leading_comments():
    sql = "-- first\nSELECT 1;\n\n/* two */ SELECT 2 ;  SELECT 3"
    spans = split(sql, PG)
    assert [s.text for s in spans] == ["SELECT 1", "SELECT 2", "SELECT 3"]
    assert sql[spans[0].start:spans[0].terminator_end] == "SELECT 1;"
    assert spans[2].terminator_end == spans[2].end == len(sql)


def test_empty_statements_and_comment_only_tail_are_dropped():
    assert texts(";;  ; -- only a comment\n") == []
    assert texts("SELECT 1; -- trailing note") == ["SELECT 1"]


def test_semicolons_inside_literals_identifiers_comments():
    sql = "SELECT 'a;b', \"c;d\" /* e;f */ FROM t; SELECT E'g\\';h'"
    assert texts(sql) == ["SELECT 'a;b', \"c;d\" /* e;f */ FROM t", "SELECT E'g\\';h'"]


def test_postgres_dollar_quoted_function_bodies():
    fn = "CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $fn$ BEGIN RETURN 1; END; $fn$"
    assert texts(f"{fn}; SELECT f();") == [fn, "SELECT f()"]
    assert texts("DO $$ BEGIN RAISE NOTICE 'x;y'; END $$; SELECT 1") == [
        "DO $$ BEGIN RAISE NOTICE 'x;y'; END $$", "SELECT 1",
    ]


def test_postgres_begin_atomic_with_case_inside():
    fn = (
        "CREATE FUNCTION g(x int) RETURNS int LANGUAGE sql BEGIN ATOMIC "
        "SELECT CASE WHEN x > 0 THEN 1 ELSE 0 END; SELECT 2; END"
    )
    assert texts(f"{fn}; SELECT g(1)") == [fn, "SELECT g(1)"]


def test_transaction_begin_and_end_do_not_open_bodies():
    assert texts("BEGIN; UPDATE t SET a = 1; END; BEGIN TRANSACTION; COMMIT") == [
        "BEGIN", "UPDATE t SET a = 1", "END", "BEGIN TRANSACTION", "COMMIT",
    ]


def test_sqlite_trigger_body():
    trg = "CREATE TEMP TRIGGER trg AFTER INSERT ON t BEGIN UPDATE c SET n = n + 1; SELECT CASE WHEN 1 THEN 2 END; END"
    assert texts(f"{trg}; SELECT 1;", LITE) == [trg, "SELECT 1"]
    assert texts("BEGIN; SELECT 1; COMMIT", LITE) == ["BEGIN", "SELECT 1", "COMMIT"]


def test_nested_comments_only_in_postgres():
    assert texts("/* a /* ; */ still comment */ SELECT 1") == ["SELECT 1"]
    # In SQLite the first */ closes the comment, so "still comment */ SELECT 1" is one (bogus) statement.
    assert texts("/* a /* ; */ x */ SELECT 1", LITE) == ["x */ SELECT 1"]


def test_offsets_with_cjk_and_emoji():
    sql = "SELECT '中😀';\nSELECT 2"
    spans = split(sql, PG)
    assert [sql[s.start:s.end] for s in spans] == ["SELECT '中😀'", "SELECT 2"]


def test_unterminated_string_swallows_rest():
    assert texts("SELECT 'abc; SELECT 2") == ["SELECT 'abc; SELECT 2"]
