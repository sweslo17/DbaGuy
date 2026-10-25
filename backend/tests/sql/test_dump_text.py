"""The text a dump writes (spec ⑥ §3.3.2, D-06-5, D-06-6, D-06-7): multi-row INSERTs and their limits, each cell's
literal, the SELECT that reads the rows, and the file's sections, settings and comments. Pure."""

import math
from datetime import date, datetime
from decimal import Decimal

import pytest

from dbaguy_backend.adapters.columns import SQL_VARIANT, TableColumn
from dbaguy_backend.adapters.dump import DumpCatalog, DumpDependent, DumpObject, DumpTable
from dbaguy_backend.domain.errors import ValidationFailed
from dbaguy_backend.domain.profiles import Engine
from dbaguy_backend.sql import bulk_insert
from dbaguy_backend.sql.bulk_insert import MYSQL_PACKET_BYTES, InsertBatcher, size_warning
from dbaguy_backend.sql.dialects import Dialect, datetime_literal, float_literal, identity_insert, non_finite_literal
from dbaguy_backend.sql.dump_header import comment_safe, filtered_rows, parse_header
from dbaguy_backend.sql.dump_script import (
    DumpScript, drop_warnings, filter_warnings, insert_text, lost_columns, object_text, restore_warnings,
    statement_text, table_sql,
)
from dbaguy_backend.sql.select_builder import build_copy_select
from dbaguy_backend.sql.splitter import split

PG, MY, TS, LITE = Dialect.POSTGRES, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLITE


def col(name: str, kind: str = "number", **fields) -> TableColumn:
    fields.setdefault("family", "int64")
    return TableColumn(name=name, type_name="t", nullable=True, has_default=False, kind=kind, compare="equals",
                       writable=True, **fields)


def test_an_insert_writes_each_cell_as_its_columns_literal():
    columns = [col("id"), col("label", "text"), col("ok", "bool", family="bool"), col("data", "binary"),
               col("at", "datetime"), col("doc", "json"), col("code", "text", unicode=False)]
    batcher = InsertBatcher(TS, "[dbo].[t]", columns)
    batcher.add([1, "it's 中文", True, b"\x00\xff", "2026-10-05 12:34:56.123", '{"a": 1}', "abc"])
    batcher.add([2, None, False, None, None, None, "中"])
    assert batcher.flush() == (
        "INSERT INTO [dbo].[t] (id, label, ok, data, at, doc, code) VALUES\n"
        "(1, N'it''s 中文', 1, 0x00ff, N'2026-10-05T12:34:56.123', N'{\"a\": 1}', 'abc'),\n"
        "(2, NULL, 0, NULL, NULL, NULL, N'中')")  # a non-Unicode column: ASCII without N, other text with it
    assert batcher.flush() is None


@pytest.mark.parametrize(("dialect", "escapes", "expected"), [
    (MY, True, "('a\\\\b', 'x\\0y')"),  # the header's sql_mode: backslash escapes
    (PG, None, "('a\\b', NULL)"),  # PostgreSQL cannot hold U+0000: the cell is never one
    (LITE, None, "('a\\b', 'x' || char(0) || 'y')"),
])
def test_text_is_escaped_for_the_dialect_and_mode(dialect, escapes, expected):
    batcher = InsertBatcher(dialect, "t", [col("a", "text"), col("b", "text")], backslash_escapes=escapes)
    assert batcher.row_text(["a\\b", None if dialect is PG else "x\x00y"]) == expected


def test_numbers_dates_and_decimals_keep_every_digit():
    batcher = InsertBatcher(PG, "t", [col("a"), col("b"), col("c"), col("d", "datetime"), col("e", "datetime")])
    row = batcher.row_text([2**63 - 1, "12345678901234567890.1234567890", Decimal("1.10"), date(2026, 10, 5),
                            datetime(2026, 10, 5, 1, 2, 3, 456789)])
    assert row == "(9223372036854775807, 12345678901234567890.1234567890, 1.10, '2026-10-05', '2026-10-05T01:02:03.456789')"


