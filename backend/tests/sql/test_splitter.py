import time

import pytest

from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.splitter import split

PG, LITE, MY = Dialect.POSTGRES, Dialect.SQLITE, Dialect.MYSQL


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


def test_mysql_delimiter_lines_are_consumed_and_change_the_terminator():
    sql = (
        "DELIMITER $$\n"
        "CREATE PROCEDURE p()\nBEGIN\n  SELECT 1;\n  SELECT 2;\nEND$$\n"
        "DELIMITER ;\n"
        "CALL p();\nSELECT 3"
    )
    spans = split(sql, MY)
    assert [s.text for s in spans] == ["CREATE PROCEDURE p()\nBEGIN\n  SELECT 1;\n  SELECT 2;\nEND", "CALL p()", "SELECT 3"]
    assert sql[spans[0].end:spans[0].terminator_end] == "$$"
    assert spans[0].has_body and not spans[1].has_body
    assert sql[spans[1].start:spans[1].terminator_end] == "CALL p();"


def test_a_custom_delimiter_ends_statements_only_outside_literals_and_comments():
    sql = "delimiter //\nSELECT 'a//b'; SELECT 2 /* // */ //\nSELECT 3 // -- the rest is a comment\nSELECT `x//y`//"
    # a span ends at its last significant token: the trailing comment stays outside, as for `;`
    assert texts(sql, MY) == ["SELECT 'a//b'; SELECT 2", "SELECT 3", "SELECT `x//y`"]


def test_delimiter_is_a_command_only_first_on_its_line_at_a_statement_start():
    assert texts("SELECT 1; DELIMITER $$\nSELECT 2", MY) == ["SELECT 1", "DELIMITER $$\nSELECT 2"]
    assert texts("SELECT delimiter FROM t; SELECT 2", MY) == ["SELECT delimiter FROM t", "SELECT 2"]
    assert texts("SELECT 1\nDELIMITER $$\n", MY) == ["SELECT 1\nDELIMITER $$"]  # inside a statement: SQL, not a command
    assert texts("  -- setup\n  DELIMITER $$  -- comment\nSELECT 1$$", MY) == ["SELECT 1"]


_PROC = (
    "CREATE DEFINER=`root`@`%` PROCEDURE p(IN n INT)\n"
    "lbl: BEGIN\n"
    "  DECLARE i INT DEFAULT 0;\n"
    "  WHILE i < n DO SET i = i + 1; END WHILE;\n"
    "  IF i > 1 THEN SELECT 'x;y'; ELSEIF i = 1 THEN SELECT 1; ELSE SELECT 2; END IF;\n"
    "  CASE i WHEN 1 THEN SELECT 1; ELSE BEGIN SELECT 2; END; END CASE;\n"
    "  REPEAT SET i = i - 1; UNTIL i = 0 END REPEAT;\n"
    "  inner_loop: LOOP LEAVE inner_loop; END LOOP inner_loop;\n"
    "  SELECT CASE WHEN i = 0 THEN 'z' END;\n"
    "END lbl"
)


def test_mysql_compound_bodies_keep_their_semicolons_without_delimiter():
    spans = split(f"{_PROC};\nSELECT 9;", MY)
    assert [s.text for s in spans] == [_PROC, "SELECT 9"]
    assert spans[0].has_body


def test_mysql_trigger_event_and_function_bodies():
    trg = "CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW BEGIN SET NEW.a = 1; SET NEW.b = 2; END"
    ev = "CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO BEGIN DELETE FROM log; END"
    fn = "CREATE FUNCTION f(x INT) RETURNS TEXT DETERMINISTIC BEGIN RETURN IF(x > 0, REPEAT('a', x), ''); END"
    assert texts(f"{trg}; {ev}; {fn}; SELECT 1", MY) == [trg, ev, fn, "SELECT 1"]


def test_mysql_nothing_else_opens_a_body():
    assert texts("CREATE TABLE t (event INT, begin INT); DROP TABLE x", MY) == [
        "CREATE TABLE t (event INT, begin INT)", "DROP TABLE x",
    ]
    assert texts("BEGIN; INSERT INTO t VALUES (1); COMMIT", MY) == ["BEGIN", "INSERT INTO t VALUES (1)", "COMMIT"]
    trg = "CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW SET NEW.begin = 1"  # a column named begin
    assert texts(f"{trg}; DROP TABLE x", MY) == [trg, "DROP TABLE x"]
    assert texts("CREATE PROCEDURE p() SELECT 1; DROP TABLE x", MY) == ["CREATE PROCEDURE p() SELECT 1", "DROP TABLE x"]


