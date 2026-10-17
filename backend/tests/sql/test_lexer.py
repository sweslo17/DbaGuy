import pytest

from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.lexer import TokenKind as K
from dbaguy_backend.sql.lexer import INSIGNIFICANT, first_keyword, scan, tokenize

PG, LITE, MY = Dialect.POSTGRES, Dialect.SQLITE, Dialect.MYSQL


def kinds(text, dialect=PG):
    return [(t.kind, t.text) for t in tokenize(text, dialect) if t.kind != K.SPACE]


def test_basic_tokens():
    assert kinds("SELECT a.b, 1.5 FROM t;") == [
        (K.WORD, "SELECT"), (K.WORD, "a"), (K.DOT, "."), (K.WORD, "b"), (K.COMMA, ","),
        (K.NUMBER, "1.5"), (K.WORD, "FROM"), (K.WORD, "t"), (K.SEMI, ";"),
    ]


def test_strings_and_quoted_identifiers():
    assert kinds("'it''s; ok' \"we\"\"ird;\"") == [(K.STRING, "'it''s; ok'"), (K.QUOTED, '"we""ird;"')]
    assert kinds(r"E'a\'b;c'") == [(K.STRING, r"E'a\'b;c'")]
    assert kinds("[my;col] `x;y`", LITE) == [(K.QUOTED, "[my;col]"), (K.QUOTED, "`x;y`")]


def test_comments_nested_only_in_postgres():
    assert kinds("/* a /* b */ c */ x") == [(K.COMMENT, "/* a /* b */ c */"), (K.WORD, "x")]
    lite = kinds("/* a /* b */ c */ x", LITE)
    assert lite[0] == (K.COMMENT, "/* a /* b */")
    assert kinds("-- note; here\nx") == [(K.COMMENT, "-- note; here"), (K.WORD, "x")]


def test_line_comment_ends_at_cr_in_postgres_but_only_at_lf_in_sqlite():
    text = "SELECT 1 -- c\r; SELECT 2"
    assert kinds(text) == [
        (K.WORD, "SELECT"), (K.NUMBER, "1"), (K.COMMENT, "-- c"), (K.SEMI, ";"), (K.WORD, "SELECT"), (K.NUMBER, "2"),
    ]
    assert kinds(text, LITE)[-1] == (K.COMMENT, "-- c\r; SELECT 2")
    assert kinds("-- c\n; x", LITE)[:2] == [(K.COMMENT, "-- c"), (K.SEMI, ";")]


def test_dollar_quotes_and_params():
    body = "$fn$ BEGIN RETURN 1; END $fn$"
    assert kinds(f"AS {body};") == [(K.WORD, "AS"), (K.STRING, body), (K.SEMI, ";")]
    assert kinds("$$a;b$$") == [(K.STRING, "$$a;b$$")]
    assert kinds("WHERE id = $1") == [(K.WORD, "WHERE"), (K.WORD, "id"), (K.OTHER, "="), (K.PARAM, "$1")]
    assert kinds("foo$bar") == [(K.WORD, "foo$bar")]


def test_unterminated_constructs_do_not_raise():
    assert kinds("SELECT 'abc")[-1] == (K.STRING, "'abc")
    assert kinds("SELECT /* never closed")[-1][0] == K.COMMENT
    assert kinds("$$ open")[-1] == (K.STRING, "$$ open")


@pytest.mark.parametrize(
    "text, dialect",
    [("'abc", PG), ("E'a\\'", PG), ('"abc', PG), ('"abc""', PG), ("$$ open", PG), ("$t$ x $$", PG), ("/* open", PG),
     ("/* a /* b */", PG), ("/* open", LITE), ("[abc", LITE), ("`abc", LITE),
     ("'abc\\'", MY), ('"abc', MY), ("`abc", MY), ("/*! open", MY)],
)
def test_unterminated_tokens_are_flagged(text, dialect):
    assert [token.terminated for token in tokenize(text, dialect)] == [False]


