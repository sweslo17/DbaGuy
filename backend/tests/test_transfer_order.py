"""A copy into existing tables goes parents first by the target's own foreign keys (06c Task 2 fix round 4): replacing
rows deletes each table's rows first, and on PostgreSQL and SQL Server an ON DELETE CASCADE / SET NULL / SET DEFAULT
carries that DELETE into the tables that reference it. Pure rules, then the dev databases: scratch tables `zz_06c_*`,
dropped by exact name, children before their parents."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dbaguy_backend.adapters.references import Reference
from dbaguy_backend.domain.dialect import Dialect
from dbaguy_backend.domain.schema import SchemaSnapshot, SnapshotForeignKey, SnapshotRelation, SnapshotSchema
from dbaguy_backend.services.transfer_service import (
    CASCADE_LOOP, CASCADES_OUTSIDE, order_by_target, reference_warnings, snapshot_references,
)
from tests.data_support import DataApp, dropping
from tests.edit_support import scratch_db
from tests.support import requires_mssql, requires_mysql, requires_pg
from tests.test_transfer_job import copy
from tests.transfer_support import profile, table_name, tables_request


@pytest.fixture
def app(tmp_path: Path):
    app = DataApp(tmp_path)
    yield app
    app.close()


def key(child: str, parent: str, on_delete: str = "no_action", schema: str = "s") -> Reference:
    return Reference(schema, child, schema, parent, on_delete)  # type: ignore[arg-type]


def test_existing_tables_go_parents_first_by_the_targets_keys() -> None:
    names = ["child", "parent", "other"]
    assert order_by_target(names, "s", [key("child", "parent")], replace=False) == ([1, 0, 2], False, [])
    assert order_by_target(names, "s", [], replace=True) == ([0, 1, 2], False, [])  # no key: as chosen
    assert order_by_target(names, "s", [key("child", "parent", schema="t")], replace=False)[0] == [0, 1, 2]
    # a cycle of keys that only refuse: no order avoids a failure, which is loud; the cycle is said (FK_CYCLE)
    assert order_by_target(["a", "b"], "s", [key("a", "b"), key("b", "a")], replace=True) == ([0, 1], True, [])


def test_mysql_and_sqlite_targets_are_ordered_by_their_snapshots_keys() -> None:
    fk = SnapshotForeignKey(name="f", columns=["p"], ref_schema="shop", ref_table="parent", ref_columns=["id"])
    snapshot = SchemaSnapshot(database=None, default_schema="shop", search_path=[], built_at=datetime.now(UTC), schemas=[
        SnapshotSchema(name="shop", relations=[SnapshotRelation(name="parent", kind="table", columns=[]),
                                               SnapshotRelation(name="child", kind="table", columns=[],
                                                                foreign_keys=[fk])])])
    found = [Reference("shop", "child", "shop", "parent")]
    assert snapshot_references(snapshot, "shop", ["child"]) == snapshot_references(snapshot, "shop", ["parent"]) == found
    assert snapshot_references(snapshot, "other", ["child"]) == [] and snapshot_references(None, "shop", ["x"]) == []
    assert order_by_target(["child", "parent"], "shop", found, replace=False)[0] == [1, 0]


def test_a_cascade_is_obeyed_before_a_key_that_only_refuses() -> None:
    # a ↔ b, but only b's key into a cascades: a goes first, so a's DELETE reaches b's old rows only
    links = [key("a", "b"), key("b", "a", "cascade")]
    assert order_by_target(["b", "a"], "s", links, replace=True) == ([1, 0], True, [])


def test_a_loop_of_cascades_has_no_order() -> None:
    links = [key("a", "b", "cascade"), key("b", "a", "set_null")]
    assert order_by_target(["a", "b"], "s", links, replace=True) == ([0, 1], True, [("b", "a")])
    assert order_by_target(["a", "b"], "s", links, replace=False)[2] == []  # appending deletes nothing
    assert order_by_target(["t"], "s", [key("t", "t", "cascade")], replace=True) == ([0], False, [])  # its own DELETE
    assert CASCADE_LOOP.format(parent="s.b", child="s.a") == (
        "Deleting the rows of s.b also deletes or changes rows of s.a, and their tables' ON DELETE actions form a "
        "loop, so no order copies s.a after s.b: turn the cascade off, then copy them")


def test_a_replace_says_which_tables_outside_the_copy_its_delete_reaches() -> None:
    def said(mode: str, dialect: Dialect = Dialect.POSTGRES, *links: Reference) -> list[str]:
        return reference_warnings(dialect, mode, "s.p", "s", "p", {"p", "c"}, links,
                                  lambda schema, table: f"{schema}.{table}")

    outside = [key("x", "p", "cascade"), key("y", "p", "set_default"), key("c", "p", "cascade"),
               key("z", "p", "restrict"), Reference("other", "w", "s", "p", "set_null")]
    assert said("replace_rows", Dialect.POSTGRES, *outside) == [
        CASCADES_OUTSIDE.format("s.p", "s.x, s.y, other.w")]  # c is copied; z only refuses
    assert CASCADES_OUTSIDE.format("s.p", "s.x") == (
        "Deleting the rows of s.p also deletes (or changes) rows of s.x, which this copy does not write")
    assert said("append", Dialect.POSTGRES, *outside) == []  # nothing is deleted
    assert said("replace_rows", Dialect.MYSQL, *outside) == []  # its cascades are off during the copy
    assert said("append", Dialect.TSQL, key("p", "p")) == [
        "s.p references itself: the copy may fail unless its rows load in order"]  # fix M3, read live now


def _family(db, made: list, *, loop: bool = False) -> tuple[str, str]:
    """A parent and a child that references it ON DELETE CASCADE (with `loop`, the parent references the child the same
    way), each with two rows."""
    parent, child = table_name("p"), table_name("c")
    made += [("TABLE", parent), ("TABLE", child)]  # dropped newest first: the child, then the parent
    db.execute(f"CREATE TABLE {db.table(parent)} (id int PRIMARY KEY, c int)",
               f"CREATE TABLE {db.table(child)} (id int PRIMARY KEY, p int, FOREIGN KEY (p) REFERENCES "
               f"{db.table(parent)} (id) ON DELETE CASCADE)",  # table-level: MySQL ignores a column's REFERENCES
               f"INSERT INTO {db.table(parent)} (id) VALUES (1), (2)",
               f"INSERT INTO {db.table(child)} VALUES (10, 1), (11, 2)")
    if loop:
        db.execute(f"ALTER TABLE {db.table(parent)} ADD CONSTRAINT {parent}_c FOREIGN KEY (c) REFERENCES "
                   f"{db.table(child)} (id) ON DELETE SET NULL")
    return parent, child


def _sources(lite, parent: str, child: str) -> None:
    """The same tables in SQLite with no foreign key: the source's order says nothing."""
    lite.execute(f"CREATE TABLE {lite.table(parent)} (id INTEGER PRIMARY KEY, c INTEGER)",
                 f"CREATE TABLE {lite.table(child)} (id INTEGER PRIMARY KEY, p INTEGER)",
                 f"INSERT INTO {lite.table(parent)} (id) VALUES (1), (2), (3)",
                 f"INSERT INTO {lite.table(child)} VALUES (20, 1), (21, 3)")


