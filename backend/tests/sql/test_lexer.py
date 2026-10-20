import pytest

from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.lexer import TokenKind as K
from dbaguy_backend.sql.lexer import INSIGNIFICANT, first_keyword, scan, tokenize

PG, LITE, MY, TS, SQ = Dialect.POSTGRES, Dialect.SQLITE, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLPP


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
     ("'abc\\'", MY), ('"abc', MY), ("`abc", MY), ("/*! open", MY),
     ("[abc", TS), ("[a]]", TS), ("N'abc", TS), ('"abc', TS), ("/* a /* b */", TS),
     ("`abc", SQ), (r"`a\`", SQ), (r"'a\'", SQ), ('"abc', SQ)],
)
def test_unterminated_tokens_are_flagged(text, dialect):
    assert [token.terminated for token in tokenize(text, dialect)] == [False]


@pytest.mark.parametrize(
    "text, dialect",
    [("'it''s'", PG), ('"we""ird"', PG), ("$$a$$", PG), ("$t$x$t$", PG), ("/* a /* b */ c */", PG), ("-- to the end", PG),
     (r"E'a\'b'", PG), ("[a]", LITE), ("`a`", LITE), ("/* a /* b */", LITE),
     (r"'a\'b'", MY), ('"a""b"', MY), ("/*!1 x */", MY), ("# to the end", MY),
     ("[a]]b]", TS), ("N'a''b'", TS), ("/* a /* b */ */", TS), ("-- to the end", TS),
     (r"`a\`b`", SQ), ("`a``b`", SQ), (r'"a\"b"', SQ), ("-- to the end", SQ)],
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


def test_a_strings_end_of_comment_inside_an_executable_comment_ends_nothing():  # 06a Task 6 review T8
    # SHOW CREATE TABLE writes a partition clause as /*!50500 … */: its values and comments are strings
    text = "/*!50500 PARTITION p1 VALUES IN ('a*/b') COMMENT 'x */ ; y' */;"
    assert kinds(text, MY) == [(K.EXEC_COMMENT, text[:-1]), (K.SEMI, ";")]
    assert kinds('/*!40000 "q*/q" `n*/n` */ x', MY)[0] == (K.EXEC_COMMENT, '/*!40000 "q*/q" `n*/n` */')
    assert kinds("/*+ SET_VAR(sql_mode = 'a*/') */ 1", MY)[0] == (K.EXEC_COMMENT, "/*+ SET_VAR(sql_mode = 'a*/') */")
    assert kinds("/*!50500 'open */", MY) == [(K.EXEC_COMMENT, "/*!50500 'open */")]  # unterminated: to the end
    assert not tokenize("/*!50500 'open */", Dialect.MYSQL)[0].terminated  # a statement stream holds it back
    assert kinds("/* plain 'a*/ x', b */", MY)[0] == (K.COMMENT, "/* plain 'a*/")  # an ordinary comment is text


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


def test_tsql_names_and_strings():
    assert kinds("[a]]b;c] \"x;y\" N'it''s;中文' n'z'", TS) == [
        (K.QUOTED, "[a]]b;c]"), (K.QUOTED, '"x;y"'), (K.STRING, "N'it''s;中文'"), (K.STRING, "n'z'"),
    ]
    assert kinds("N 'x'", TS) == [(K.WORD, "N"), (K.STRING, "'x'")]  # with a space: the word N, then a string
    assert kinds("'a\\' + 'b'", TS) == [(K.STRING, "'a\\'"), (K.OTHER, "+"), (K.STRING, "'b'")]  # no backslash escapes
    assert kinds("N'x'", PG) == [(K.WORD, "N"), (K.STRING, "'x'")]  # other dialects unchanged


def test_tsql_comments_and_words():
    assert kinds("/* a /* b */ c */ x", TS)[0] == (K.COMMENT, "/* a /* b */ c */")
    assert kinds("SELECT 1--1", TS)[-1] == (K.COMMENT, "--1")  # T-SQL needs no space after --
    assert kinds("-- c\r; x", TS) == [(K.COMMENT, "-- c"), (K.SEMI, ";"), (K.WORD, "x")]  # ends at CR too
    assert kinds("FROM #t JOIN ##g ON @x = @@ROWCOUNT", TS) == [
        (K.WORD, "FROM"), (K.WORD, "#t"), (K.WORD, "JOIN"), (K.WORD, "##g"), (K.WORD, "ON"), (K.WORD, "@x"),
        (K.OTHER, "="), (K.WORD, "@@ROWCOUNT"),
    ]


# SQL Server separates tokens at every Unicode space (probed on 2022: exactly str.isspace() and U+200B), so
# `DELETE\u00a0FROM t` is a DELETE there; elsewhere a non-ASCII character continues a name, as in the servers.
TSQL_SPACES = ["\x85", "\xa0", "\u1680", *map(chr, range(0x2000, 0x200C)), "\u2028", "\u2029", "\u202f", "\u205f",
               "\u3000"]


@pytest.mark.parametrize("space", TSQL_SPACES)
def test_tsql_unicode_spaces_end_words(space):
    text = f"SELECT{space}a{space}FROM @t{space}#x{space}DELETE{space}FROM t"
    assert kinds(text, TS) == [
        (K.WORD, "SELECT"), (K.WORD, "a"), (K.WORD, "FROM"), (K.WORD, "@t"), (K.WORD, "#x"), (K.WORD, "DELETE"),
        (K.WORD, "FROM"), (K.WORD, "t"),
    ]
    assert "".join(t.text for t in tokenize(text, TS)) == text


def test_other_unicode_stays_in_tsql_names_and_spaces_stay_in_other_dialects_names():
    assert kinds("SELECT a\u200cb, 名前 FROM t", TS)[1:4] == [(K.WORD, "a\u200cb"), (K.COMMA, ","), (K.WORD, "名前")]
    for dialect in (PG, LITE, MY):
        assert kinds("SELECT a\u00a0b\u200bc", dialect) == [(K.WORD, "SELECT"), (K.WORD, "a\u00a0b\u200bc")]


def test_closing_brackets_double_in_every_dialect():  # Ruling 4
    assert kinds("[a]]b] x", LITE) == [(K.QUOTED, "[a]]b]"), (K.WORD, "x")]


def test_tsql_offsets_cover_the_text():
    text = "SELECT N'中文' -- 😀\nFROM [a]]b] /* x /* y */ */ WHERE @v = #t"
    tokens = tokenize(text, TS)
    assert "".join(t.text for t in tokens) == text
    assert all(text[t.start:t.end] == t.text for t in tokens)


def test_sqlpp_strings_names_and_parameters():
    text = r"""'it''s;' "a\"b;" 'c\'d' `a``b;` `c\`d;` $x @y $1 ? $ x$y"""
    assert kinds(text, SQ) == [
        (K.STRING, "'it''s;'"), (K.STRING, r'"a\"b;"'), (K.STRING, r"'c\'d'"), (K.QUOTED, "`a``b;`"),
        (K.QUOTED, r"`c\`d;`"), (K.PARAM, "$x"), (K.PARAM, "@y"), (K.PARAM, "$1"), (K.PARAM, "?"),
        (K.OTHER, "$"), (K.WORD, "x$y"),
    ]
    assert kinds("? $x", PG) == [(K.OTHER, "?"), (K.OTHER, "$"), (K.WORD, "x")]  # other dialects unchanged


def test_sqlpp_doubled_double_quote_covers_the_same_text_as_the_server_reads():
    # The server reads "a""b" as two strings (a syntax error, probed in Task 2): one token here, the same extent.
    assert kinds('"a""b;" ;', SQ) == [(K.STRING, '"a""b;"'), (K.SEMI, ";")]


def test_sqlpp_comments():
    assert kinds("SELECT 1--x", SQ)[-1] == (K.COMMENT, "--x")  # no space needed after --
    assert kinds("-- c\r; x", SQ) == [(K.COMMENT, "-- c"), (K.SEMI, ";"), (K.WORD, "x")]  # CR ends it (A6: safe side)
    assert kinds("/* a /* b */ c */", SQ)[0] == (K.COMMENT, "/* a /* b */")  # not nested
    assert kinds("# not a comment", SQ)[0] == (K.OTHER, "#")
    assert kinds("/*+ ordered */ SELECT", SQ)[0] == (K.COMMENT, "/*+ ordered */")  # a hint is a plain comment


def test_sqlpp_unicode_spaces_follow_the_server():  # A7: the server rejects U+00A0 (syntax error, nothing runs)
    assert kinds("DELETE\u00a0FROM t", SQ) == [(K.WORD, "DELETE\u00a0FROM"), (K.WORD, "t")]


@pytest.mark.parametrize("text, tokens", [  # Task 9 fix round 3: T-SQL numbers as SQL Server 2022 reads them
    ("0x1TRUNCATE", [(K.NUMBER, "0x1"), (K.WORD, "TRUNCATE")]),
    ("0X1F TABLE", [(K.NUMBER, "0X1F"), (K.WORD, "TABLE")]),
    ("0xTRUNCATE", [(K.NUMBER, "0x"), (K.WORD, "TRUNCATE")]),  # 0x then a name
    ("0x1e5z", [(K.NUMBER, "0x1e5"), (K.WORD, "z")]),  # all hex digits belong to 0x
    ("1eTRUNCATE", [(K.NUMBER, "1e"), (K.WORD, "TRUNCATE")]),
    ("1Else", [(K.NUMBER, "1E"), (K.WORD, "lse")]),
    ("1.eUNION", [(K.NUMBER, "1.e"), (K.WORD, "UNION")]),
    (".5eUNION", [(K.NUMBER, ".5e"), (K.WORD, "UNION")]),
    ("1e+UNION", [(K.NUMBER, "1e+"), (K.WORD, "UNION")]),
    ("1E-SELECT", [(K.NUMBER, "1E-"), (K.WORD, "SELECT")]),
    ("1.5e3x", [(K.NUMBER, "1.5e3"), (K.WORD, "x")]),
    ("$1UNION", [(K.NUMBER, "$1"), (K.WORD, "UNION")]),  # money literal
    ("$1.5z", [(K.NUMBER, "$1.5"), (K.WORD, "z")]),
    ("$1eUNION", [(K.NUMBER, "$1"), (K.WORD, "eUNION")]),  # $ money takes no exponent
    ("$action", [(K.WORD, "$action")]),  # a pseudocolumn
    ("1dUNION", [(K.NUMBER, "1"), (K.WORD, "dUNION")]),  # no d/f/m suffix in T-SQL
    ("0b1UNION", [(K.NUMBER, "0"), (K.WORD, "b1UNION")]),  # no binary 0b literal
    ("N'0x1'", [(K.STRING, "N'0x1'")]),  # inside a string: untouched
    ("[0x1z]", [(K.QUOTED, "[0x1z]")]),  # inside brackets: untouched
])
def test_tsql_numbers_glued_to_words_match_the_server(text, tokens):
    assert kinds(text, TS) == tokens


def test_other_dialects_keep_their_number_lexing():
    # PG/MySQL/SQLite read 0x1UNION as 0 then x1UNION (they have no 0x literal); the server rejects it as a syntax
    # error, so DbaGuy never sends it. Their lexing is unchanged by the T-SQL rule.
    for dialect in (PG, MY, LITE):
        assert kinds("0x1UNION", dialect) == [(K.NUMBER, "0"), (K.WORD, "x1UNION")]
        assert kinds("1e5 x", dialect)[0] == (K.NUMBER, "1e5")