def test_numbers_stay_numbers_in_a_column_without_a_number_kind():
    """A SQLite column without a declared type ('other') holds integers past 2^53 and reals as themselves: written as
    text they would come back as text. A Decimal goes as its plain digits (1E-7 would be read as a double)."""
    batcher = InsertBatcher(LITE, "t", [col("v", "other"), col("w", "other"), col("x", "other"), col("y", "other")])
    assert batcher.row_text([2**63 - 1, 1.5, "007", math.inf]) == "(9223372036854775807, 1.5, '007', 9e999)"
    assert InsertBatcher(TS, "t", [col("d")]).row_text([Decimal("1.234E-7")]) == "(0.0000001234)"
    assert InsertBatcher(TS, "t", [col("d")]).row_text([Decimal("NaN")]) == "(N'NaN')"


def test_non_finite_floats_are_written_as_each_dialect_reads_them():  # R-06-11, L3
    sqlite = InsertBatcher(LITE, "t", [col("a"), col("b"), col("c"), col("d")])
    assert sqlite.row_text([math.inf, -math.inf, math.nan, 1.5]) == "(9e999, -9e999, NULL, 1.5)"
    pg = InsertBatcher(PG, "t", [col("a"), col("b")])
    assert pg.row_text([math.inf, math.nan]) == "('inf', 'nan')"
    assert non_finite_literal(MY, "inf") is None
    # text that only looks like one stays text: a SQLite REAL column can hold the string 'inf'
    assert sqlite.row_text(["inf", "-inf", "nan", "x"]) == "('inf', '-inf', 'nan', 'x')"


def test_negative_zero_keeps_its_sign():  # 06a Task 4: a plain -0.0 is an exact numeric, whose zero has no sign
    assert float_literal(MY, -0.0) == "-0.0e0" and float_literal(MY, 0.0) == "0.0"
    assert InsertBatcher(PG, "t", [col("f")]).row_text([-0.0]) == "('-0.0')"
    assert float_literal(LITE, -0.0) == "-0.0" and float_literal(PG, 0.1) == "0.1" and float_literal(MY, 1e16) == "1e+16"


def test_a_statement_holds_at_most_1000_rows_or_100_on_sql_server():
    for dialect, limit in ((PG, 1000), (TS, 100), (MY, 1000), (LITE, 1000)):
        batcher = InsertBatcher(dialect, "t", [col("id")])
        statements = [s for i in range(2 * limit + 1) if (s := batcher.add([i])) is not None]
        statements.append(batcher.flush())
        assert [s.count("\n") for s in statements] == [limit, limit, 1], dialect


def test_a_statement_holds_at_most_a_mebibyte_and_a_longer_row_is_alone():
    batcher = InsertBatcher(PG, "t", [col("id"), col("v", "text")], max_chars=100)
    out = [batcher.add([1, "a" * 40]), batcher.add([2, "b" * 40]), batcher.add([3, "c" * 300]), batcher.add([4, "d"])]
    out.append(batcher.flush())
    assert [s is None for s in out] == [True, True, False, False, False]
    assert [s.count("\n") for s in out if s] == [2, 1, 1]  # 1 and 2 together; 3 alone; 4 alone at the end
    assert "c" * 300 in out[3]
    assert batcher.row_chars == 2 * 47 + 307 + 8  # what a dump sizes its next fetch by (pre-flight M12)


def test_a_row_too_large_to_restore_is_said(monkeypatch):  # R-06-11, pre-flight M11
    assert MYSQL_PACKET_BYTES == 64 * 1_048_576
    monkeypatch.setattr(bulk_insert, "MYSQL_PACKET_BYTES", 4096)
    batcher = InsertBatcher(MY, "t", [col("id"), col("v", "text")], backslash_escapes=True)
    batcher.add([1, "x" * 5000])
    statement = batcher.flush()
    assert size_warning(MY, "big", statement) == (
        "big has an INSERT statement of 4.9 KiB: MySQL refuses statements over its max_allowed_packet (64 MiB by "
        "default) when the file is run")
    assert size_warning(MY, "big", statement, packet_bytes=4096) == (  # the dumped server's own (Task 9 fix L5)
        "big has an INSERT statement of 4.9 KiB: MySQL refuses statements over its max_allowed_packet (4 KiB on the "
        "server dumped) when the file is run")
    assert size_warning(PG, "big", statement) is None
    assert size_warning(MY, "t", "中" * 1200) is None and size_warning(MY, "t", "中" * 1400) is not None  # UTF-8 bytes
    monkeypatch.setattr(bulk_insert, "MAX_STATEMENT_CHARS", 1000)
    assert size_warning(LITE, "big", statement) == (
        "big has an INSERT statement over 1 KiB: Run SQL file cannot restore it")


