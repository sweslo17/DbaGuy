from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.lexer import TokenKind as K
from dbaguy_backend.sql.lexer import first_keyword, tokenize

PG, LITE = Dialect.POSTGRES, Dialect.SQLITE


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


def test_offsets_cover_the_text():
    text = "SELECT '中文' -- 😀\nFROM t"
    tokens = tokenize(text, PG)
    assert "".join(t.text for t in tokens) == text
    assert all(text[t.start:t.end] == t.text for t in tokens)


def test_first_keyword():
    assert first_keyword("  -- c\n/* x */ with x as (select 1) select * from x", PG) == "WITH"
    assert first_keyword("", PG) == ""
    assert first_keyword("(select 1)", PG) == ""
