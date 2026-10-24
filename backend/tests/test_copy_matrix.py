"""The copy matrix (06c Task 5, spec ⑥ A9): a type-complete table of each relational engine copied into a new table
on each of them (16 copies, the same engine included), and every value read back with the target's own driver equal to
the source's by what it means (`copy_matrix.canon`) — a time zone's value by its instant, UTC where the target has no
zone. Then SQL Server's 7th fractional digit into each of the four. Scratch tables `zz_06c_*`, dropped by exact name."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from dbaguy_backend.domain.execution import CopyDataCommand
from dbaguy_backend.domain.transfer import TransferPlan
from dbaguy_backend.sql.type_map import FRACTION_NOTE
from tests.copy_matrix import READ_AS, SEVENTH, SEVENTH_KEPT, SEVENTH_ROUNDED, TYPES, canon
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
    src, dst = scratch_db(source, tmp_path / "s"), scratch_db(target, tmp_path / "t")
    try:
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
        src.close()
        dst.close()
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
        before = _read(copied.src, copied.name, columns)
        after = _read(copied.dst, copied.copy, columns)
    assert len(after) == len(before)
    for row_before, row_after in zip(before, after, strict=True):
        for column, was, now in zip(columns, row_before, row_after, strict=True):
            assert canon(now, families[column]) == canon(was, families[column]), (column, was, now)


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
