"""The streaming splitter of Run SQL file (spec ⑥ D-06-3, A6): read in chunks cut anywhere, a file gives exactly the
statements, lines and byte offsets of reading it whole. Pure."""

import pytest

from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.splitter import SplitState, iter_spans, split
from dbaguy_backend.sql.statement_stream import (
    FILE_NOT_UTF8, STATEMENT_TOO_LARGE, UTF16_MESSAGE, FileStatement, StatementStream, StreamError,
    starts_with_utf16_bom,
)

PG, MY, TS, LITE, SQ = Dialect.POSTGRES, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLITE, Dialect.SQLPP

CORPUS = {
    PG: (
        "\\restrict k3y\n"
        "SET statement_timeout = 0;\nSELECT pg_catalog.set_config('search_path', '', false);\n"
        "CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $fn$\nBEGIN\n  RETURN 1; -- ;\nEND;\n$fn$;\n"
        "INSERT INTO t VALUES (1, 'a;\nb'), (2, E'c\\';d'), (3, '中文😀');\r\n"
        "/* a /* nested ; */ comment */ SELECT 2;\n"
        "CREATE FUNCTION g(x int) RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT CASE WHEN x > 0 THEN 1 END; END;\n"
        "  \\connect other\n"
        "SELECT '\\not a command';\n"
        "\\unrestrict k3y\n"
    ),
    MY: (
        "/*!40101 SET NAMES utf8mb4 */;\nLOCK TABLES `t` WRITE;\n"
        "INSERT INTO `t` VALUES (1,'it''s \\\\ back'),(2,\"x;y\"),(3,'中文😀');\r\n"
        "DELIMITER ;;\n"
        "CREATE PROCEDURE p()\nBEGIN\n  SELECT 1; SELECT 2;\nEND ;;\n"
        "DELIMITER ;\n"
        "# a hash comment ;\nSELECT 3; -- trailing\n"
        "CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW BEGIN SET NEW.a = 1; END;\n"
        "UNLOCK TABLES;\n"
    ),
    TS: (
        "SET NOCOUNT ON;\nGO\n"
        "CREATE PROCEDURE dbo.p AS\nSELECT 1;\nSELECT 'GO\nnot a line';\nGO\n"
        "INSERT INTO t VALUES (N'中文😀'), (N'x]y');\r\nGO 3\r\n"
        "/* GO\n */ PRINT 'done'\n  go\n"
        "SELECT 2"
    ),
    LITE: (
        "PRAGMA foreign_keys=OFF;\nBEGIN TRANSACTION;\n"
        "CREATE TABLE t (a, \"b;\" TEXT);\n"
        "CREATE TRIGGER tr AFTER INSERT ON t BEGIN UPDATE t SET a = 1; SELECT CASE WHEN 1 THEN 2 END; END;\n"
        "INSERT INTO t VALUES(1,'x\ny;'),(2,X'00ff'),(3,'中文😀');\nCOMMIT;\n"
    ),
    SQ: (
        "UPSERT INTO ks (KEY, VALUE) VALUES ('k1', {\"a\": \";\"});\n"
        "SELECT `a;b` FROM ks WHERE x = $v; /* ; */\nDELETE FROM ks USE KEYS 'k1';\n"
    ),
}


class Pieces:
    """A file whose read() hands out the given pieces, whatever size is asked for, then b''."""

    def __init__(self, *pieces: bytes) -> None:
        self._pieces = [p for p in pieces if p]
        self.asked: list[int] = []

    def read(self, size: int) -> bytes:
        self.asked.append(size)
        return self._pieces.pop(0) if self._pieces else b""


def whole(data: bytes, dialect: Dialect) -> list[FileStatement]:
    """The statements of the file read in one piece, from the one-shot splitter (the reference)."""
    bom = data.startswith(b"\xef\xbb\xbf")
    text = data[3:].decode() if bom else data.decode()
    found, index = [], 0
    for span in iter_spans(text, dialect, meta_commands=dialect is PG):
        index += not span.meta
        found.append(FileStatement(
            index=0 if span.meta else index, line=text.count("\n", 0, span.start) + 1, text=span.text,
            end_byte=len(text[:span.terminator_end].encode()) + 3 * bom, repeat=span.repeat, meta=span.meta,
        ))
    return found


