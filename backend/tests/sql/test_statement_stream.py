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


def test_split_is_unchanged_by_the_single_pass():
    for dialect, text in CORPUS.items():  # meta lines are only spans of their own in a file
        assert [s.text for s in split(text, dialect)] == [
            s.text for s in iter_spans(text, dialect) if not s.meta
        ]
    assert not any(s.meta for s in split(CORPUS[PG], PG))


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
