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


def test_line_comment_ends_at_cr_in_postgres_only():
    assert texts("SELECT 1 -- c\r; SELECT 2") == ["SELECT 1", "SELECT 2"]
    assert texts("SELECT 1 -- c\r; SELECT 2", LITE) == ["SELECT 1"]  # in SQLite the comment runs to the next LF


def test_begin_atomic_opens_a_body_only_in_a_routine_and_only_as_adjacent_words():
    # Column names: no CREATE FUNCTION/PROCEDURE, so no body (the DROP used to hide inside the SELECT).
    assert texts("SELECT started AS begin, atomic FROM jobs; DROP TABLE t") == [
        "SELECT started AS begin, atomic FROM jobs", "DROP TABLE t",
    ]
    # A routine, but BEGIN and ATOMIC are not adjacent tokens.
    assert texts("CREATE FUNCTION f() RETURNS int LANGUAGE sql RETURN begin.atomic; DROP TABLE t") == [
        "CREATE FUNCTION f() RETURNS int LANGUAGE sql RETURN begin.atomic", "DROP TABLE t",
    ]


def test_begin_atomic_bodies_of_functions_and_procedures():
    fn = "CREATE FUNCTION f() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT 1; SELECT 2; END"
    proc = "CREATE OR REPLACE PROCEDURE p() LANGUAGE sql BEGIN /* c */ ATOMIC INSERT INTO t VALUES (1); END"
    assert texts(f"{fn}; {proc}; SELECT 3") == [fn, proc, "SELECT 3"]
    assert [s.has_body for s in split(f"{fn}; {proc}; SELECT 3", PG)] == [True, True, False]


def test_sqlite_trigger_bodies_open_only_in_create_trigger():
    assert texts("CREATE TABLE t (trigger int, begin int); DELETE FROM t", LITE) == [
        "CREATE TABLE t (trigger int, begin int)", "DELETE FROM t",
    ]
    assert texts("CREATE VIEW v AS SELECT 1 AS trigger, 2 AS begin; DELETE FROM t", LITE) == [
        "CREATE VIEW v AS SELECT 1 AS trigger, 2 AS begin", "DELETE FROM t",
    ]
    assert texts("CREATE TABLE trigger_log (id int); DROP TRIGGER x; BEGIN; DELETE FROM t", LITE) == [
        "CREATE TABLE trigger_log (id int)", "DROP TRIGGER x", "BEGIN", "DELETE FROM t",
    ]


def test_sqlite_trigger_forms_and_a_case_in_the_when_clause():
    temp = "CREATE TEMPORARY TRIGGER IF NOT EXISTS tr AFTER INSERT ON t BEGIN UPDATE c SET n = 1; DELETE FROM d; END"
    when = (
        "CREATE TRIGGER tr AFTER UPDATE ON t WHEN CASE WHEN new.a > 0 THEN 1 ELSE 0 END "
        "BEGIN UPDATE c SET n = n + 1; END"
    )
    spans = split(f"{temp}; {when}; SELECT 1", LITE)
    assert [s.text for s in spans] == [temp, when, "SELECT 1"]
    assert [s.has_body for s in spans] == [True, True, False]