def streamed(read, dialect: Dialect, **options) -> list[FileStatement]:
    return list(StatementStream(read, dialect, **options))


def until_the_error(read, dialect: Dialect, **options) -> tuple[list[FileStatement], StreamError]:
    """What the stream gave out before it raised, and the error."""
    given: list[FileStatement] = []
    with pytest.raises(StreamError) as raised:
        for statement in StatementStream(read, dialect, **options):
            given.append(statement)
    return given, raised.value


@pytest.mark.parametrize("dialect", list(CORPUS), ids=lambda d: d.value)
def test_every_cut_gives_what_reading_the_file_whole_gives(dialect):  # A6
    data = CORPUS[dialect].encode()
    expected = whole(data, dialect)
    assert len(expected) >= 3
    for cut in range(len(data) + 1):
        assert streamed(Pieces(data[:cut], data[cut:]).read, dialect) == expected, f"cut at byte {cut}"


@pytest.mark.parametrize("dialect", list(CORPUS), ids=lambda d: d.value)
@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 64, 1 << 20])
def test_any_chunk_size_gives_the_same_statements(dialect, chunk):
    data = CORPUS[dialect].encode()

    class File:
        position = 0

        def read(self, size: int) -> bytes:
            piece = data[self.position:self.position + size]
            self.position += len(piece)
            return piece

    assert streamed(File().read, dialect, chunk=chunk) == whole(data, dialect)


# What the splitter gave for CORPUS before the single pass (recorded from 9a8775d's split(), a test of its own
# written once, not computed here): (start, end, terminator_end, text, has_body, repeat, params). split() is binding (02a).
EXPECTED_SPANS = {
    PG: [
        (0, 39, 40, '\\restrict k3y\nSET statement_timeout = 0', False, 1, ()),
        (41, 95, 96, "SELECT pg_catalog.set_config('search_path', '', false)", False, 1, ()),
        (97, 186, 187, 'CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $fn$\nBEGIN\n  RETURN 1; -- ;\nEND;\n$fn$', False, 1, ()),
        (188, 247, 248, "INSERT INTO t VALUES (1, 'a;\nb'), (2, E'c\\';d'), (3, '中文😀')", False, 1, ()),
        (281, 289, 290, 'SELECT 2', False, 1, ()),
        (291, 392, 393, 'CREATE FUNCTION g(x int) RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT CASE WHEN x > 0 THEN 1 END; END', True, 1, ()),
        (396, 434, 435, "\\connect other\nSELECT '\\not a command'", False, 1, ()),
        (436, 451, 451, '\\unrestrict k3y', False, 1, ()),
    ],
    MY: [
        (0, 29, 30, '/*!40101 SET NAMES utf8mb4 */', False, 1, ()),
        (31, 52, 53, 'LOCK TABLES `t` WRITE', False, 1, ()),
        (54, 116, 117, 'INSERT INTO `t` VALUES (1,\'it\'\'s \\\\ back\'),(2,"x;y"),(3,\'中文😀\')', False, 1, ()),
        (132, 184, 187, 'CREATE PROCEDURE p()\nBEGIN\n  SELECT 1; SELECT 2;\nEND', True, 1, ()),
        (219, 227, 228, 'SELECT 3', False, 1, ()),
        (241, 315, 316, 'CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW BEGIN SET NEW.a = 1; END', True, 1, ()),
        (317, 330, 331, 'UNLOCK TABLES', False, 1, ()),
    ],
    TS: [
        (0, 15, 18, 'SET NOCOUNT ON;', False, 1, ()),
        (19, 78, 81, "CREATE PROCEDURE dbo.p AS\nSELECT 1;\nSELECT 'GO\nnot a line';", True, 1, ()),
        (82, 122, 129, "INSERT INTO t VALUES (N'中文😀'), (N'x]y');", False, 3, ()),
        (140, 152, 157, "PRINT 'done'", False, 1, ()),
        (158, 166, 166, 'SELECT 2', False, 1, ()),
    ],
    LITE: [
        (0, 23, 24, 'PRAGMA foreign_keys=OFF', False, 1, ()),
        (25, 42, 43, 'BEGIN TRANSACTION', False, 1, ()),
        (44, 73, 74, 'CREATE TABLE t (a, "b;" TEXT)', False, 1, ()),
        (75, 171, 172, 'CREATE TRIGGER tr AFTER INSERT ON t BEGIN UPDATE t SET a = 1; SELECT CASE WHEN 1 THEN 2 END; END', True, 1, ()),
        (173, 225, 226, "INSERT INTO t VALUES(1,'x\ny;'),(2,X'00ff'),(3,'中文😀')", False, 1, ()),
        (227, 233, 234, 'COMMIT', False, 1, ()),
    ],
    SQ: [
        (0, 53, 54, 'UPSERT INTO ks (KEY, VALUE) VALUES (\'k1\', {"a": ";"})', False, 1, ()),
        (55, 88, 89, 'SELECT `a;b` FROM ks WHERE x = $v', False, 1, ('$v',)),
        (98, 126, 127, "DELETE FROM ks USE KEYS 'k1'", False, 1, ()),
    ],
}