@requires_pg
def test_replacing_a_parent_and_its_cascading_child_keeps_the_childs_new_rows(app: DataApp, tmp_path: Path) -> None:
    lite, pg = scratch_db("sqlite", tmp_path), scratch_db("postgres", tmp_path)
    try:
        with dropping(pg, before_drop=app.close_sessions) as made:
            parent, child = _family(pg, made)
            outside = table_name("x")
            made.append(("TABLE", outside))
            pg.execute(f"CREATE TABLE {pg.table(outside)} (id int, p int REFERENCES {pg.table(parent)} (id) "
                       "ON DELETE SET NULL)")
            _sources(lite, parent, child)
            src, dst = profile(app, lite, "lite"), profile(app, pg, "pg")
            request = tables_request(lite, src, [child, parent], pg, dst, "replace_rows")  # the child chosen first
            plan = app.container.transfers.plan(request)
            ran = copy(app, request, dst, "dbaguy")
            rows = pg.rows(f"SELECT id, p FROM {pg.table(child)} ORDER BY id")
        assert [table.target for table in plan.tables] == [f"public.{parent}", f"public.{child}"]
        assert CASCADES_OUTSIDE.format(f"public.{parent}", f"public.{outside}") in plan.tables[0].warnings
        assert ran.outcome.outcome == "completed" and rows == [(20, 1), (21, 3)]  # the parent's DELETE came first
    finally:
        lite.close()
        pg.close()


