"""The copy matrix (06c Task 5, spec ⑥ A9): a type-complete table of each relational engine copied into a new table
on each of them (16 copies, the same engine included), and every value read back with the target's own driver equal to
the source's by what it means (`copy_matrix.canon`) — a time zone's value by its instant and, where the target keeps
it, its offset; JSON by its exact text, MySQL's JSON by the normalisation the preview announces. Then SQL Server's 7th
fractional digit into each of the four. MySQL's connections read in UTC (a TIMESTAMP is shown in the session's zone).
Scratch tables `zz_06c_*`, dropped by exact name."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from dbaguy_backend.domain.execution import CopyDataCommand
from dbaguy_backend.domain.transfer import TransferPlan
from dbaguy_backend.sql.type_map import FRACTION_NOTE, MYSQL_JSON_NOTE
from tests.copy_matrix import (
    BACKSLASH_BREAKS, READ_AS, SEVENTH, SEVENTH_KEPT, SEVENTH_ROUNDED, TYPES, canon, compared_as, expected,
)
from tests.data_support import DataApp, dropping
from tests.edit_support import ENGINES, ScratchDb, scratch_db
from tests.support import requires_mssql
from tests.transfer_support import profile, table_name, tables_request


@dataclass
class Copied:
    src: ScratchDb
    name: str  # the source's table
    dst: ScratchDb
    copy: str  # the copy
    plan: TransferPlan


@contextmanager
def _copied(tmp_path: Path, source: str, target: str, ddl: str, rows: list[str]) -> Iterator[Copied]:
    """`ddl`'s table made and filled on `source`, then copied into a new table of `target`: both are there to read
    until the block ends, when every scratch table is dropped by exact name."""
    (tmp_path / "s").mkdir()
    (tmp_path / "t").mkdir()
    app = DataApp(tmp_path)
    dbs: list[ScratchDb] = []  # opened one by one: one that fails to open leaves no other behind
    try:
        for engine, directory in ((source, tmp_path / "s"), (target, tmp_path / "t")):
            dbs.append(scratch_db(engine, directory))
            if engine == "mysql":
                dbs[-1].execute("SET SESSION time_zone = '+00:00'")  # the copy reads and writes UTC
        src, dst = dbs
        with dropping(src) as src_made, dropping(dst, before_drop=app.close_sessions) as dst_made:
            name, copy = table_name("mx"), table_name("mxc")
            src.execute(ddl.replace("{t}", src.table(name)))
            src_made.append(("TABLE", name))
            for row in rows:
                src.execute(row.replace("{t}", src.table(name)).replace("{{", "{").replace("}}", "}"))
            into = profile(app, dst, "dst")
            plan = app.container.transfers.plan(tables_request(src, profile(app, src, "src"), [name], dst, into,
                                                               table=copy))
            assert plan.plan_id is not None, [p.message for t in plan.tables for p in t.problems]
            tab = app.job_session(into, dst.database)
            dst_made.append(("TABLE", copy))
            ran = tab.run(lambda rid: CopyDataCommand(request_id=rid, plan_id=plan.plan_id))
            assert (ran.outcome.outcome, ran.outcome.rows) == ("completed", len(rows)), ran.error
            yield Copied(src, name, dst, copy, plan)
    finally:
        for db in dbs:
            db.close()
        app.close()


def _read(db: ScratchDb, table: str, columns: list[str]) -> list[tuple]:
    reads = READ_AS.get(db.engine, {})
    picked = ", ".join(reads[column].format(db.q(column)) if column in reads else db.q(column) for column in columns)
    return db.rows(f"SELECT {picked} FROM {db.table(table)} ORDER BY {db.q('id')}")


@pytest.mark.parametrize("target", ENGINES)
@pytest.mark.parametrize("source", ENGINES)
def test_every_value_of_a_type_complete_table_reads_back_as_itself(tmp_path: Path, source: str, target: str) -> None:
    ddl, rows, families = TYPES[source]
    columns = list(families)
    with _copied(tmp_path, source, target, ddl, rows) as copied:
        every = copied.src.rows(f"SELECT * FROM {copied.src.table(copied.name)}")
        before = _read(copied.src, copied.name, columns)
        after = _read(copied.dst, copied.copy, columns)
    assert len(every[0]) == len(columns), "`families` names every column of the table"
    assert len(after) == len(before)
    for number, (row_before, row_after) in enumerate(zip(before, after, strict=True)):
        for column, was, now in zip(columns, row_before, row_after, strict=True):
            family = families[column]
            want = expected(source, target, column, family, number, was)
            as_family = compared_as(family, source, target)
            assert canon(now, as_family) == canon(want, as_family), (column, was, now)
    # MySQL's JSON is the one column that changes a value on purpose, and the preview says so there and nowhere else
    for column in copied.plan.tables[0].columns:
        says = MYSQL_JSON_NOTE in (column.conversion or "")
        assert says == (families[column.source_name] == "json" and target == "mysql" and source != "mysql"), (
            column.source_name, column.conversion)


@requires_mssql
@pytest.mark.parametrize("target", ENGINES)
def test_a_seventh_fraction_digit_is_kept_where_the_target_holds_it_and_rounded_where_it_does_not(
    tmp_path: Path, target: str,
) -> None:
    ddl, rows = SEVENTH
    with _copied(tmp_path, "mssql", target, ddl, rows) as copied:
        # SQL Server's driver reads microseconds only: read its three columns as ISO text, so the 7th digit shows
        picked = ", ".join(f"CONVERT(varchar(40), {c}, 126)" if target == "mssql" else c for c in ("tm", "dt2", "dto"))
        after = copied.dst.rows(f"SELECT {picked} FROM {copied.dst.table(copied.copy)} ORDER BY id")
    rounds = target in ("postgres", "mysql")
    if rounds:
        kinds = ("time", "timestamp", "timestamp_tz")
        assert [tuple(canon(value, kind) for value, kind in zip(row, kinds, strict=True)) for row in after] == (
            SEVENTH_ROUNDED)
    else:
        assert after == SEVENTH_KEPT
    # the preview says it for the columns that round, and for no other
    columns = copied.plan.tables[0].columns
    assert [FRACTION_NOTE in (column.conversion or "") for column in columns] == [False, rounds, rounds, rounds], [
        (column.source_name, column.conversion) for column in columns]


# SQL Server drops a backslash and the line break after it from a string literal (a line continuation): `quote_literal`
# (shared by ⑤'s edits, 06a's dump and the copy) ends the T-SQL literal after such a backslash (06c Task 5b). Here from
# SQLite's text into every target; tests/test_mssql_literal_breaks.py has the server side of it.
@pytest.mark.parametrize("target", ENGINES)
def test_a_backslash_before_a_line_break_is_kept(tmp_path: Path, target: str) -> None:
    ddl, rows = BACKSLASH_BREAKS
    with _copied(tmp_path, "sqlite", target, ddl, rows) as copied:
        before = copied.src.rows(f"SELECT tx FROM {copied.src.table(copied.name)} ORDER BY id")
        after = copied.dst.rows(f"SELECT tx FROM {copied.dst.table(copied.copy)} ORDER BY id")
    assert after == before