@pytest.mark.parametrize("dialect", list(CORPUS), ids=lambda d: d.value)
def test_split_gives_what_it_gave_before_the_single_pass(dialect):
    # (The splitter's own suites, test_splitter.py and test_tsql.py, are unchanged and pass: that is A6's other half.)
    text = CORPUS[dialect]
    expected = EXPECTED_SPANS[dialect]
    as_tuples = lambda spans: [(s.start, s.end, s.terminator_end, s.text, s.has_body, s.repeat, s.params) for s in spans]
    assert as_tuples(split(text, dialect)) == expected
    assert as_tuples(iter_spans(text, dialect)) == expected  # without meta_commands, a psql line is part of a statement
    assert not any(s.meta for s in split(text, dialect))


def test_psql_meta_lines_are_spans_of_their_own_in_a_postgresql_file():
    statements = streamed(Pieces(CORPUS[PG].encode()).read, PG)
    meta = [(s.line, s.text) for s in statements if s.meta]
    assert meta == [(1, "\\restrict k3y"), (13, "\\connect other"), (15, "\\unrestrict k3y")]
    assert all(s.index == 0 for s in statements if s.meta)
    assert [s.index for s in statements if not s.meta] == list(range(1, 8))
    assert "SELECT '\\not a command'" in [s.text for s in statements]  # a backslash inside a statement is not one


def test_line_numbers_and_byte_offsets():
    data = "SELECT 1;\n\n-- note\nSELECT 'a\nb';\r\nSELECT '中';".encode()
    statements = streamed(Pieces(data).read, PG)
    assert [(s.index, s.line, s.text) for s in statements] == [
        (1, 1, "SELECT 1"), (2, 4, "SELECT 'a\nb'"), (3, 6, "SELECT '中'"),
    ]
    assert [s.end_byte for s in statements] == [9, 9 + len("\n\n-- note\nSELECT 'a\nb';"), len(data)]


def test_a_bom_is_dropped_and_counted_in_the_bytes():
    data = b"\xef\xbb\xbfSELECT 1;\nSELECT 2;\n"
    for cut in range(len(data) + 1):
        statements = streamed(Pieces(data[:cut], data[cut:]).read, MY)
        assert [(s.text, s.end_byte) for s in statements] == [("SELECT 1", 12), ("SELECT 2", 22)], cut


def test_go_counts_and_crlf_line_ends():
    data = b"SELECT 1\r\nGO 3\r\nSELECT 2\r\ngo\r\n"
    assert [(s.text, s.repeat, s.line) for s in streamed(Pieces(data).read, TS)] == [
        ("SELECT 1", 3, 1), ("SELECT 2", 1, 3),
    ]