@pytest.mark.parametrize(
    "text, dialect",
    [("'it''s'", PG), ('"we""ird"', PG), ("$$a$$", PG), ("$t$x$t$", PG), ("/* a /* b */ c */", PG), ("-- to the end", PG),
     (r"E'a\'b'", PG), ("[a]", LITE), ("`a`", LITE), ("/* a /* b */", LITE),
     (r"'a\'b'", MY), ('"a""b"', MY), ("/*!1 x */", MY), ("# to the end", MY)],
)
def test_closed_tokens_are_terminated(text, dialect):
    tokens = tokenize(text, dialect)
    assert len(tokens) == 1 and tokens[0].terminated


def test_offsets_cover_the_text():
    text = "SELECT '中文' -- 😀\nFROM t"
    tokens = tokenize(text, PG)
    assert "".join(t.text for t in tokens) == text
    assert all(text[t.start:t.end] == t.text for t in tokens)


def test_first_keyword():
    assert first_keyword("  -- c\n/* x */ with x as (select 1) select * from x", PG) == "WITH"
    assert first_keyword("", PG) == ""
    assert first_keyword("(select 1)", PG) == ""


def test_mysql_strings_escape_with_backslashes_and_double_quotes_are_strings():
    assert kinds(r"'it\'s; ok' 'a''b' '\\'", MY) == [
        (K.STRING, r"'it\'s; ok'"), (K.STRING, "'a''b'"), (K.STRING, r"'\\'"),
    ]
    assert kinds('"a;b" "x\\"y" "p""q"', MY) == [(K.STRING, '"a;b"'), (K.STRING, '"x\\"y"'), (K.STRING, '"p""q"')]
    assert kinds("`a``b;c`", MY) == [(K.QUOTED, "`a``b;c`")]
    assert kinds('"x"', PG) == [(K.QUOTED, '"x"')]  # still an identifier in PostgreSQL


def test_mysql_comments():
    assert kinds("SELECT 1 # c; x\nFROM t", MY) == [
        (K.WORD, "SELECT"), (K.NUMBER, "1"), (K.COMMENT, "# c; x"), (K.WORD, "FROM"), (K.WORD, "t"),
    ]
    # `--` is a comment only before whitespace, a control character or the end: 1--1 is 1 - (-1)
    assert kinds("SELECT 1--1", MY) == [
        (K.WORD, "SELECT"), (K.NUMBER, "1"), (K.OTHER, "-"), (K.OTHER, "-"), (K.NUMBER, "1"),
    ]
    assert kinds("x -- c\ny", MY)[1] == (K.COMMENT, "-- c")
    assert kinds("x --\ty", MY)[1] == (K.COMMENT, "--\ty")
    assert kinds("x --", MY)[-1] == (K.COMMENT, "--")
    assert kinds("x --\x7fy", MY)[1] == (K.COMMENT, "--\x7fy")  # DEL is a control character
    # only an ASCII space or control character counts: U+00A0 and U+2003 are whitespace to Python, not to the server
    for odd_space in ("\u00a0", "\u2003"):
        assert kinds(f"x --{odd_space}y", MY) == [(K.WORD, "x"), (K.OTHER, "-"), (K.OTHER, "-"), (K.WORD, "y")]
    assert kinds("-- c\r; x", MY)[0] == (K.COMMENT, "-- c\r; x")  # a line comment ends at LF only
    assert kinds("/* a /* b */ c */", MY)[0] == (K.COMMENT, "/* a /* b */")  # no nesting
    assert kinds("a # b") == [(K.WORD, "a"), (K.OTHER, "#"), (K.WORD, "b")]  # PostgreSQL: an operator


def test_mysql_executable_comments_are_code():
    assert kinds("/*!40101 SET NAMES utf8 */;", MY) == [(K.EXEC_COMMENT, "/*!40101 SET NAMES utf8 */"), (K.SEMI, ";")]
    assert kinds("SELECT /*+ BKA(t) */ 1", MY)[1] == (K.EXEC_COMMENT, "/*+ BKA(t) */")
    assert kinds("/*! x */") == [(K.COMMENT, "/*! x */")]  # PostgreSQL: an ordinary comment
    assert K.EXEC_COMMENT not in INSIGNIFICANT