@requires_pg
def test_a_loop_of_cascades_is_a_problem(app: DataApp, tmp_path: Path) -> None:
    lite, pg = scratch_db("sqlite", tmp_path), scratch_db("postgres", tmp_path)
    try:
        with dropping(pg, before_drop=lambda: pg.execute(f"ALTER TABLE {pg.table(parent)} DROP CONSTRAINT "
                                                         f"{parent}_c")) as made:
            parent, child = _family(pg, made, loop=True)
            _sources(lite, parent, child)
            src, dst = profile(app, lite, "lite"), profile(app, pg, "pg")
            plan = app.container.transfers.plan(tables_request(lite, src, [parent, child], pg, dst, "replace_rows"))
            appended = app.container.transfers.plan(tables_request(lite, src, [parent, child], pg, dst, "append"))
        assert plan.plan_id is None and [p.code for p in plan.problems] == ["cascade_loop"]
        message = plan.problems[0].message
        assert f"public.{parent}" in message and f"public.{child}" in message and "turn the cascade off" in message
        assert appended.problems == []  # appending deletes nothing
    finally:
        lite.close()
        pg.close()


@requires_mssql
def test_sql_server_replaces_a_parent_before_its_cascading_child(app: DataApp, tmp_path: Path) -> None:
    lite, ms = scratch_db("sqlite", tmp_path), scratch_db("mssql", tmp_path)
    try:
        with dropping(ms, before_drop=app.close_sessions) as made:
            parent, child = _family(ms, made)
            _sources(lite, parent, child)
            src, dst = profile(app, lite, "lite"), profile(app, ms, "ms")
            request = tables_request(lite, src, [child, parent], ms, dst, "replace_rows")
            plan = app.container.transfers.plan(request)
            ran = copy(app, request, dst, "dbaguy")
            rows = ms.rows(f"SELECT id, p FROM {ms.table(child)} ORDER BY id")
        assert [table.target for table in plan.tables] == [f"dbo.{parent}", f"dbo.{child}"]
        assert ran.outcome.outcome == "completed" and rows == [(20, 1), (21, 3)]
    finally:
        lite.close()
        ms.close()


@requires_mysql
def test_mysql_cascades_nothing_with_its_checks_off(tmp_path: Path) -> None:
    """Why the rule leaves MySQL out: the copy's session turns FOREIGN_KEY_CHECKS off (TARGET_SETUP), and then a
    parent's DELETE neither cascades nor refuses (probed on the dev server)."""
    my = scratch_db("mysql", tmp_path)
    try:
        with dropping(my) as made:
            parent, child = _family(my, made)
            my.execute(f"DELETE FROM {my.table(parent)} WHERE id = 2")  # checks on: the key cascades
            cascaded = my.rows(f"SELECT id, p FROM {my.table(child)} ORDER BY id")
            my.execute("SET FOREIGN_KEY_CHECKS = 0", f"DELETE FROM {my.table(parent)}", "SET FOREIGN_KEY_CHECKS = 1")
            rows = my.rows(f"SELECT id, p FROM {my.table(child)} ORDER BY id")
        assert cascaded == [(10, 1)]
        assert rows == [(10, 1)]  # checks off: its row stays, referencing a parent that is gone
    finally:
        my.close()