def test_text_that_is_not_utf8_stops_at_its_line():
    data = b"SELECT 1;\nSELECT 2;\nSELECT '\xff';\n"
    for cut in range(len(data) + 1):
        with pytest.raises(StreamError) as raised:
            streamed(Pieces(data[:cut], data[cut:]).read, PG)
        assert (raised.value.code, raised.value.line, raised.value.message) == (
            FILE_NOT_UTF8, 3, "Line 3 is not UTF-8 text"), cut


def test_the_statements_before_a_line_that_is_not_utf8_are_still_given():  # pre-flight M3
    data = b"SELECT 1;\nSELECT 2;\nSELECT 'x\xff';\nSELECT 4;\n"
    for dialect in (PG, MY, LITE):
        for cut in range(len(data) + 1):
            given, error = until_the_error(Pieces(data[:cut], data[cut:]).read, dialect)
            assert [(s.index, s.line, s.text, s.end_byte) for s in given] == [
                (1, 1, "SELECT 1", 9), (2, 2, "SELECT 2", 19),
            ], (dialect, cut)
            assert (error.code, error.line) == (FILE_NOT_UTF8, 3), (dialect, cut)
    for chunk in (1, 5, 1 << 20):  # whatever the chunk size, the bytes after the line are never decoded
        given, error = until_the_error(Pieces(data).read, PG, chunk=chunk)
        assert [s.text for s in given] == ["SELECT 1", "SELECT 2"] and error.line == 3


def test_a_line_that_is_not_utf8_inside_a_statement_holds_that_statement_back():  # pre-flight M3
    data = b"SELECT 1;\nINSERT INTO t VALUES\n(1),\n('\xff'),\n(2);\nSELECT 3;\n"
    for cut in range(len(data) + 1):
        given, error = until_the_error(Pieces(data[:cut], data[cut:]).read, PG)
        assert [s.text for s in given] == ["SELECT 1"], cut  # the INSERT never ended
        assert (error.code, error.line) == (FILE_NOT_UTF8, 4), cut


def test_a_line_that_is_not_utf8_after_a_t_sql_batch_and_at_the_end_of_the_file():  # pre-flight M3
    data = b"SELECT 1\nGO\nSELECT 2\nGO 2\nSELECT '\xff'"
    for cut in range(len(data) + 1):
        given, error = until_the_error(Pieces(data[:cut], data[cut:]).read, TS)
        assert [(s.text, s.repeat) for s in given] == [("SELECT 1", 1), ("SELECT 2", 2)], cut
        assert (error.code, error.line) == (FILE_NOT_UTF8, 5), cut


def test_a_line_that_is_not_utf8_ends_a_file_whose_last_statement_has_not_ended():  # pre-flight M3
    data = b"SELECT 1;\nINSERT INTO t VALUES\n(1),\n('\xff')"  # no line break after the bad line
    for cut in range(len(data) + 1):
        given, error = until_the_error(Pieces(data[:cut], data[cut:]).read, PG)
        assert [s.text for s in given] == ["SELECT 1"], cut  # the INSERT, cut short by the bad line, is not given
        assert (error.code, error.line) == (FILE_NOT_UTF8, 4), cut


def test_a_utf16_file_is_named_as_such():  # pre-flight L5: SSMS's "Unicode text"
    for bom, encoding in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        data = bom + "SELECT 1;\r\nGO\r\n".encode(encoding)
        for cut in range(len(data) + 1):
            with pytest.raises(StreamError) as raised:
                streamed(Pieces(data[:cut], data[cut:]).read, TS)
            assert (raised.value.code, raised.value.line, raised.value.message) == (
                FILE_NOT_UTF8, 1, UTF16_MESSAGE), cut
        assert starts_with_utf16_bom(data) and starts_with_utf16_bom(data[:2])
    assert not starts_with_utf16_bom(b"\xef\xbb\xbfSELECT 1") and not starts_with_utf16_bom(b"")
    assert not starts_with_utf16_bom(b"\xff")