def test_mariadb_executable_comments_are_code_too():  # M4: MariaDB runs `/*M! … */`
    assert kinds("/*M!DELETE FROM t WHERE*/ (SELECT 1)", MY)[0] == (K.EXEC_COMMENT, "/*M!DELETE FROM t WHERE*/")
    assert kinds("SELECT /*M!100100 1, */ 2", MY)[1] == (K.EXEC_COMMENT, "/*M!100100 1, */")
    assert kinds("/*M! open", MY) == [(K.EXEC_COMMENT, "/*M! open")]
    assert kinds("/*m! x */", MY) == [(K.COMMENT, "/*m! x */")]  # the server's prefix is case-sensitive
    assert kinds("/*M x */", MY) == [(K.COMMENT, "/*M x */")]
    assert kinds("/*M! x */") == [(K.COMMENT, "/*M! x */")]  # PostgreSQL: an ordinary comment


def test_mysql_variables_are_words():
    assert kinds("SET @x := @@session.sql_mode", MY) == [
        (K.WORD, "SET"), (K.WORD, "@x"), (K.OTHER, ":"), (K.OTHER, "="), (K.WORD, "@@session"), (K.DOT, "."),
        (K.WORD, "sql_mode"),
    ]
    assert kinds("@'v' @`w`", MY) == [(K.WORD, "@"), (K.STRING, "'v'"), (K.WORD, "@"), (K.QUOTED, "`w`")]
    assert kinds("@x") == [(K.OTHER, "@"), (K.WORD, "x")]  # PostgreSQL


def test_dollar_params_and_quotes_are_postgres_only():
    assert kinds("a$b $1 $$x$$", MY) == [
        (K.WORD, "a$b"), (K.OTHER, "$"), (K.NUMBER, "1"), (K.OTHER, "$"), (K.OTHER, "$"), (K.WORD, "x$$"),
    ]


def scanned(text, delimiter, start=0):
    return [(t.kind, t.text) for t in scan(text, MY, start, delimiter) if t.kind != K.SPACE]


def test_scan_with_a_custom_delimiter():
    assert scanned("END$$ x//", "$$") == [
        (K.WORD, "END"), (K.DELIMITER, "$$"), (K.WORD, "x"), (K.OTHER, "/"), (K.OTHER, "/"),
    ]
    assert scanned("SELECT 1// 'a//b' /* // */", "//") == [
        (K.WORD, "SELECT"), (K.NUMBER, "1"), (K.DELIMITER, "//"), (K.STRING, "'a//b'"), (K.COMMENT, "/* // */"),
    ]
    assert scanned("x;;y;z", ";;") == [(K.WORD, "x"), (K.DELIMITER, ";;"), (K.WORD, "y"), (K.SEMI, ";"), (K.WORD, "z")]
    assert scanned("abcXYdef", "XY") == [(K.WORD, "abc"), (K.DELIMITER, "XY"), (K.WORD, "def")]
    assert scanned("1253", "5") == [(K.NUMBER, "12"), (K.DELIMITER, "5"), (K.NUMBER, "3")]
    tokens = list(scan("xx SELECT", MY, 3))
    assert (tokens[0].start, tokens[0].text) == (3, "SELECT")
    assert [t.text for t in scan("abc", MY, 0, "")] == ["abc"]  # an empty delimiter is no delimiter


def test_mysql_offsets_cover_the_text():
    text = "SELECT '中文' # 😀\nFROM `t` WHERE a = \"x\\\"y\" /*! 1 */ AND @v"
    tokens = tokenize(text, MY)
    assert "".join(t.text for t in tokens) == text
    assert all(text[t.start:t.end] == t.text for t in tokens)