def test_an_identity_written_with_its_values():
    columns = [col("id", identity="always"), col("v", "text")]
    assert InsertBatcher(PG, "t", columns).add([1, "a"]) is None
    batcher = InsertBatcher(PG, "s.t", columns)
    batcher.add([1, "a"])
    assert batcher.flush().startswith("INSERT INTO s.t (id, v) OVERRIDING SYSTEM VALUE VALUES\n")
    assert InsertBatcher(PG, "t", [col("id", identity="by_default")])._head == "INSERT INTO t (id) VALUES\n"
    assert identity_insert(TS, "[s].[t]") == ("SET IDENTITY_INSERT [s].[t] ON", "SET IDENTITY_INSERT [s].[t] OFF")
    assert identity_insert(MY, "t") is None


def test_the_rows_are_read_by_the_columns_a_dump_writes():  # D-06-6
    columns = [col("id"), col("Total"), col("m", read_as="CAST({} AS numeric)"),
               col("gen", copied=False, not_copied_reason="generated column"), col("order", "text")]
    assert build_copy_select(PG, "public", "orders", columns, only=True) == (
        'SELECT id, "Total", CAST(m AS numeric) AS m, "order" FROM ONLY public.orders')
    assert build_copy_select(TS, "dbo", "t", [col("t7", read_as="CONVERT(varchar(16), {}, 126)")],
                             where="t7 > '12:00' -- noon") == (
        "SELECT CONVERT(varchar(16), t7, 126) AS t7 FROM dbo.t\nWHERE t7 > '12:00'")
    with pytest.raises(ValidationFailed):
        build_copy_select(PG, "public", "t", [col("id")], where="1 = 1; DROP TABLE t")
    with pytest.raises(ValidationFailed):
        build_copy_select(PG, "public", "t", [col("g", copied=False)])


def test_datetimeoffset_text_with_a_space_before_its_offset():  # A15: 241 with the T and the space
    assert datetime_literal(TS, "2026-10-05 12:34:56.1234567 +08:00") == "N'2026-10-05T12:34:56.1234567+08:00'"
    assert datetime_literal(TS, "2026-10-05 12:34:56 -05:30") == "N'2026-10-05T12:34:56-05:30'"
    assert datetime_literal(PG, "2026-10-05 12:34:56 +08:00") == "'2026-10-05 12:34:56 +08:00'"


def catalog(**fields) -> DumpCatalog:
    table = DumpTable(schema="public", table="t", name="public.t",
                      columns=(col("id", identity="always"), col("v", copied=False, not_copied_reason=SQL_VARIANT)))
    defaults = dict(
        server="PostgreSQL 17.11", scope="database d · schema public", consistency="snapshot",
        pre=(DumpObject("table", "public.t", ("CREATE TABLE public.t (id int)",), "DROP TABLE IF EXISTS public.t"),),
        tables=(table,),
        post=(DumpObject("sequence value", "public.t_id_seq", ("SELECT setval('s', 3, true)",), section="data"),
              DumpObject("view", "public.v", ("CREATE VIEW public.v AS SELECT 1",), "DROP VIEW IF EXISTS public.v"),
              DumpObject("index", "public.t_i", ("CREATE INDEX t_i ON public.t (id)",))),
    )
    return DumpCatalog(**{**defaults, **fields})


def whole(script: DumpScript, rows: int = 2) -> str:
    table = script.catalog.tables[0]
    data = script.table_head(table) + "INSERT INTO public.t (id) VALUES\n(1),\n(2);\n" + script.table_tail(table, rows)
    return script.head() + script.pre() + (data if script.data else "") + script.post() + script.tail(rows)