def test_a_statement_past_the_limit_stops_at_its_first_line():
    data = b"SELECT 1;\n\n  INSERT INTO t VALUES\n" + b"(1),\n" * 50 + b"(2);\n"
    with pytest.raises(StreamError) as raised:
        streamed(Pieces(data).read, PG, max_statement_chars=100)
    assert (raised.value.code, raised.value.line) == (STATEMENT_TOO_LARGE, 3)
    one_line = b"SELECT '" + b"x" * 500 + b"'"  # no line break at all: still stopped, never held whole
    with pytest.raises(StreamError) as raised:
        streamed(Pieces(*[one_line[i:i + 10] for i in range(0, len(one_line), 10)]).read, PG,
                 max_statement_chars=100)
    assert (raised.value.code, raised.value.line) == (STATEMENT_TOO_LARGE, 1)
    assert len(streamed(Pieces(data).read, PG, max_statement_chars=1000)) == 2  # under the limit: fine


def test_a_statement_that_goes_on_doubles_the_next_read():
    data = b"INSERT INTO t VALUES\n" + b"(1),\n" * 1000 + b"(2);\nSELECT 1;\n"

    class File:
        position = 0
        asked: list[int] = []

        def read(self, size: int) -> bytes:
            self.asked.append(size)
            piece = data[self.position:self.position + size]
            self.position += len(piece)
            return piece

    file = File()
    assert [s.text[:6] for s in streamed(file.read, PG, chunk=16)] == ["INSERT", "SELECT"]
    assert file.asked[:10] == [16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 16]  # log2(size / chunk) rescans


def test_bytes_read_counts_the_whole_file():
    data = CORPUS[MY].encode()
    stream = StatementStream(Pieces(data[:100], data[100:]).read, MY)
    list(stream)
    assert stream.bytes_read == len(data)


def test_a_file_with_carriage_returns_only_is_one_line_read_whole():
    # An old Mac file has no "\n": there is no line to cut at, so it is held whole (and so limited like one statement)
    data = b"SELECT 1;\rSELECT 2;\rSELECT 3;"
    for cut in range(len(data) + 1):
        assert streamed(Pieces(data[:cut], data[cut:]).read, PG) == whole(data, PG), cut
    assert [(s.text, s.line) for s in streamed(Pieces(data).read, PG)] == [
        ("SELECT 1", 1), ("SELECT 2", 1), ("SELECT 3", 1),
    ]
    with pytest.raises(StreamError) as raised:
        streamed(Pieces(data).read, PG, max_statement_chars=10)
    assert (raised.value.code, raised.value.line) == (STATEMENT_TOO_LARGE, 1)


def test_a_psql_line_ends_before_its_carriage_return():
    data = b"\\connect db\r\nSELECT 1;\r\n  \\x\r\n"
    for cut in range(len(data) + 1):
        statements = streamed(Pieces(data[:cut], data[cut:]).read, PG)
        assert [(s.meta, s.text, s.line, s.end_byte) for s in statements] == [
            (True, "\\connect db", 1, 12), (False, "SELECT 1", 2, 22), (True, "\\x", 3, 29),
        ], cut


def test_an_empty_file_and_one_without_statements_give_nothing():
    for data in (b"", b"\xef\xbb\xbf", b"-- only a comment\n\n", b"/* c */\n"):
        for dialect in CORPUS:
            assert streamed(Pieces(data).read, dialect) == []


@pytest.mark.parametrize(("dialect", "data", "texts"), [
    (MY, b"SELECT 1; DELIMITER $$\nSELECT 2$$\nSELECT 3;\n", ["SELECT 1", "DELIMITER $$\nSELECT 2$$\nSELECT 3"]),
    (TS, b"SELECT 1; GO\nSELECT 2\nGO\n", ["SELECT 1; GO\nSELECT 2"]),
    (PG, b"SELECT 1; \\connect x\nSELECT 2;\n", ["SELECT 1", "\\connect x\nSELECT 2"]),
    (PG, b"SELECT 1\n\\x\n;\n", ["SELECT 1\n\\x"]),
], ids=["mysql-delimiter", "tsql-go", "psql-after-a-statement", "psql-inside-a-statement"])
def test_a_command_that_is_not_first_on_its_line_is_no_command_at_any_cut(dialect, data, texts):
    # The text carried to the next chunk starts at its line, so "first on its line" is judged as in the whole file
    for cut in range(len(data) + 1):
        statements = streamed(Pieces(data[:cut], data[cut:]).read, dialect)
        assert [s.text for s in statements] == texts, cut
        assert not any(s.meta for s in statements) and statements == whole(data, dialect), cut