def test_mysql_strings_and_comments_hide_semicolons():
    sql = "SELECT 'x\\'; DROP TABLE t; -- ' AS a; SELECT \"q;\" # c; d\n, 2; SELECT 1--1; SELECT 3"
    assert texts(sql, MY) == [
        "SELECT 'x\\'; DROP TABLE t; -- ' AS a", "SELECT \"q;\" # c; d\n, 2", "SELECT 1--1", "SELECT 3",
    ]
    # PostgreSQL has no backslash escapes in plain strings: the same text splits where its server splits it
    assert texts("SELECT 'x\\'; DROP TABLE t", PG) == ["SELECT 'x\\'", "DROP TABLE t"]


def test_mysqldump_style_script_with_crlf():  # Review Focus 2
    dump = (
        "/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;\r\n"
        "DELIMITER ;;\r\n"
        "/*!50003 CREATE*/ /*!50017 DEFINER=`root`@`localhost`*/ /*!50003 TRIGGER trg BEFORE INSERT ON t FOR EACH ROW "
        "BEGIN SET NEW.a = 1; END */;;\r\n"
        "DELIMITER ;\r\n"
        "INSERT INTO t VALUES (1);\r\n"
    )
    assert texts(dump, MY) == [
        "/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */",
        "/*!50003 CREATE*/ /*!50017 DEFINER=`root`@`localhost`*/ /*!50003 TRIGGER trg BEFORE INSERT ON t FOR EACH ROW "
        "BEGIN SET NEW.a = 1; END */",
        "INSERT INTO t VALUES (1)",
    ]


def test_offsets_under_a_custom_delimiter_with_cjk_and_emoji():
    sql = "DELIMITER $$\nSELECT '中😀'$$\nDELIMITER ;\nSELECT 2;"
    spans = split(sql, MY)
    assert [sql[s.start:s.end] for s in spans] == ["SELECT '中😀'", "SELECT 2"]
    assert sql[spans[0].end:spans[0].terminator_end] == "$$"


def test_executable_comments_are_statements_in_mysql_only():
    assert texts("/*!40101 SET NAMES utf8 */; SELECT 1", MY) == ["/*!40101 SET NAMES utf8 */", "SELECT 1"]
    assert texts("/*! x */; SELECT 1", PG) == ["SELECT 1"]


def test_mysql_executable_comments_in_a_stored_program_head_are_looked_past():
    # mysqldump version-gates the DEFINER clause; the rest of the program is plain or inside a comment too
    proc = "CREATE /*!50003 DEFINER=`u`@`h` */ PROCEDURE p() BEGIN SELECT 1; SELECT 2; END"
    spans = split(f"{proc}; SELECT 3", MY)
    assert [s.text for s in spans] == [proc, "SELECT 3"]
    assert spans[0].has_body
    both = (
        "CREATE /*!50003 DEFINER=`u`@`h` */ /*!50003 SQL SECURITY INVOKER */ "
        "FUNCTION f() RETURNS INT BEGIN RETURN 1; END"
    )
    assert texts(f"{both}; SELECT 3", MY) == [both, "SELECT 3"]
    hidden = "CREATE /*!50003 DEFINER=`u`@`h` */ /*!50003 PROCEDURE p() BEGIN SELECT 1; SELECT 2; END */"
    assert texts(f"{hidden}; SELECT 3", MY) == [hidden, "SELECT 3"]


def test_mysql_a_word_after_a_dot_is_a_name_never_a_block_keyword():
    trg = (
        "CREATE TRIGGER trg BEFORE UPDATE ON ev FOR EACH ROW "
        "BEGIN IF NEW.end < NEW.start THEN SET NEW.end = NEW.start; END IF; SELECT ev.end FROM ev; END"
    )
    assert texts(f"{trg}; SELECT 1", MY) == [trg, "SELECT 1"]


def test_mysql_an_unqualified_end_inside_a_body_is_an_identifier():  # R-03a-T2: never a half statement
    proc = "CREATE PROCEDURE p() BEGIN SELECT start, end FROM ev; DELETE FROM x; END"
    spans = split(f"{proc}; SELECT 3", MY)
    assert [s.text for s in spans] == [proc, "SELECT 3"]
    assert spans[0].has_body
    body = (
        "CREATE PROCEDURE p() BEGIN SELECT end FROM ev; UPDATE ev SET end = 1 WHERE end > 2; SET @x = end; "
        "SELECT IF(end, 1, 2), (end); DELETE FROM x; END"
    )
    assert texts(f"{body}; SELECT 3", MY) == [body, "SELECT 3"]