def test_the_file_reads_back_as_the_statements_it_was_written_as():
    script = DumpScript(PG, Engine.POSTGRES, catalog(), content="both", drop_first=True, created="2026-10-20T12:00:00Z")
    text = whole(script)
    assert [s.text for s in split(text, PG)] == [
        "SET client_encoding = 'UTF8'", "SET standard_conforming_strings = on", "SET check_function_bodies = false",
        "DROP VIEW IF EXISTS public.v", "DROP TABLE IF EXISTS public.t",  # every drop first, in reverse order
        "CREATE TABLE public.t (id int)", "INSERT INTO public.t (id) VALUES\n(1),\n(2)",
        "SELECT setval('s', 3, true)", "CREATE VIEW public.v AS SELECT 1", "CREATE INDEX t_i ON public.t (id)"]
    header = parse_header(text)
    assert (header.engine, header.drop_first) == (Engine.POSTGRES, True)
    assert "-- Content: schema and data · 1 table, 1 view" in text
    assert "-- Not included: owners and privileges · the values of sql_variant column public.t.v" in text
    assert text.endswith("-- Dump complete: 2 rows in 1 tables\n") and "-- public.t: 2 rows\n" in text


def test_schema_only_and_data_only():
    schema = whole(DumpScript(PG, Engine.POSTGRES, catalog(), content="schema", drop_first=False, created="x"))
    assert "INSERT" not in schema and "setval" not in schema and "CREATE VIEW" in schema
    data = whole(DumpScript(PG, Engine.POSTGRES, catalog(), content="data", drop_first=True, created="x"))
    assert "CREATE" not in data and "DROP" not in data and "setval" in data and "-- Content: data only · 1 table" in data
    assert parse_header(data).drop_first is False  # a data-only file drops nothing, and its header says so


def test_only_what_the_file_creates_again_is_dropped():
    unwritten = DumpObject("view", "public.w", (), "DROP VIEW IF EXISTS public.w")  # no definition: never dropped
    text = DumpScript(PG, Engine.POSTGRES, catalog(post=(unwritten,)), content="both", drop_first=True,
                      created="x").head()
    assert "public.w" not in text and "DROP TABLE IF EXISTS public.t;\n" in text


def test_sql_server_batches_and_its_identity_switch():
    script = DumpScript(TS, Engine.MSSQL, catalog(), content="both", drop_first=False, created="x")
    table = script.catalog.tables[0]
    assert script.table_head(table).endswith("SET IDENTITY_INSERT [public].t ON\nGO\n")  # public: reserved in T-SQL
    assert script.table_tail(table, 3) == "SET IDENTITY_INSERT [public].t OFF\nGO\n-- public.t: 3 rows\n"
    assert statement_text(TS, "SELECT 1") == "SELECT 1\nGO\n" == insert_text(TS, "SELECT 1")
    assert table_sql(TS, table) == "[public].t" and table_sql(MY, table) == "t"
    assert "NOCOUNT" not in script.head()  # S21: under it no INSERT reports its rows


def test_mysql_stored_programs_go_between_delimiter_lines():
    program = DumpObject("procedure", "p", ("SET SESSION sql_mode = ''", "CREATE PROCEDURE p() BEGIN SELECT 1; END"),
                         compound=True)
    text = object_text(MY, program)
    assert text == "DELIMITER ;;\nSET SESSION sql_mode = '';;\nCREATE PROCEDURE p() BEGIN SELECT 1; END;;\nDELIMITER ;\n"
    assert [s.text for s in split(text, MY)] == ["SET SESSION sql_mode = ''", "CREATE PROCEDURE p() BEGIN SELECT 1; END"]
    script = DumpScript(MY, Engine.MYSQL, catalog(pre=(), post=()), content="schema", drop_first=False, created="x")
    assert script.tail(0).startswith("\nSET FOREIGN_KEY_CHECKS = 1;\n")
    assert "SET SESSION sql_mode = 'STRICT_ALL_TABLES,NO_AUTO_VALUE_ON_ZERO';\nSET time_zone = '+00:00';\n" in (
        script.head())