def test_a_psql_line_without_its_line_break_is_not_given_until_the_text_is_final():
    # What iter_spans does when its caller's text ends inside the line: the line may go on in the next chunk
    assert list(iter_spans("\\connect x", PG, final=False, meta_commands=True)) == []
    assert [(s.meta, s.text) for s in iter_spans("\\connect x", PG, meta_commands=True)] == [(True, "\\connect x")]
    state = SplitState()
    assert list(iter_spans("SELECT 1;\n\\conn", PG, state, final=False, meta_commands=True))[0].text == "SELECT 1"
    assert state.resume == len("SELECT 1;")  # the unfinished line is the statement that has not ended: kept


# --- the limit on one statement: what is held, where it is measured from, which line it names -------------------------

class File:
    """A file read in the sizes it is asked for, which it keeps."""

    def __init__(self, data: bytes) -> None:
        self.data, self.position, self.asked = data, 0, []

    def read(self, size: int) -> bytes:
        self.asked.append(size)
        piece = self.data[self.position:self.position + size]
        self.position += len(piece)
        return piece


@pytest.mark.parametrize("chunk", [1, 16, 4096, 1 << 20])
def test_a_read_never_goes_past_the_limit_by_more_than_a_character(chunk):
    cap = 1000
    for data in (b"SELECT '" + b"x" * 50_000 + b"'",  # one line
                 b"INSERT INTO t VALUES\n" + b"(1),\n" * 20_000 + b"(2);\n"):  # many
        file = File(data)
        stream = StatementStream(file.read, PG, chunk=chunk, max_statement_chars=cap)
        with pytest.raises(StreamError) as raised:
            list(stream)
        assert raised.value.code == STATEMENT_TOO_LARGE
        assert max(file.asked) <= cap + 1, file.asked  # no read is larger than the room left
        assert stream.bytes_read <= cap + chunk and stream.bytes_read == file.position  # held: cap plus one chunk at most
        assert sum(file.asked) <= cap * 3 + chunk  # and the doubling reads that got there are few


def test_the_limit_is_measured_from_the_first_token_at_every_chunk_size_and_cut():
    # 60 characters of statement under a limit of 70, behind 50 of comment: fine, whichever way the file is read
    statement = "SELECT '" + "x" * 50 + "';"
    data = ("SELECT 1;\n-- " + "c" * 47 + "\n" + statement + "\n").encode()
    assert len(statement) == 60
    expected = [("SELECT 1", 1), ("SELECT '" + "x" * 50 + "'", 3)]
    for chunk in (1, 2, 3, 7, 16, 64, 100, 1 << 20):
        assert [(s.text, s.line) for s in streamed(File(data).read, PG, chunk=chunk, max_statement_chars=70)] == expected
    for cut in range(len(data) + 1):
        statements = streamed(Pieces(data[:cut], data[cut:]).read, PG, max_statement_chars=70)
        assert [(s.text, s.line) for s in statements] == expected, cut
    for cap in (59, 58):  # the statement and its terminator are 60 characters: one more than the limit stops it
        with pytest.raises(StreamError) as raised:
            streamed(File(data).read, PG, chunk=7, max_statement_chars=cap)
        assert (raised.value.code, raised.value.line) == (STATEMENT_TOO_LARGE, 3)