def test_mysql_end_closes_a_block_only_after_a_semicolon_or_an_opening_begin():
    empty = "CREATE PROCEDURE p() lbl: BEGIN BEGIN END; DELETE FROM x; BEGIN END; END lbl"
    assert texts(f"{empty}; SELECT 3", MY) == [empty, "SELECT 3"]
    # a CASE expression closes with a bare END, inside a body and next to an identifier named end
    case = (
        "CREATE PROCEDURE p() BEGIN SELECT CASE WHEN a THEN 1 ELSE CASE WHEN b THEN 2 END END AS c, end FROM ev; "
        "DELETE FROM x; END"
    )
    assert texts(f"{case}; SELECT 3", MY) == [case, "SELECT 3"]
    # IF / WHILE / LOOP / REPEAT inside a CASE statement and inside a BEGIN block: their END takes nothing with it
    nested = (
        "CREATE PROCEDURE p() BEGIN CASE i WHEN 1 THEN IF x THEN SELECT 1; END IF; WHILE y DO SELECT 2; END WHILE; "
        "ELSE BEGIN IF z THEN SELECT 3; END IF; END; END CASE; DELETE FROM x; END"
    )
    assert texts(f"{nested}; SELECT 3", MY) == [nested, "SELECT 3"]


@pytest.mark.parametrize(
    "account",
    [
        "sql@localhost", "`sql`@`%`", "'sql'@'%'", "CURRENT_USER", "CURRENT_USER()", "procedure@h", "''@'%'",
        "`u` @ `h`", "u@h",
    ],
)
def test_mysql_definer_accounts_in_every_form(account):
    proc = f"CREATE DEFINER={account} PROCEDURE p() BEGIN SELECT 1; SELECT 2; END"
    spans = split(f"{proc}; SELECT 3", MY)
    assert [s.text for s in spans] == [proc, "SELECT 3"]
    assert spans[0].has_body


def test_mysql_the_definer_skip_stops_at_the_end_of_the_account():
    view = "CREATE DEFINER=u@h VIEW v AS SELECT event, begin FROM t"
    spans = split(f"{view}; SELECT 2", MY)
    assert [s.text for s in spans] == [view, "SELECT 2"]
    assert not spans[0].has_body
    secure = "CREATE DEFINER=`u`@`h` SQL SECURITY INVOKER VIEW v AS SELECT procedure_name, begin FROM t"
    assert texts(f"{secure}; SELECT 2", MY) == [secure, "SELECT 2"]


def test_mysql_delimiter_edge_lines():
    # no argument: not a command, so the server gets (and rejects) it as it is
    assert texts("DELIMITER\nSELECT 1; SELECT 2", MY) == ["DELIMITER\nSELECT 1", "SELECT 2"]
    assert texts("DELIMITER  \nSELECT 1", MY) == ["DELIMITER  \nSELECT 1"]
    # at the end of the text, with and without a line break
    assert texts("SELECT 1;\nDELIMITER $$", MY) == ["SELECT 1"]
    assert texts("SELECT 1;\nDELIMITER $$\n", MY) == ["SELECT 1"]
    assert texts("DELIMITER $$\nSELECT 1$$\nDELIMITER ;", MY) == ["SELECT 1"]
    # any letter case
    assert texts("Delimiter $$\nSELECT 1; SELECT 2$$\nDELIMITER ;\nSELECT 3", MY) == ["SELECT 1; SELECT 2", "SELECT 3"]
    assert texts("dElImItEr //\nSELECT 1//", MY) == ["SELECT 1"]


def test_mysql_an_identifier_named_end_inside_a_case_never_closes_an_outer_block():
    stmt_case = (  # a CASE statement: its branches hold statements, one of them reads a column named end
        "CREATE PROCEDURE p() BEGIN CASE x WHEN 1 THEN SELECT end FROM ev; ELSE SELECT 2; END CASE; "
        "DELETE FROM x; END"
    )
    expr_in_stmt = (
        "CREATE PROCEDURE p() BEGIN CASE x WHEN 1 THEN SELECT CASE WHEN end THEN 1 END; END CASE; DELETE FROM x; END"
    )
    nested = (  # a CASE statement right after THEN and ELSE, in an IF
        "CREATE PROCEDURE p() BEGIN IF a THEN CASE WHEN end THEN SELECT end; END CASE; "
        "ELSE CASE b WHEN 1 THEN SET v = end; END CASE; END IF; DELETE FROM x; END"
    )
    in_expr = (
        "CREATE PROCEDURE p() BEGIN SELECT CASE WHEN end THEN end ELSE 0 END; "
        "SELECT CASE WHEN a THEN CASE WHEN b THEN end END ELSE end END; DELETE FROM x; END"
    )
    for proc in (stmt_case, expr_in_stmt, nested, in_expr):
        assert texts(f"{proc}; SELECT 3", MY) == [proc, "SELECT 3"], proc


