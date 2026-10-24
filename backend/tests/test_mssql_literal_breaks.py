"""A backslash right before a line break in a T-SQL string literal (06c Task 5b), against the dev SQL Server.

SQL Server reads such a backslash as a line continuation and drops it: with the break when it is LF or CRLF, alone when
the break is a lone CR (`N'a\\<LF>b'` is `ab`, `N'a\\<CR>b'` is `a<CR>b`). Every T-SQL literal DbaGuy writes comes from
`quote_literal`, which ends the literal after that backslash, so a value with the pair is the value the user typed (⑤'s
edits), dumped (06a) and copied (06c). The pure text of it is in tests/sql/test_dialects_offsets.py; here the server
reads the literals back. Set-up and checks go through the driver with expressions (`NCHAR(92)`), never through the code
under test. Scratch objects: `zz_06c_*`, dropped by exact name."""

import re

import pytest
from pymssql import _mssql

from dbaguy_backend.domain.data_jobs import DumpScope
from dbaguy_backend.domain.execution import DumpCommand, RunFileCommand
from dbaguy_backend.sql.dialects import Dialect, quote_literal
from dbaguy_backend.sql.formatting import value_condition
from tests.data_support import SCRATCH_06C, DataApp, Ran, dropping, scratch_database, scratch_name
from tests.edit_support import delete, edit_env, insert, scratch_db, update, val
from tests.support import mssql_connect, requires_mssql

pytestmark = requires_mssql
TS = Dialect.TSQL
BS, LF, CR = "\\", "\n", "\r"

# Every text is written by `quote_literal`; `national` False (a non-Unicode column's ASCII text) only for the ASCII
# ones. The last is more breaks than a chain of `+` could carry (the server stops one at 1,000-2,000, error 8631).
SHAPES: dict[str, str] = {
    "lf": f"a{BS}{LF}b",
    "crlf": f"a{BS}{CR}{LF}b",
    "lone_cr": f"a{BS}{CR}b",  # the server keeps the CR and drops the backslash
    "two_backslashes": f"a{BS}{BS}{LF}b",  # only the one next to the break is read as a continuation
    "three_backslashes_crlf": f"a{BS}{BS}{BS}{CR}{LF}b",
    "two_breaks": f"a{BS}{LF}b{BS}{CR}{LF}c{BS}{CR}d",
    "only_the_pair": f"{BS}{LF}",
    "pair_then_pair": f"{BS}{LF}{BS}{LF}",
    "break_then_backslash": f"a{LF}{BS}",
    "backslash_last": f"a{BS}",
    "backslash_space_break": f"a{BS} {LF}b",
    "break_without_backslash": f"a{LF}{LF}b{CR}{LF}c",
    "quote": f"it's{BS}{LF}x''",
    "nul": f"a\x00{BS}{LF}b{BS}\x00{CR}{LF}c",
    "long_before_and_after": "x" * 3900 + f"{BS}{LF}" + "y" * 3900,
    "long_crlf": "x" * 7900 + f"{BS}{CR}{LF}" + "y" * 7900,
    "twenty_thousand_chars": f"line one{BS}{LF}line two{BS}{CR}{LF}it's{BS}" * 1000,
    "five_thousand_breaks": f"ab{BS}{LF}" * 5000,
}
UNICODE_SHAPES = {"unicode": f"é😀'{BS}{LF}中文{BS}{CR}{LF}"}


@pytest.fixture(scope="module")
def conn():
    connection = mssql_connect()
    yield connection
    connection.close()


def read(connection: _mssql.MSSQLConnection, sql: str) -> list[tuple]:
    connection.execute_query(sql)
    rows = [tuple(row) for row in connection.get_iterator(_mssql.ROW_FORMAT_TUPLE)]
    while connection.nextresult():
        for _ in connection.get_iterator(_mssql.ROW_FORMAT_TUPLE):
            pass
    return rows


@pytest.mark.parametrize("national", [True, False], ids=["N", "plain"])
@pytest.mark.parametrize("shape", [*SHAPES, *UNICODE_SHAPES])
def test_a_literal_reads_back_as_the_text_it_was_written_from(conn, shape, national):
    text = (SHAPES | UNICODE_SHAPES)[shape]
    if not national and not text.isascii():
        pytest.skip("a non-Unicode literal is only written for ASCII text")
    assert read(conn, f"SELECT {quote_literal(TS, text, national=national)}") == [(text,)]