def test_the_line_of_a_statement_past_the_limit_is_its_own_not_a_comment_s():
    data = (b"SELECT 1;\n\n-- one comment line\n/* and another */\n  INSERT INTO t VALUES\n" + b"(1),\n" * 60 + b"(2);\n")
    seen = set()
    for chunk in (1, 3, 16, 100, 1 << 20):  # held back across reads, or finished inside one read (the large chunk)
        for cap in (100, 150):
            with pytest.raises(StreamError) as raised:
                streamed(File(data).read, PG, chunk=chunk, max_statement_chars=cap)
            seen.add((raised.value.code, raised.value.line))
    assert seen == {(STATEMENT_TOO_LARGE, 5)}  # the INSERT, not line 3 where the comments begin
    for cut in range(len(data) + 1):
        with pytest.raises(StreamError) as raised:
            streamed(Pieces(data[:cut], data[cut:]).read, PG, max_statement_chars=120)
        assert (raised.value.code, raised.value.line) == (STATEMENT_TOO_LARGE, 5), cut


def test_comments_and_blank_lines_between_statements_are_not_held_against_the_limit():
    # 130 KB of comments and blank lines under a limit of 100 characters: dropped as they are read, never counted
    data = b"SELECT 1;\n" + (b"-- a comment that goes on\n\n/* and a block\n comment */\n" * 2000) + b"SELECT 2;\n"
    for chunk in (1, 16, 4096, 1 << 20):
        statements = streamed(File(data).read, PG, chunk=chunk, max_statement_chars=100)
        assert [(s.text, s.line) for s in statements] == [("SELECT 1", 1), ("SELECT 2", 8002)], chunk
    for cut in range(0, len(data) + 1, 997):
        assert len(streamed(Pieces(data[:cut], data[cut:]).read, PG, max_statement_chars=100)) == 2


def test_a_block_comment_that_stays_open_past_the_limit_names_the_line_it_opened_on():
    data = b"SELECT 1;\n\n  /* an open comment\n" + b"and more of it\n" * 40
    for chunk in (1, 16, 1 << 20):
        with pytest.raises(StreamError) as raised:
            streamed(File(data).read, PG, chunk=chunk, max_statement_chars=100)
        assert (raised.value.code, raised.value.line) == (STATEMENT_TOO_LARGE, 3), chunk


def test_a_statement_at_the_limit_gets_through_and_one_character_more_does_not():
    statement = "INSERT INTO t VALUES\n(1),\n(2);"  # 30 characters with its terminator
    data = ("SELECT 1;\n-- c\n" + statement + "\n").encode()
    assert len(statement) == 30
    for chunk in (1, 2, 5, 16, 1 << 20):
        assert len(streamed(File(data).read, PG, chunk=chunk, max_statement_chars=30)) == 2, chunk
        with pytest.raises(StreamError) as raised:
            streamed(File(data).read, PG, chunk=chunk, max_statement_chars=29)
        assert (raised.value.code, raised.value.line) == (STATEMENT_TOO_LARGE, 3), chunk
    for cut in range(len(data) + 1):  # a read that hands out more than it was asked for: the finished statement is judged
        assert len(streamed(Pieces(data[:cut], data[cut:]).read, PG, max_statement_chars=30)) == 2, cut
        with pytest.raises(StreamError) as raised:
            streamed(Pieces(data[:cut], data[cut:]).read, PG, max_statement_chars=29)
        assert (raised.value.code, raised.value.line) == (STATEMENT_TOO_LARGE, 3), cut


def test_the_last_statement_of_a_file_without_a_terminator_is_measured_to_the_end_of_the_text():
    data = b"SELECT 1;\nSELECT '" + b"x" * 20 + b"'\n"  # 29 characters, then a line break and the end of the file
    for chunk in (1, 4, 1 << 20):
        assert len(streamed(File(data).read, PG, chunk=chunk, max_statement_chars=30)) == 2, chunk  # with the "\n"
        with pytest.raises(StreamError):
            streamed(File(data).read, PG, chunk=chunk, max_statement_chars=29)
    for cut in range(len(data) + 1):
        assert len(streamed(Pieces(data[:cut], data[cut:]).read, PG, max_statement_chars=30)) == 2, cut
        with pytest.raises(StreamError):
            streamed(Pieces(data[:cut], data[cut:]).read, PG, max_statement_chars=29)