def test_mysql_what_the_tracker_cannot_tell_apart_merges_and_never_splits_a_body():
    # a name `begin` inside the body, and a CASE that opens after a name `end` closed its CASE expression: the
    # statement keeps a block open and merges with what follows (the server rejects that), it is never cut inside
    for proc in (
        "CREATE PROCEDURE p() BEGIN SELECT begin FROM ev; DELETE FROM x; END",
        "CREATE PROCEDURE p() BEGIN SELECT CASE WHEN end THEN CASE a WHEN 1 THEN 2 END END; DELETE FROM x; END",
    ):
        first = split(f"{proc}; SELECT 3", MY)[0]
        assert first.end >= len(proc), proc


@pytest.mark.parametrize("text", [
    "delimiter;" * 40_000,  # the word at every statement start, never first on its line: 3.4 s before M5
    ";delimiter x" * 33_000,
    "\t delimiter;" * 30_000,
    "/*c*/" * 40_000 + " delimiter" * 40_000,  # comments before the statement starts
], ids=["bare", "with argument", "spaces before", "comments first"])
def test_delimiter_lines_are_found_in_linear_time_on_one_long_line(text):  # M5: /sql/split runs on every edit
    started = time.perf_counter()
    split(text, MY)
    assert time.perf_counter() - started < 1.0  # ~0.2 s here; 3.4 s for the first input before M5


TS = Dialect.TSQL


def batches(sql):
    return [(s.text, s.repeat) for s in split(sql, TS)]


def test_go_lines_end_batches_and_semicolons_do_not():
    sql = "DECLARE @x int = 1;\nSELECT @x;\nGO\nPRINT 'two'\n  go 3  -- three times\nSELECT 1; SELECT 2"
    assert batches(sql) == [("DECLARE @x int = 1;\nSELECT @x;", 1), ("PRINT 'two'", 3), ("SELECT 1; SELECT 2", 1)]
    first, second, _ = split(sql, TS)
    assert sql[first.end:first.terminator_end] == "\nGO"
    assert sql[second.end:second.terminator_end] == "\n  go 3  -- three times"


def test_what_is_not_a_go_line():
    for sql in ("SELECT 1\nGO;\nSELECT 2", "SELECT 1\nGO2\nSELECT 2", "SELECT 1\nGO /* c */\nSELECT 2", "SELECT 1 GO\nSELECT 2"):
        assert batches(sql) == [(sql, 1)], sql
    assert batches("SELECT 'a\nGO\nb'\nGO") == [("SELECT 'a\nGO\nb'", 1)]
    assert batches("/* a /* b */\nGO\n*/ SELECT 1\nGO") == [("SELECT 1", 1)]  # nested comment: still inside it
    assert batches("SELECT [a\nGO\nb] FROM t") == [("SELECT [a\nGO\nb] FROM t", 1)]
    assert batches("SELECT N'x\nGO\n' AS s") == [("SELECT N'x\nGO\n' AS s", 1)]


def test_go_edge_cases():
    assert batches("SELECT 1\r\nGO\r\nSELECT 2\r\n") == [("SELECT 1", 1), ("SELECT 2", 1)]
    assert batches("SELECT 1 -- x\nGO\n") == [("SELECT 1", 1)]  # a trailing comment stays outside, as for `;`
    assert batches("GO\nGO 5\n  \nSELECT 1\nGO") == [("SELECT 1", 1)]  # empty batches give no span
    assert batches("SELECT 1\nGO 0\nSELECT 2") == [("SELECT 1", 0), ("SELECT 2", 1)]
    assert batches("SELECT 1\nGo 02") == [("SELECT 1", 2)]
    sql = "SELECT N'中😀'\nGO\nSELECT 2"
    assert [sql[s.start:s.end] for s in split(sql, TS)] == ["SELECT N'中😀'", "SELECT 2"]


def test_module_definitions_own_the_rest_of_their_batch():
    sql = "CREATE OR ALTER PROCEDURE p AS\nSELECT 1;\nDELETE FROM t;\nGO\nEXEC p\nGO\nALTER VIEW v AS SELECT 1 AS a"
    spans = split(sql, TS)
    assert [(s.has_body, s.repeat) for s in spans] == [(True, 1), (False, 1), (True, 1)]
    assert split("/* header */ CREATE PROC q AS SELECT 1", TS)[0].has_body
    assert not split("CREATE TABLE t (a int); CREATE VIEW v AS SELECT 1", TS)[0].has_body  # not first in the batch
    assert [s.repeat for s in split("SELECT 1; SELECT 2", PG)] == [1, 1]  # other dialects: always 1


def test_a_huge_go_count_does_not_crash_the_splitter():
    assert batches("SELECT 1\nGO " + "9" * 5000) == [("SELECT 1", 10**9)]  # int() alone raises over 4300 digits
    assert batches("SELECT 1\nGO 000000000000000000000000000003") == [("SELECT 1", 3)]  # leading zeros are no digits
    assert batches("SELECT 1\nGO 00000000000") == [("SELECT 1", 0)]
    assert batches("SELECT 1\nGO 999999999") == [("SELECT 1", 999_999_999)]