def test_the_server_really_does_drop_the_pair_from_a_plain_literal(conn):
    """The control: literals written as they are, without the guard — so the tests above prove something."""
    assert read(conn, f"SELECT N'a{BS}{LF}b', N'a{BS}{CR}{LF}b', N'a{BS}{CR}b', N'a{BS}{BS}{LF}b'") == [
        ("ab", "ab", f"a{CR}b", f"a{BS}b")]


def plan_xml(connection: _mssql.MSSQLConnection, sql: str) -> str:
    connection.execute_non_query("SET SHOWPLAN_XML ON")
    try:
        return read(connection, sql)[0][0]
    finally:
        connection.execute_non_query("SET SHOWPLAN_XML OFF")


def test_a_non_unicode_literal_keeps_its_index_seek_across_the_split(conn, tmp_path):  # ⑤ A8, E17
    db = scratch_db("mssql", tmp_path)
    try:
        with dropping(db) as made:
            name = scratch_name("seek", SCRATCH_06C)
            made.append(("TABLE", name))
            table = db.table(name)
            db.execute(f"CREATE TABLE {table} (k varchar(100) NOT NULL PRIMARY KEY, n nvarchar(10) NULL)",
                       f"INSERT INTO {table} VALUES ('a' + CHAR(92) + CHAR(10) + 'b', N'found')",
                       f"INSERT INTO {table} SELECT TOP (2000) CONCAT('k', ROW_NUMBER() OVER (ORDER BY (SELECT NULL))), "
                       "N'v' FROM sys.all_objects a CROSS JOIN sys.all_objects b")
            key = f"a{BS}{LF}b"
            plain = f"SELECT n FROM {table} WHERE k = {quote_literal(TS, key, national=False)}"
            assert read(conn, plain) == [("found",)]
            assert "NVARCHAR" not in plain and "N'" not in plain  # no N anywhere: the key's type is not converted
            plan = plan_xml(conn, plain)
            assert "SeekKeys" in plan and re.search(r'PhysicalOp="Clustered Index Seek"', plan)
            # and the Unicode form of the same key finds it too (it may scan: that is what the plain form is for)
            assert read(conn, f"SELECT n FROM {table} WHERE k = {quote_literal(TS, key)}") == [("found",)]
    finally:
        db.close()


def test_filter_by_this_value_finds_the_row_whose_text_holds_the_pair(conn, tmp_path):  # the same writer as the grid's
    db = scratch_db("mssql", tmp_path)
    try:
        with dropping(db) as made:
            name = scratch_name("filter", SCRATCH_06C)
            made.append(("TABLE", name))
            table = db.table(name)
            db.execute(f"CREATE TABLE {table} (id int PRIMARY KEY, note nvarchar(100) NULL)",
                       f"INSERT INTO {table} VALUES (1, N'a' + NCHAR(92) + NCHAR(10) + N'b'), (2, N'ab'), "
                       "(3, N'a' + NCHAR(92) + NCHAR(13) + N'b')")
            for text, id_ in ((f"a{BS}{LF}b", 1), ("ab", 2), (f"a{BS}{CR}b", 3)):
                condition = value_condition(TS, "note", text, "text")
                assert read(conn, f"SELECT id FROM {table} WHERE {condition}") == [(id_,)], text
    finally:
        db.close()