@pytest.mark.parametrize(("dialect", "statements", "compound"), [
    (LITE, ("CREATE VIEW v AS SELECT 1 -- the view's own comment", "CREATE INDEX i ON t (a) -- c\r"), False),
    (LITE, ("CREATE VIEW v AS SELECT 1 /* left open", "CREATE VIEW w AS SELECT 2"), False),
    (MY, ("SET SESSION sql_mode = ''", "CREATE PROCEDURE p() BEGIN SELECT 1; END # done", "SET NAMES utf8mb4"), True),
    (MY, ("CREATE PROCEDURE p() BEGIN SELECT 1; END -- done",), True),
    (MY, ("CREATE VIEW v AS SELECT 1 /*!50001 'open", "SELECT 2"), False),  # closing it does not end it: left out
    (PG, ("CREATE VIEW v AS SELECT 1 /* a /* b */", "CREATE VIEW w AS SELECT 2 -- c"), False),  # nested
    (TS, ("CREATE VIEW v AS SELECT 1 /* open", "SELECT 2 -- c"), False),
])
def test_a_trailing_comment_never_takes_the_terminator(dialect, statements, compound):  # 06a Task 6 review T9
    item = DumpObject("view", "v", statements, compound=compound)
    text = object_text(dialect, item) + statement_text(dialect, "SELECT 3")
    got = [s.text for s in split(text, dialect)]
    assert len(got) == len(statements) + 1 and got[-1] == "SELECT 3", got
    assert all(not s.rstrip().endswith((";", ";;")) for s in got)


def test_what_ends_a_trailing_comment():
    assert statement_text(LITE, "CREATE VIEW v AS SELECT 1 -- c") == "CREATE VIEW v AS SELECT 1 -- c\n;\n"
    assert statement_text(LITE, "CREATE VIEW v AS SELECT 1 /* open") == "CREATE VIEW v AS SELECT 1 /* open */;\n"
    assert statement_text(PG, "SELECT 1 /* a /* b") == "SELECT 1 /* a /* b */ */;\n"  # PostgreSQL nests them
    assert statement_text(MY, "SELECT 1 /*!50001 'open") == "SELECT 1;\n"  # closing it ends nothing: left out
    assert statement_text(TS, "SELECT 1 -- c") == "SELECT 1 -- c\n\nGO\n"
    assert insert_text(PG, "INSERT INTO t (a) VALUES\n(1)") == "INSERT INTO t (a) VALUES\n(1);\n"


def test_a_name_with_a_line_break_never_leaves_its_comment():  # Review Focus: names that break comments
    evil = 'x\nDROP TABLE victim;--\r'
    table = DumpTable(schema="public", table=evil, name=f"public.{evil}", columns=(col("id"),))
    script = DumpScript(PG, Engine.POSTGRES, catalog(tables=(table,), scope=f"schema {evil}", pre=(), post=()),
                        content="data", drop_first=False, created="x")
    text = script.head() + script.table_head(table) + script.table_tail(table, 0) + script.tail(0)
    assert [s.text for s in split(text, PG)] == [
        "SET client_encoding = 'UTF8'", "SET standard_conforming_strings = on", "SET check_function_bodies = false"]
    assert lost_columns(catalog()) == ["public.t.v"]


def test_every_line_break_python_reads_is_written_as_text():  # S15: parse_header reads the header with splitlines
    breaks = "\n\r\x0b\x0c\x1c\x1d\x1e\x85  "
    assert comment_safe(f"a{breaks}\x00b").splitlines() == [comment_safe(f"a{breaks}\x00b")]
    assert comment_safe("a\nb c") == "a\\nb\\u2028c"
    script = DumpScript(LITE, Engine.SQLITE, catalog(scope="schema a b", pre=(), post=()), content="schema",
                        drop_first=True, created="x")
    header = parse_header(script.head())
    assert header.scope == "schema a\\u2028b" and header.lines[-1] == "-- Options: drop objects first: yes"


HOSTILE_FILTER = "v = 'a'\nDROP TABLE victim; -- */ x\r y */ that match a filter: z\x00"
REPLACES = ("The file holds only the rows of public.t that match a filter: restoring it drops the table and replaces "
            "it with those rows")


@pytest.mark.parametrize(("dialect", "engine"), [(PG, Engine.POSTGRES), (MY, Engine.MYSQL), (TS, Engine.MSSQL),
                                                 (LITE, Engine.SQLITE)])