def test_a_file_of_small_statements_on_one_line_is_limited_like_one_statement():
    # The text is cut at a line break, and the line it is cut in is read whole (the module's docstring says so)
    data = b"SELECT 1;" * 100  # 900 characters, 100 statements, no line break
    assert len(streamed(File(data).read, PG, chunk=16, max_statement_chars=2000)) == 100
    for chunk in (16, 1 << 20):
        with pytest.raises(StreamError) as raised:
            streamed(File(data).read, PG, chunk=chunk, max_statement_chars=500)
        assert (raised.value.code, raised.value.line) == (STATEMENT_TOO_LARGE, 1)
    with_breaks = data.replace(b";", b";\n")  # the same statements on lines: no limit in the way
    assert len(streamed(File(with_breaks).read, PG, chunk=16, max_statement_chars=500)) == 100


def test_characters_not_bytes_are_what_the_limit_counts():
    statement = "SELECT '" + "中文😀" * 10 + "';"  # 8 + 30 + 2 = 40 characters, 8 + 100 + 2 = 110 bytes
    data = (statement + "\n").encode()
    assert (len(statement), len(statement.encode())) == (40, 110)
    for chunk in (1, 3, 1 << 20):
        assert len(streamed(File(data).read, PG, chunk=chunk, max_statement_chars=40)) == 1, chunk
        with pytest.raises(StreamError):
            streamed(File(data).read, PG, chunk=chunk, max_statement_chars=39)
    for cut in range(len(data) + 1):
        assert len(streamed(Pieces(data[:cut], data[cut:]).read, PG, max_statement_chars=40)) == 1, cut
        with pytest.raises(StreamError):
            streamed(Pieces(data[:cut], data[cut:]).read, PG, max_statement_chars=39)


def test_byte_counts_cross_the_slices_a_stretch_is_encoded_in(monkeypatch):
    from dbaguy_backend.sql import statement_stream

    monkeypatch.setattr(statement_stream, "_SLICE", 3)
    text = "中a😀b\n中文é\n" * 3 + "tail"
    for start in range(len(text) + 1):
        for end in range(start, len(text) + 1):
            cursor = statement_stream._Cursor(byte=5)
            cursor.advance(text, start)
            before = (cursor.line, cursor.byte)
            cursor.advance(text, end)
            assert (cursor.line, cursor.byte) == (
                1 + text.count("\n", 0, end), 5 + len(text[:end].encode())), (start, end)
            assert before == (1 + text.count("\n", 0, start), 5 + len(text[:start].encode()))
    ascii_cursor = statement_stream._Cursor(byte=0)
    ascii_cursor.advance("ab\ncd\n", 4)
    assert (ascii_cursor.line, ascii_cursor.byte) == (2, 4)


def test_a_large_file_of_one_row_inserts_is_streamed_in_bounded_memory():
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    # A 20 MB file generated as it is read (the test never holds it), in a process of its own so that what the
    # interpreter held before does not hide the growth. Reading it whole would grow the process by several times this.
    code = textwrap.dedent("""
        import resource, sys
        from dbaguy_backend.sql.dialects import Dialect
        from dbaguy_backend.sql.statement_stream import StatementStream

        total = 20 * 1_048_576
        line = ("INSERT INTO t VALUES (7, '" + "中文😀 name " * 8 + "it''s');\\n").encode()
        pattern = line * 4096
        sent = 0

        def read(size):
            global sent
            n = min(size, total - sent)
            start = sent % len(pattern)
            piece = (pattern + pattern)[start:start + n]
            sent += n
            return piece

        scale = 1 if sys.platform == "darwin" else 1024  # ru_maxrss: bytes on macOS, kilobytes elsewhere
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
        stream = StatementStream(read, Dialect.POSTGRES)
        count = sum(1 for _ in stream)
        grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale - before
        print(count, stream.bytes_read, grown)
    """)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                            cwd=Path(__file__).parents[2])
    assert result.returncode == 0, result.stderr
    count, bytes_read, grown = (int(n) for n in result.stdout.split())
    assert count > 100_000 and bytes_read == 20 * 1_048_576
    assert grown < 48 * 1_048_576, f"the process grew by {grown:,} bytes streaming a 20 MiB file"  # measured 13 MB