def test_a_grid_commit_whose_key_value_and_original_hold_the_pair_applies(tmp_path):  # ⑤: no false conflict, no change
    with edit_env("mssql", tmp_path) as (db, app, profile), dropping(db, app.close_tabs) as made:
        name = scratch_name("edit", SCRATCH_06C)
        made.append(("TABLE", name))
        table = db.table(name)
        db.execute(
            f"CREATE TABLE {table} (code varchar(50) NOT NULL PRIMARY KEY, label nvarchar(100) NOT NULL, "
            "note nvarchar(max) NULL)",
            f"INSERT INTO {table} VALUES ('k' + CHAR(92) + CHAR(10) + '1', N'old' + NCHAR(92) + NCHAR(10) + N'text', "
            "N'n' + NCHAR(92) + NCHAR(13) + NCHAR(10) + N'1')",
            f"INSERT INTO {table} VALUES ('gone' + CHAR(92) + CHAR(13) + CHAR(10) + '2', N'x', NULL)",
            f"INSERT INTO {table} VALUES ('keep', N'k' + NCHAR(92) + NCHAR(13) + N'r', N'untouched')",
        )
        preview = app.preview(profile, db.path(name), updates=[
            update("u:1", {"code": f"k{BS}{LF}1"},
                   {"label": val(f"new{BS}{LF}text"), "note": val(f"n{BS}{CR}{LF}2")},
                   {"label": f"old{BS}{LF}text", "note": f"n{BS}{CR}{LF}1"}),
        ], inserts=[
            insert("i:1", {"code": val(f"fresh{BS}{LF}3"), "label": val(f"ins{BS}{CR}{LF}row"),
                           "note": val(f"c{BS}{CR}d")}),
        ], deletes=[
            delete("d:1", {"code": f"gone{BS}{CR}{LF}2"}),
        ])
        assert preview.problems == [] and preview.plan_id is not None
        assert all("CONCAT(" in statement.sql for statement in preview.statements)  # each holds the pair somewhere
        done = app.tab(profile, db.database).apply(preview.plan_id)
        rows = db.rows(f"SELECT code, label, note FROM {table} ORDER BY code")
    assert (done.outcome.outcome, done.error) == ("committed", None), done.outcome
    assert rows == [
        (f"fresh{BS}{LF}3", f"ins{BS}{CR}{LF}row", f"c{BS}{CR}d"),
        (f"k{BS}{LF}1", f"new{BS}{LF}text", f"n{BS}{CR}{LF}2"),
        ("keep", f"k{BS}{CR}r", "untouched"),
    ]


def run(app: DataApp, profile, database: str, make) -> Ran:
    return app.job_session(profile, database).run(make, timeout=300)


def test_a_dump_of_text_with_the_pair_restores_equal(tmp_path):  # 06a: the dump's INSERTs are the same writer
    db = scratch_db("mssql", tmp_path)
    app = DataApp(tmp_path)
    try:
        with scratch_database("mssql", "bsdump", SCRATCH_06C) as (source, admin), \
                scratch_database("mssql", "bsrestore", SCRATCH_06C) as (target, _):
            admin.run_in(
                source,
                "CREATE TABLE dbo.texts (id int PRIMARY KEY, vc varchar(100) NULL, vm varchar(max) NULL, "
                "nvc nvarchar(100) NULL, note nvarchar(max) NULL)",
                "INSERT INTO dbo.texts VALUES "
                "(1, 'a' + CHAR(92) + CHAR(10) + 'b', 'a' + CHAR(92) + CHAR(13) + CHAR(10) + 'b', "
                "N'é' + NCHAR(92) + NCHAR(13) + NCHAR(10) + N'😀', N'x' + NCHAR(92) + NCHAR(13) + N'y')",
                "INSERT INTO dbo.texts VALUES "
                "(2, 'a' + CHAR(92) + CHAR(92) + CHAR(10), 'ab' + CHAR(92), N'' + NCHAR(92) + NCHAR(10), "
                "REPLICATE(CAST(N'line' + NCHAR(92) + NCHAR(10) AS nvarchar(max)), 3000))",
                "INSERT INTO dbo.texts VALUES (3, NULL, "
                "REPLICATE(CAST('ab' + CHAR(92) + CHAR(10) AS varchar(max)), 2000), "
                "N'a' + NCHAR(92) + N' ' + NCHAR(10) + N'b', N'')",
            )
            profile = app.add_profile(db.profile(database=source))
            path = tmp_path / "texts.sql"
            scope = DumpScope(kind="tables", schema_name="dbo", names=["texts"])
            dumped = run(app, profile, source, lambda rid: DumpCommand(request_id=rid, path=str(path), scope=scope))
            assert (dumped.outcome.outcome, dumped.outcome.rows, dumped.error) == ("completed", 3, None)
            assert "CONCAT(" in path.read_text(encoding="utf-8")  # the pair was written as two literals
            app.close_sessions()
            restored = app.add_profile(db.profile(database=target))
            ran = run(app, restored, target, lambda rid: RunFileCommand(request_id=rid, path=str(path),
                                                                         mode="all_or_nothing"))
            assert (ran.outcome.outcome, ran.outcome.error_count, ran.error) == ("completed", 0, None), ran.outcome
            select = "SELECT id, vc, vm, nvc, note FROM dbo.texts ORDER BY id"
            before, after = admin.rows(source, select), admin.rows(target, select)
            assert before[0][1] == f"a{BS}{LF}b" and len(before[1][4]) == 3000 * 6  # the source holds the pairs
            assert after == before
            app.close_sessions()
    finally:
        app.close()
        db.close()