def test_a_filtered_file_says_so_in_its_header_and_its_filter_stays_in_the_comment(dialect, engine):  # fix wave F2
    text = whole(DumpScript(dialect, engine, catalog(), content="both", drop_first=True, created="x",
                            filter=HOSTILE_FILTER))
    header = parse_header(text)
    assert (header.filtered, header.filtered_table, header.drop_first) == (True, "public.t", True)
    rows = comment_safe(f"-- Rows: only those of public.t that match a filter: {HOSTILE_FILTER}")
    assert rows in header.lines and rows.count("\n") == 0 and text.count("victim") == 1
    assert not any("victim" in s.text for s in split(text, dialect))  # it never runs as SQL
    data = parse_header(whole(DumpScript(dialect, engine, catalog(), content="data", drop_first=True, created="x",
                                         filter="id < 3")))
    assert (data.filtered, data.filtered_table, data.drop_first) == (True, "public.t", False)
    for script in (DumpScript(dialect, engine, catalog(), content="schema", drop_first=True, created="x",
                              filter="id < 3"),  # no rows: nothing to say of them
                   DumpScript(dialect, engine, catalog(), content="both", drop_first=True, created="x")):
        header = parse_header(whole(script))
        assert (header.filtered, header.filtered_table) == (False, None)
        assert not any(line.startswith("-- Rows:") for line in header.lines)


def test_a_filtered_file_that_drops_its_table_is_said_to_replace_it():  # fix wave F2: the plan's and outcome's
    assert filter_warnings(catalog(), "both", True, "id < 3") == [REPLACES]
    for content, drop_first, where in [("both", False, "id < 3"), ("data", True, "id < 3"),
                                       ("schema", True, "id < 3"), ("both", True, None)]:
        assert filter_warnings(catalog(), content, drop_first, where) == []
    assert filter_warnings(catalog(tables=()), "both", True, "id < 3") == []  # a view's: no rows written
    assert filtered_rows("public.t", replaces=False) == "The file holds only the rows of public.t that match a filter"


def blocked(target: str, kind: str, name: str) -> str:
    return (f"Drop objects first: {name} ({kind}, not in this dump) depends on {target}, so restoring where it exists "
            "fails at that DROP. Dump it too, or turn Drop objects first off.")


def test_a_drop_first_file_says_what_its_drops_run_into():  # 06b I2: the plan's and the outcome's
    view = DumpDependent(target="public.t", kind="view", name="other.v_t")
    assert drop_warnings(catalog(), "both", True) == []  # nothing depends on what it drops
    assert drop_warnings(catalog(dependents=(view,)), "both", True) == [blocked("public.t", "view", "other.v_t")]
    # only a file that drops: not without the option, nor a data-only file (it creates and drops nothing)
    for content, drop_first in [("both", False), ("data", True), ("schema", False)]:
        assert drop_warnings(catalog(dependents=(view,)), content, drop_first) == []
    assert drop_warnings(catalog(dependents=(view,)), "schema", True) == [blocked("public.t", "view", "other.v_t")]
    # a view of the scope the dump leaves out (views not chosen) is one like any other; one per object dropped, in the
    # catalog's order; an object the file writes without a DROP of its own (an index goes with its table) is no target
    left_out = DumpDependent(target="public.v", kind="view", name="public.w")
    key = DumpDependent(target="public.t", kind="foreign key", name="orders_t_fkey on other.orders")
    index = DumpDependent(target="public.t_i", kind="view", name="other.x")
    assert drop_warnings(catalog(dependents=(view, left_out, key, index, view)), "both", True) == [
        "Drop objects first: other.v_t (view), orders_t_fkey on other.orders (foreign key), not in this dump, depend "
        "on public.t, so restoring where they exist fails at that DROP. Dump them too, or turn Drop objects first off.",
        blocked("public.v", "view", "public.w")]


def test_the_drops_warning_names_its_dependents_up_to_the_cap():
    many = tuple(DumpDependent(target="public.t", kind="view", name=f"s.v{i:02}") for i in range(12))
    [said] = drop_warnings(catalog(dependents=many), "both", True)
    shown = ", ".join(f"s.v{i:02} (view)" for i in range(10))
    assert said == (f"Drop objects first: {shown} …, not in this dump, depend on public.t, so restoring where they "
                    "exist fails at that DROP. Dump them too, or turn Drop objects first off.")


def test_restoring_warnings_put_the_filter_first_then_the_drops():
    view = DumpDependent(target="public.t", kind="view", name="other.v_t")
    assert restore_warnings(catalog(dependents=(view,)), "both", True, "id < 3") == [
        REPLACES, blocked("public.t", "view", "other.v_t")]
    assert restore_warnings(catalog(dependents=(view,)), "both", False, "id < 3") == []


def test_a_partition_is_counted_apart_from_the_tables():  # F-a: its rows are read through its parent
    partition = DumpObject("partition", "public.t_1", ("CREATE TABLE public.t_1 PARTITION OF public.t FOR VALUES IN (1)",))
    script = DumpScript(PG, Engine.POSTGRES, catalog(pre=(*catalog().pre, partition)), content="both", drop_first=True,
                        created="x")
    text = whole(script)
    assert "-- Content: schema and data · 1 table, 1 partition, 1 view" in text
    assert "-- partition: public.t_1" in text and "DROP TABLE IF EXISTS public.t_1" not in text
    assert text.rstrip().endswith("-- Dump complete: 2 rows in 1 tables")
    assert parse_header(text).lines[3] == "-- Content: schema and data · 1 table, 1 partition, 1 view"


def test_a_table_with_no_column_to_write_gets_rows_of_defaults():  # 06a Task 7 review, Task 9
    from dbaguy_backend.sql.dump_script import MAX_DEFAULT_ROWS, default_rows_text

    def table(schema, name):
        return DumpTable(schema=schema, table=name, name=name, columns=())

    assert list(default_rows_text(PG, table("s", "t"), 0)) == []
    assert list(default_rows_text(PG, table("s", "t"), 5)) == ["INSERT INTO s.t SELECT FROM generate_series(1, 5);\n"]
    mysql = list(default_rows_text(MY, table("db", "t"), MAX_DEFAULT_ROWS + 2))
    assert [text.count("()") - 1 for text in mysql] == [MAX_DEFAULT_ROWS, 2]  # `t ()` and each row's `()`
    assert mysql[1] == "INSERT INTO t () VALUES (), ();\n"
    assert list(default_rows_text(TS, table("dbo", "t t"), 3)) == ["INSERT INTO dbo.[t t] DEFAULT VALUES\nGO 3\n"]
    assert list(default_rows_text(TS, table("dbo", "t"), 2003)) == [  # GO n at most MAX_GO_REPEAT (Task 9 fix L4)
        "INSERT INTO dbo.t DEFAULT VALUES\nGO 1000\n"] * 2 + ["INSERT INTO dbo.t DEFAULT VALUES\nGO 3\n"]
    [(text, repeat)] = [(s.text, s.repeat) for s in split("SELECT 1\nGO\n" + "".join(
        default_rows_text(TS, table("dbo", "t"), 3)), TS) if "DEFAULT" in s.text]
    assert (text, repeat) == ("INSERT INTO dbo.t DEFAULT VALUES", 3)
    sqlite = "".join(default_rows_text(LITE, table("main", "t"), 3))
    assert [s.text for s in split(sqlite, LITE)] == ["INSERT INTO t DEFAULT VALUES"] * 3


@pytest.mark.parametrize("dialect", [PG, MY, TS, LITE])
def test_the_column_whose_value_a_file_cannot_hold_is_found(dialect):  # pre-flight L15
    batcher = InsertBatcher(dialect, "t", [col("id"), col("note", "text"), col("other", "text")])
    batcher.add([1, "a, (b) 'c'", None])
    batcher.add([2, "fine", "half \ud83d"])
    statement = batcher.flush()
    bad = next(i for i, ch in enumerate(statement) if "\ud800" <= ch <= "\udfff")
    assert batcher.column_at(statement, bad) == "other"
    assert batcher.column_at(statement, 0) is None  # the head: a name
    nested = batcher._head + "(1, CONVERT(int, 2), 'half \ud83d')"  # a value's own commas are not a column's
    assert batcher.column_at(nested, len(nested) - 3) == "other"
