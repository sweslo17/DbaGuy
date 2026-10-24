"""Copy data's preview (spec ⑥ §3.5.1, 06c Task 2): mappings, statements, problems and the one-time plan, on the dev
databases. Scratch tables are `zz_06c_*`, dropped by exact name; nothing is copied here."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dbaguy_backend.adapters.columns import TableColumn
from dbaguy_backend.domain.dialect import Dialect
from dbaguy_backend.domain.edits import PLAN_EXPIRED, PLAN_MISMATCH, PlanUnavailable
from dbaguy_backend.domain.errors import NotSupported, PasswordRequired, ReadOnlyProfile
from dbaguy_backend.domain.schema import SchemaSnapshot, SnapshotForeignKey, SnapshotRelation, SnapshotSchema
from dbaguy_backend.domain.transfer import SourceSpec, TargetSpec, TransferRequest
from dbaguy_backend.services.transfer_service import (
    CREATED_STAYS, FK_NOT_ENFORCED, FK_UNCHECKED, KEY_NOT_CREATED, NON_TRANSACTIONAL, NON_TRANSACTIONAL_REPLACE,
    QUERY_RUNS_AGAIN, foreign_key_warnings, referenced_tables,
)
from dbaguy_backend.sql.type_map import (
    FRACTION_NOTE, MYSQL_JSON_NOTE, KeyPlan, conversion, planned_column, primary_key,
)
from tests.data_support import DataApp, dropping
from tests.edit_support import ENGINES, scratch_db
from tests.support import requires_mssql, requires_mysql, requires_pg
from tests.transfer_support import profile, query_request, table_name, tables_request

ORDERS = {
    "postgres": "CREATE TABLE {t} (id bigint PRIMARY KEY, placed timestamptz NOT NULL, amount numeric(12,2), "
                "note text, done boolean)",
    "mysql": "CREATE TABLE {t} (id bigint PRIMARY KEY, placed datetime(6) NOT NULL, amount decimal(12,2), "
             "note text, done tinyint(1))",
    "mssql": "CREATE TABLE {t} (id bigint PRIMARY KEY, placed datetimeoffset(7) NOT NULL, amount decimal(12,2), "
             "note nvarchar(max), done bit)",
    "sqlite": "CREATE TABLE {t} (id INTEGER PRIMARY KEY, placed DATETIME NOT NULL, amount DECIMAL(12,2), note TEXT, "
              "done BOOLEAN)",
}


@pytest.fixture
def app(tmp_path: Path):
    app = DataApp(tmp_path)
    yield app
    app.close()


@requires_pg
@requires_mysql
def test_a_table_maps_to_the_target_engines_types_with_its_key_and_notes(app: DataApp, tmp_path: Path) -> None:
    pg, my = scratch_db("postgres", tmp_path), scratch_db("mysql", tmp_path)
    try:
        with dropping(pg) as made:
            name = table_name()
            pg.execute(ORDERS["postgres"].format(t=pg.table(name)))
            made.append(("TABLE", name))
            plan = app.container.transfers.plan(tables_request(pg, profile(app, pg, "pg"), [name], my,
                                                               profile(app, my, "my")))
        assert plan.plan_id is not None and plan.problems == []
        [table] = plan.tables
        assert (table.source, table.target, table.mode) == (f"public.{name}", name, "create")
        assert [(c.source_name, c.target_type) for c in table.columns] == [
            ("id", "bigint"), ("placed", "datetime(6)"), ("amount", "decimal(12,2)"), ("note", "longtext"),
            ("done", "tinyint(1)")]
        placed = table.columns[1]
        assert placed.conversion == ("converted to UTC: the offset is not kept; PostgreSQL's infinity and dates "
                                     "before the year 1 fail their row")
        assert table.columns[4].conversion == "true and false as 1 and 0"
        assert table.before == [f"CREATE TABLE dbaguy.{name} (\n  id bigint NOT NULL,\n  placed datetime(6) NOT NULL,"
                                "\n  amount decimal(12,2),\n  note longtext,\n  done tinyint(1),\n  PRIMARY KEY (id)\n)"]
        assert CREATED_STAYS.format(name) in table.warnings
    finally:
        pg.close()
        my.close()


@pytest.mark.parametrize("engine", ENGINES)
def test_create_refuses_a_table_that_exists_and_append_one_that_does_not(app: DataApp, tmp_path: Path,
                                                                        engine: str) -> None:
    db = scratch_db(engine, tmp_path)
    try:
        with dropping(db) as made:
            source, there = table_name("s"), table_name("e")
            for name in (source, there):
                db.execute(ORDERS[engine].format(t=db.table(name)))
                made.append(("TABLE", name))
            me = profile(app, db, "db")
            exists = app.container.transfers.plan(tables_request(db, me, [source], db, me, "create", table=there))
            missing = app.container.transfers.plan(tables_request(db, me, [source], db, me, "append",
                                                                  table=table_name("m")))
            same = app.container.transfers.plan(tables_request(db, me, [source], db, me, "append"))
        assert exists.plan_id is None and [p.code for p in exists.tables[0].problems] == ["target_exists"]
        assert exists.tables[0].problems[0].message.endswith("already exists: append to it, or choose another name")
        assert [p.code for p in missing.tables[0].problems] == ["target_missing"]
        assert [p.code for p in same.tables[0].problems] == ["same_table"]
        if engine == "sqlite":  # one file: every copy within it is refused
            assert [p.code for p in exists.problems] == ["same_sqlite_file"]
    finally:
        db.close()


@requires_pg
@requires_mssql
def test_append_maps_columns_by_name_and_writes_what_identities_need(app: DataApp, tmp_path: Path) -> None:
    pg, ms = scratch_db("postgres", tmp_path), scratch_db("mssql", tmp_path)
    try:
        with dropping(pg) as pg_made, dropping(ms) as ms_made:
            source, into_pg, into_ms = table_name("s"), table_name("p"), table_name("q")
            pg.execute(f'CREATE TABLE {pg.table(source)} ("ID" int, label text, extra text, n serial, amount numeric)')
            pg.execute(f"CREATE TABLE {pg.table(into_pg)} (id int GENERATED ALWAYS AS IDENTITY, label text, "
                       "n int DEFAULT 1, amount numeric(12,2) DEFAULT 0)")
            ms.execute(f"CREATE TABLE {ms.table(into_ms)} (id int IDENTITY(1,1), label nvarchar(50), n int, "
                       "must int NOT NULL)")
            pg_made += [("TABLE", source), ("TABLE", into_pg)]
            ms_made.append(("TABLE", into_ms))
            me, other = profile(app, pg, "pg"), profile(app, ms, "ms")
            keep = app.container.transfers.plan(tables_request(pg, me, [source], pg, me, "append", table=into_pg))
            generate = app.container.transfers.plan(tables_request(
                pg, me, [source], ms, other, "replace_rows", table=into_ms,
                identity={f"public.{source}": {"ID": "generate"}}))
        [table] = keep.tables
        assert [(c.source_name, c.target_name, c.identity, c.skipped) for c in table.columns] == [
            ("ID", "id", "keep", None), ("label", "label", None, None), ("extra", None, None, "no such column in the "
                                                                                              "target"),
            ("n", "n", None, None), ("amount", "amount", None, None)]
        # id and n only: a numeric column owns no sequence, and its setval would not resolve (pre-flight H1)
        assert [line.splitlines()[0] for line in table.after] == ["SELECT setval(s.seq, GREATEST(s.top, "
                                                                  "COALESCE(pg_sequence_last_value(s.seq), s.top)))"] * 2
        assert keep.plan_id is not None
        [table] = generate.tables
        assert table.before == [f"DELETE FROM dbo.{into_ms}"]  # the identity is generated: no IDENTITY_INSERT
        assert [p.code for p in table.problems] == ["missing_column"]
        assert generate.plan_id is None
    finally:
        pg.close()
        ms.close()


@requires_mssql
@requires_pg
def test_a_type_without_a_counterpart_is_chosen_or_left_out(app: DataApp, tmp_path: Path) -> None:
    ms, pg = scratch_db("mssql", tmp_path), scratch_db("postgres", tmp_path)
    try:
        with dropping(ms) as made:
            name = table_name()
            ms.execute(f"CREATE TABLE {ms.table(name)} (id int PRIMARY KEY, g geography, v sql_variant)")
            made.append(("TABLE", name))
            source, target = profile(app, ms, "ms"), profile(app, pg, "pg")
            key = f"dbo.{name}"
            asked = app.container.transfers.plan(tables_request(ms, source, [name], pg, target))
            typed = app.container.transfers.plan(tables_request(ms, source, [name], pg, target,
                                                                overrides={key: {"g": "bytea"}}))
            bad = app.container.transfers.plan(tables_request(ms, source, [name], pg, target,
                                                              overrides={key: {"g": "bytea; DROP TABLE x"}}))
            left = app.container.transfers.plan(tables_request(ms, source, [name], pg, target,
                                                               left_out={key: ["g"]}))
        assert [p.code for p in asked.tables[0].problems] == ["unmappable_type"]
        # the source's order, with the column not copied in its place (Task 2 fix M4)
        assert [c.source_name for c in asked.tables[0].columns] == ["id", "g", "v"]
        assert asked.tables[0].columns[2].skipped == "not copied: sql_variant column: only its raw bytes can be read"
        assert typed.plan_id is not None and typed.tables[0].columns[1].overridden
        assert [p.message for p in bad.tables[0].problems] == ["g: Not a single type: it has a ;"]
        assert left.plan_id is not None and left.tables[0].columns[1].skipped == "left out"
    finally:
        ms.close()
        pg.close()


@requires_pg
def test_tables_go_parents_first_and_a_query_runs_again(app: DataApp, tmp_path: Path) -> None:
    pg, lite = scratch_db("postgres", tmp_path), scratch_db("sqlite", tmp_path)
    try:
        with dropping(pg) as made:
            parent, child = table_name("p"), table_name("c")
            pg.execute(f"CREATE TABLE {pg.table(parent)} (id int PRIMARY KEY)",
                       f"CREATE TABLE {pg.table(child)} (id int PRIMARY KEY, p int REFERENCES {pg.table(parent)})")
            made += [("TABLE", parent), ("TABLE", child)]
            me, target = profile(app, pg, "pg"), profile(app, lite, "lite")
            plan = app.container.transfers.plan(tables_request(pg, me, [child, parent], lite, target))
            query = app.container.transfers.plan(query_request(
                pg, me, "SELECT 1 AS a, now() AS b", [{"name": "a", "type_name": "int4", "category": "number"},
                                                     {"name": "b", "type_name": "timestamptz",
                                                      "category": "datetime"}], lite, target, "zz_06c_q"))
            write = app.container.transfers.plan(query_request(
                pg, me, f"DELETE FROM {pg.table(parent)} RETURNING id", [{"name": "id", "type_name": "int4",
                                                                          "category": "number"}],
                lite, target, "zz_06c_q"))
            twice = app.container.transfers.plan(query_request(
                pg, me, "SELECT 1 AS a, 2 AS a", [{"name": "a", "type_name": "int4", "category": "number"}] * 2,
                lite, target, "zz_06c_q"))
        assert [table.source for table in plan.tables] == [f"public.{parent}", f"public.{child}"]
        assert query.plan_id is not None and QUERY_RUNS_AGAIN in query.warnings
        assert [c.target_type for c in query.tables[0].columns] == ["INTEGER", "TEXT"]
        assert [p.code for p in write.problems] == ["query_not_select"]
        assert [p.code for p in twice.problems] == ["duplicate_column"]
    finally:
        pg.close()
        lite.close()


def test_a_read_only_target_and_an_engine_without_copies_are_refused_first(app: DataApp, tmp_path: Path) -> None:
    lite = scratch_db("sqlite", tmp_path)
    source = profile(app, lite, "source")
    read_only = app.add_profile(lite.profile(name="ro", read_only=True))
    bucket = app.add_profile({"profile": {"engine": "couchbase", "name": "cb", "host": "127.0.0.1"},
                              "secrets": {"password": "x"}})

    def request(target: object) -> TransferRequest:
        return TransferRequest(source=SourceSpec(profile_id=source, tables=[lite.path("t")]),
                               target=TargetSpec(profile_id=target, schema_name="main", mode="create"))

    with pytest.raises(ReadOnlyProfile):
        app.container.transfers.plan(request(read_only))
    with pytest.raises(NotSupported, match="Couchbase connections cannot copy data"):
        app.container.transfers.plan(request(bucket))
    lite.close()


def test_a_plan_is_taken_once_by_the_targets_session(app: DataApp, tmp_path: Path) -> None:
    (tmp_path / "other").mkdir()
    lite, other = scratch_db("sqlite", tmp_path), scratch_db("sqlite", tmp_path / "other")
    try:
        with dropping(lite) as made:
            name = table_name()
            lite.execute(f"CREATE TABLE {lite.table(name)} (id INTEGER PRIMARY KEY)")
            made.append(("TABLE", name))
            source, target = profile(app, lite, "a"), profile(app, other, "b")
            plan = app.container.transfers.plan(tables_request(lite, source, [name], other, target))
        store = app.container.transfers.plans
        with pytest.raises(PlanUnavailable) as wrong:
            store.take(plan.plan_id, source, None)
        assert wrong.value.code == PLAN_MISMATCH
        taken = store.take(plan.plan_id, target, None)
        assert (taken.source_profile_id, taken.query, len(taken.tables)) == (source, False, 1)
        assert taken.summary.startswith(f"-- Copy data: a main.{name} → b main.{name} (create)\nCREATE TABLE")
        with pytest.raises(PlanUnavailable) as again:
            store.take(plan.plan_id, target, None)
        assert (again.value.code, again.value.message) == (PLAN_EXPIRED, "The preview has expired: preview the copy "
                                                                          "again")  # pre-flight L-a
    finally:
        lite.close()
        other.close()


@requires_pg
def test_a_same_engine_copy_keeps_declared_types_but_the_databases_own(app: DataApp, tmp_path: Path) -> None:
    """Task 1's `TableColumn.custom_type`, read from PostgreSQL's catalog: a domain and an enum are the source
    database's own, so a new table gets their family's type (the target database may lack them); a built-in type
    stays as declared."""
    pg = scratch_db("postgres", tmp_path)
    try:
        with dropping(pg) as made:
            name, money, mood = table_name(), table_name("d"), table_name("e")
            pg.execute(f"CREATE DOMAIN {pg.table(money)} AS numeric(10,2)",
                       f"CREATE TYPE {pg.table(mood)} AS ENUM ('ok', 'sad')")
            made += [("DOMAIN", money), ("TYPE", mood)]
            pg.execute(f"CREATE TABLE {pg.table(name)} (id integer PRIMARY KEY, price {pg.table(money)}, "
                       f"feel {pg.table(mood)}, at timestamptz)")
            made.append(("TABLE", name))
            me = profile(app, pg, "pg")
            read = app.container.metadata.with_connection(me, pg.database, lambda c: c.table_columns(pg.path(name)))
            plan = app.container.transfers.plan(tables_request(pg, me, [name], pg, me, table=table_name("n")))
        assert [(c.name, c.family, c.custom_type) for c in read] == [
            ("id", "int32", False), ("price", "decimal", True), ("feel", "enum", True), ("at", "timestamp_tz", False)]
        assert plan.plan_id is not None
        [table] = plan.tables
        assert [(c.source_name, c.target_type) for c in table.columns] == [
            ("id", "integer"), ("price", "numeric(10,2)"), ("feel", "text"), ("at", "timestamp with time zone")]
        assert table.columns[1].conversion.endswith("is the source database's own type: the new column is "
                                                    "numeric(10,2)")
    finally:
        pg.close()


@requires_pg
def test_a_target_connection_without_a_database_copies_into_its_own(app: DataApp, tmp_path: Path) -> None:  # M2
    pg = scratch_db("postgres", tmp_path)
    try:
        with dropping(pg) as made:
            name = table_name()
            pg.execute(f"CREATE TABLE {pg.table(name)} (id int PRIMARY KEY)")
            made.append(("TABLE", name))
            me = profile(app, pg, "pg")
            request = tables_request(pg, me, [name], pg, me, "append")
            unnamed = request.model_copy(update={"target": request.target.model_copy(update={"database": None})})
            same = app.container.transfers.plan(unnamed)  # the profile's own database, dbaguy
        assert [p.code for p in same.tables[0].problems] == ["same_table"]
    finally:
        pg.close()


@requires_pg
def test_a_missing_password_says_whose_it_is(app: DataApp, tmp_path: Path) -> None:  # M3: the preview reads two profiles
    lite, pg = scratch_db("sqlite", tmp_path), scratch_db("postgres", tmp_path)
    body = pg.profile(name="no password")
    body.pop("secrets", None)
    body["profile"]["save_password"] = False
    target = app.add_profile(body)
    with pytest.raises(PasswordRequired) as asked:
        app.container.transfers.plan(tables_request(lite, profile(app, lite, "lite"), ["t"], pg, target))
    assert asked.value.detail["profile_id"] == str(target)
    lite.close()
    pg.close()


def test_mysql_and_sqlite_targets_say_their_foreign_keys_go_unchecked() -> None:  # 06c pre-flight L2
    fk = SnapshotForeignKey(name="f", columns=["p"], ref_schema="shop", ref_table="parent", ref_columns=["id"])
    me = SnapshotForeignKey(name="m", columns=["up"], ref_schema="shop", ref_table="tree", ref_columns=["id"])
    snapshot = SchemaSnapshot(database=None, default_schema="shop", search_path=[], built_at=datetime.now(UTC), schemas=[
        SnapshotSchema(name="shop", relations=[
            SnapshotRelation(name="parent", kind="table", columns=[]),
            SnapshotRelation(name="child", kind="table", columns=[], foreign_keys=[fk]),
            SnapshotRelation(name="tree", kind="table", columns=[], foreign_keys=[me]),
        ])])
    my, lite = Dialect.MYSQL, Dialect.SQLITE
    assert referenced_tables(snapshot) == frozenset({("shop", "parent")})  # tree's reference to itself is not another's

    def said(dialect: Dialect, mode: str, shown: str, name: str, keys: SchemaSnapshot | None = snapshot) -> list[str]:
        return foreign_key_warnings(dialect, mode, shown, keys, referenced_tables(keys), "shop", name)

    assert said(my, "append", "child", "child") == [FK_UNCHECKED.format("child", "MySQL")]
    assert said(my, "append", "parent", "parent") == []
    assert said(my, "replace_rows", "parent", "parent") == [FK_NOT_ENFORCED.format("parent", "MySQL")]
    assert said(lite, "replace_rows", "main.tree", "tree") == [FK_UNCHECKED.format("main.tree", "SQLite")]
    assert said(my, "create", "child", "child") == []
    assert said(Dialect.POSTGRES, "append", "child", "child") == []  # PostgreSQL checks them row by row
    # fix M3: there a table that references itself loads in the source's order, each row checked as it goes
    itself = "shop.tree references itself: the copy may fail unless its rows load in order"
    assert said(Dialect.POSTGRES, "append", "shop.tree", "tree") == [itself]
    assert said(Dialect.TSQL, "replace_rows", "shop.tree", "tree") == [itself]
    assert said(Dialect.POSTGRES, "create", "shop.tree", "tree") == []
    assert said(my, "append", "child", "child", keys=None) == []  # no snapshot: no warning
    assert said(my, "append", "gone", "gone") == []


@requires_mysql
def test_appending_into_a_mysql_table_with_foreign_keys_says_they_go_unchecked(app: DataApp, tmp_path: Path) -> None:
    """L2 live: the target's snapshot names the child's foreign key (append) and the parent's referrers (replace)."""
    lite, my = scratch_db("sqlite", tmp_path), scratch_db("mysql", tmp_path)
    try:
        with dropping(my, before_drop=app.close_sessions) as made:
            source, parent, child = table_name("s"), table_name("p"), table_name("c")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY, p INTEGER)")
            my.execute(f"CREATE TABLE {my.table(parent)} (id int PRIMARY KEY)",
                       f"CREATE TABLE {my.table(child)} (id int PRIMARY KEY, p int, FOREIGN KEY (p) REFERENCES "
                       f"{my.table(parent)} (id))")
            made += [("TABLE", parent), ("TABLE", child)]
            src, dst = profile(app, lite, "lite"), profile(app, my, "my")
            plan = app.container.transfers.plan(tables_request(lite, src, [source], my, dst, "append", table=child))
            replace = app.container.transfers.plan(tables_request(lite, src, [source], my, dst, "replace_rows",
                                                                  table=parent))
        assert plan.plan_id is not None and FK_UNCHECKED.format(child, "MySQL") in plan.tables[0].warnings
        assert replace.plan_id is not None
        assert FK_NOT_ENFORCED.format(parent, "MySQL") in replace.tables[0].warnings
        assert FK_UNCHECKED.format(parent, "MySQL") not in replace.tables[0].warnings  # the parent has none of its own
    finally:
        lite.close()
        my.close()


@requires_mysql
def test_a_table_that_cannot_roll_back_is_warned_of_by_mode(app: DataApp, tmp_path: Path) -> None:  # pre-flight H3
    lite, my = scratch_db("sqlite", tmp_path), scratch_db("mysql", tmp_path)
    try:
        with dropping(my, before_drop=app.close_sessions) as made:
            source, target = table_name("s"), table_name("m")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY)")
            my.execute(f"CREATE TABLE {my.table(target)} (id int PRIMARY KEY) ENGINE=MyISAM")
            made.append(("TABLE", target))
            src, dst = profile(app, lite, "lite"), profile(app, my, "my")
            append, replace = (app.container.transfers.plan(tables_request(lite, src, [source], my, dst, mode,
                                                                           table=target))
                               for mode in ("append", "replace_rows"))
        assert append.tables[0].warnings == [NON_TRANSACTIONAL.format(target)]
        assert replace.tables[0].warnings == [NON_TRANSACTIONAL_REPLACE.format(target)]  # its DELETE stays at once
        assert replace.tables[0].before == [f"DELETE FROM dbaguy.{target}"]
    finally:
        lite.close()
        my.close()


@requires_pg
@pytest.mark.parametrize(("source_database", "target_database"), [
    ("dbaguy", "dbaguy"), (None, "dbaguy"), (None, None), ("dbaguy", None)])
def test_a_table_is_never_copied_into_itself_whichever_database_is_left_out(
        app: DataApp, tmp_path: Path, source_database: str | None, target_database: str | None) -> None:  # fix I1
    pg = scratch_db("postgres", tmp_path)
    try:
        with dropping(pg) as made:
            name = table_name()
            pg.execute(f"CREATE TABLE {pg.table(name)} (id int PRIMARY KEY)")
            made.append(("TABLE", name))
            me = profile(app, pg, "pg")
            request = tables_request(pg, me, [name], pg, me, "append")
            request = request.model_copy(update={
                "source": request.source.model_copy(update={"database": source_database}),
                "target": request.target.model_copy(update={"database": target_database})})
            same = app.container.transfers.plan(request)  # either one left out is the profile's own, dbaguy
        assert same.plan_id is None and [p.code for p in same.tables[0].problems] == ["same_table"]
    finally:
        pg.close()


def _source_column(name: str, family: str, type_name: str = "text") -> TableColumn:
    return TableColumn(name=name, type_name=type_name, family=family, nullable=False, has_default=False, kind="text",
                       compare="none", writable=True)


def test_a_new_tables_key_columns_get_types_the_target_can_key() -> None:  # fix I2 and fix round 2, pure
    code, doc, blob = _source_column("code", "text"), _source_column("doc", "json"), _source_column("b", "binary")
    a, b, n = _source_column("a", "text"), _source_column("b", "text"), _source_column("n", "int64")
    my, ms = Dialect.MYSQL, Dialect.TSQL

    def bounded(name: str, type_sql: str, dialect: Dialect) -> str:
        limit = "MySQL keys at most 3,072" if dialect is my else "SQL Server keys at most 900"
        return f"Key column {name} becomes {type_sql}: {limit} bytes; longer values fail their row"  # N4

    assert primary_key([(code, "longtext", False)], my) == KeyPlan(("varchar(768)",),
                                                                   (bounded("code", "varchar(768)", my),))
    assert primary_key([(code, "varchar(100)", False)], my).notes == (None,)  # its declared length fits: kept
    assert primary_key([(code, "varchar(1000)", False)], my).types == ("varchar(768)",)  # 4,000 bytes do not
    assert primary_key([(code, "nvarchar(max)", False)], ms) == KeyPlan(("nvarchar(450)",),
                                                                        (bounded("code", "nvarchar(450)", ms),))
    assert primary_key([(_source_column("c", "varchar"), "varchar(max)", False)], ms).types == ("varchar(900)",)
    assert primary_key([(blob, "longblob", False)], my).types == ("varbinary(3072)",)
    assert primary_key([(blob, "varbinary(max)", False)], ms).types == ("varbinary(900)",)
    assert primary_key([(doc, "json", False)], my).reason == "doc would be json, which MySQL can't key"
    assert primary_key([(code, "longtext", True)], my).reason == "code would be longtext, which MySQL can't key"
    assert primary_key([(code, "text", False)], Dialect.POSTGRES).types == ("text",)  # PostgreSQL keys it as it is
    # N3: MySQL's text key columns share what the other key columns leave of its 3,072 bytes
    assert primary_key([(code, "longtext", False), (n, "bigint", False)], my).types == ("varchar(766)", "bigint")
    assert primary_key([(a, "longtext", False), (b, "longtext", False), (_source_column("i", "int32"), "int", False)],
                       my).types == ("varchar(383)", "varchar(383)", "int")
    assert primary_key([(a, "varchar(100)", False), (b, "longtext", False)], my) == KeyPlan(
        ("varchar(100)", "varchar(384)"), (None, bounded("b", "varchar(384)", my)))
    assert primary_key([(a, "varchar(1000)", True), (n, "bigint", False)], my).reason == (
        "its columns would take more than the 3,072 bytes MySQL keys")  # the user's type: never shortened
    # N2: SQL Server creates a key declared wider than 900 bytes; only a longer row fails
    wide = [(_source_column(name, "varchar"), "nvarchar(400)", False) for name in "abc"]
    assert primary_key(wide, ms) == KeyPlan(("nvarchar(400)",) * 3, (None,) * 3)
    assert primary_key([(a, "nvarchar(max)", False), (b, "nvarchar(max)", False)], ms).types == (
        "nvarchar(450)", "nvarchar(450)")
    # N4: a key whose type does not change says nothing
    assert primary_key([(_source_column("v", "varchar"), "varchar(255)", False)], my).notes == (None,)
    assert primary_key([(_source_column("v", "varchar"), "nvarchar(510)", False)], ms).notes == (None,)
    # N1: a type no rule reads keeps its key, with no note: the server decides at CREATE
    for type_sql, dialect, chosen in (("nvarchar(100) COLLATE Latin1_General_BIN", ms, True),
                                      ("varchar(255) CHARACTER SET ascii", my, True), ("[nvarchar](100)", ms, True),
                                      ("int(10) unsigned zerofill", my, False)):
        assert primary_key([(code, type_sql, chosen)], dialect) == KeyPlan((type_sql,), (None,)), type_sql


def _key_tables(pg, made: list) -> dict[str, str]:
    """PostgreSQL tables with the key shapes of fix I2 and fix round 2, by shape."""
    shapes = {
        "text": "(code text PRIMARY KEY, v int)",
        "pair": "(a text, b text, PRIMARY KEY (a, b))",
        "json": "(doc jsonb PRIMARY KEY)",
        "tenant": "(tenant text, id bigint, PRIMARY KEY (tenant, id))",
        "triple": "(a text, b text, n int, PRIMARY KEY (a, b, n))",
        "wide": "(a varchar(200), b varchar(200), c varchar(200), PRIMARY KEY (a, b, c))",
    }
    names = {shape: table_name(shape[0]) for shape in shapes}
    for shape, columns in shapes.items():
        made.append(("TABLE", names[shape]))
        pg.execute(f"CREATE TABLE {pg.table(names[shape])} {columns}")
    return names


def _keys_planned(plan) -> dict[str, tuple[list[str | None], list[str]]]:
    """Each previewed table's target types and table warnings, by source name."""
    return {table.source: ([c.target_type for c in table.columns], table.warnings) for table in plan.tables}


@requires_pg
@requires_mysql
def test_a_text_key_into_mysql_is_bounded_and_a_key_it_cannot_make_is_left_out(app: DataApp, tmp_path: Path) -> None:
    pg, my = scratch_db("postgres", tmp_path), scratch_db("mysql", tmp_path)  # fix I2 and round 2, CREATE run live
    try:
        with dropping(pg) as pg_made, dropping(my, before_drop=app.close_sessions) as my_made:
            names = _key_tables(pg, pg_made)
            src, dst = profile(app, pg, "pg"), profile(app, my, "my")
            plan = app.container.transfers.plan(tables_request(pg, src, list(names.values()), my, dst))
            chosen = {f"public.{names['text']}": {"code": "longtext"}}
            typed = app.container.transfers.plan(tables_request(pg, src, [names["text"]], my, dst, overrides=chosen))
            assert plan.plan_id is not None
            for table in plan.tables:  # each CREATE TABLE the preview shows is one MySQL takes
                my_made.append(("TABLE", table.target))
                my.execute(table.before[0])
        by = {shape: _keys_planned(plan)[f"public.{name}"] for shape, name in names.items()}
        made = {table.source: table.before[0] for table in plan.tables}
        coded = next(table for table in plan.tables if table.source == f"public.{names['text']}")
        assert (coded.columns[0].target_type, coded.columns[0].conversion) == (
            "varchar(768)", "Key column code becomes varchar(768): MySQL keys at most 3,072 bytes; longer values fail "
                            "their row")
        assert by["pair"][0] == ["varchar(384)", "varchar(384)"]  # N3: the two share the key's bytes
        assert by["tenant"][0] == ["varchar(766)", "bigint"]
        assert by["triple"][0] == ["varchar(383)", "varchar(383)", "int"]
        assert by["wide"][0] == ["varchar(200)"] * 3  # 2,400 bytes fit: kept
        for shape in ("text", "pair", "tenant", "triple", "wide"):
            assert "PRIMARY KEY (" in made[f"public.{names[shape]}"] and not any(
                "primary key is not created" in said for said in by[shape][1]), shape
        assert KEY_NOT_CREATED.format("doc would be json, which MySQL can't key") in by["json"][1]
        assert "PRIMARY KEY" not in made[f"public.{names['json']}"]
        assert KEY_NOT_CREATED.format("code would be longtext, which MySQL can't key") in typed.tables[0].warnings
        assert typed.tables[0].columns[0].target_type == "longtext"  # the user's type wins
    finally:
        pg.close()
        my.close()


@requires_pg
@requires_mssql
def test_a_text_key_into_sql_server_is_bounded_and_a_key_it_cannot_make_is_left_out(app: DataApp,
                                                                                    tmp_path: Path) -> None:
    pg, ms = scratch_db("postgres", tmp_path), scratch_db("mssql", tmp_path)  # fix I2 and round 2, CREATE run live
    try:
        with dropping(pg) as pg_made, dropping(ms, before_drop=app.close_sessions) as ms_made:
            names = _key_tables(pg, pg_made)
            src, dst = profile(app, pg, "pg"), profile(app, ms, "ms")
            plan = app.container.transfers.plan(tables_request(pg, src, list(names.values()), ms, dst))
            assert plan.plan_id is not None
            for table in plan.tables:  # a key declared wider than 900 bytes too: SQL Server warns and creates it
                ms_made.append(("TABLE", table.source.removeprefix("public.")))
                ms.execute(table.before[0])
        by = {shape: _keys_planned(plan)[f"public.{name}"] for shape, name in names.items()}
        made = {table.source: table.before[0] for table in plan.tables}
        coded = next(table for table in plan.tables if table.source == f"public.{names['text']}")
        assert (coded.columns[0].target_type, coded.columns[0].conversion) == (
            "nvarchar(450)", "Key column code becomes nvarchar(450): SQL Server keys at most 900 bytes; longer values "
                             "fail their row")
        assert by["pair"][0] == ["nvarchar(450)", "nvarchar(450)"]  # N2: 1,800 bytes declared, the key is kept
        assert by["tenant"][0] == ["nvarchar(450)", "bigint"]
        assert by["triple"][0] == ["nvarchar(450)", "nvarchar(450)", "int"]
        assert by["wide"][0] == ["nvarchar(400)"] * 3  # not a max type: as mapped, no key note
        for shape in ("text", "pair", "tenant", "triple", "wide"):
            assert "PRIMARY KEY (" in made[f"public.{names[shape]}"] and not any(
                "primary key is not created" in said for said in by[shape][1]), shape
        assert KEY_NOT_CREATED.format("doc would be nvarchar(max), which SQL Server can't key") in by["json"][1]
    finally:
        pg.close()
        ms.close()


@requires_pg
def test_a_copys_target_is_read_without_planning_a_browse(app: DataApp, tmp_path: Path, monkeypatch) -> None:  # M1
    pg = scratch_db("postgres", tmp_path)
    lite = scratch_db("sqlite", tmp_path)
    try:
        with dropping(pg) as made:
            name, source = table_name(), table_name("s")
            pg.execute(f"CREATE TABLE {pg.table(name)} (id int PRIMARY KEY)")
            made.append(("TABLE", name))
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY)")
            me, src = profile(app, pg, "pg"), profile(app, lite, "lite")
            browse, copy = app.container.metadata.with_connection(me, pg.database, lambda c: (
                c.edit_target(pg.path(name)), c.edit_target(pg.path(name), order=False)))
            read: list = []
            real = app.container.schema_cache.get
            monkeypatch.setattr(app.container.schema_cache, "get",
                                lambda profile_id, database=None: read.append(profile_id) or real(profile_id, database))
            app.container.transfers.plan(tables_request(lite, src, [source], pg, me, "append", table=name))
            appended = list(read)
            app.container.transfers.plan(tables_request(lite, src, [source], pg, me, "create", table=table_name("n")))
        assert (browse.stable_order, copy.stable_order, copy.editable) == (["id"], None, True)  # ⑤'s is unchanged
        assert appended == [me]  # one table appended: the target's foreign keys only, never the source's keys
        assert read[len(appended):] == [src]  # create: the source's primary key
    finally:
        pg.close()
        lite.close()


def test_two_sources_into_one_target_table_are_refused(app: DataApp, tmp_path: Path) -> None:  # fix M2
    (tmp_path / "other").mkdir()
    lite, other = scratch_db("sqlite", tmp_path), scratch_db("sqlite", tmp_path / "other")
    try:
        name = table_name()
        lite.execute(f"CREATE TABLE {lite.table(name)} (id INTEGER PRIMARY KEY)")
        source, target = profile(app, lite, "a"), profile(app, other, "b")
        plan = app.container.transfers.plan(tables_request(lite, source, [name, name], other, target))
        assert plan.plan_id is None
        assert [[p.code for p in table.problems] for table in plan.tables] == [[], ["duplicate_target"]]
        assert plan.tables[1].problems[0].message == f"main.{name} goes into main.{name} too: copy them one at a time"
    finally:
        lite.close()
        other.close()


@requires_pg
def test_column_names_that_differ_only_in_case_are_one_column_where_case_is_folded(app: DataApp,
                                                                                    tmp_path: Path) -> None:  # M2
    pg, lite = scratch_db("postgres", tmp_path), scratch_db("sqlite", tmp_path)
    try:
        with dropping(pg) as made:
            name, into = table_name("s"), table_name("t")
            pg.execute(f'CREATE TABLE {pg.table(name)} ("ID" int, id int)')
            made.append(("TABLE", name))
            lite.execute(f"CREATE TABLE {lite.table(into)} (id INTEGER)")
            src, dst = profile(app, pg, "pg"), profile(app, lite, "lite")
            create = app.container.transfers.plan(tables_request(pg, src, [name], lite, dst))
            append = app.container.transfers.plan(tables_request(pg, src, [name], lite, dst, "append", table=into))
            kept = app.container.transfers.plan(tables_request(pg, src, [name], pg, src, table=table_name("n")))
        assert [(p.code, p.message) for p in create.tables[0].problems] == [
            ("duplicate_column", "ID and id would be one column in SQLite")]
        assert [(p.code, p.message) for p in append.tables[0].problems] == [
            ("duplicate_column", f"ID and id would both go into {into}.id")]
        assert kept.plan_id is not None  # PostgreSQL keeps both: quoted names are not folded
    finally:
        pg.close()
        lite.close()


@requires_pg
def test_appending_into_a_table_that_references_itself_is_warned_of(app: DataApp, tmp_path: Path) -> None:  # M3
    lite, pg = scratch_db("sqlite", tmp_path), scratch_db("postgres", tmp_path)
    try:
        with dropping(pg) as made:
            source, tree = table_name("s"), table_name("t")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY, up INTEGER)")
            pg.execute(f"CREATE TABLE {pg.table(tree)} (id int PRIMARY KEY, up int REFERENCES {pg.table(tree)})")
            made.append(("TABLE", tree))
            src, dst = profile(app, lite, "lite"), profile(app, pg, "pg")
            plan = app.container.transfers.plan(tables_request(lite, src, [source], pg, dst, "append", table=tree))
        assert plan.tables[0].warnings == [
            f"public.{tree} references itself: the copy may fail unless its rows load in order"]
    finally:
        lite.close()
        pg.close()


def test_a_json_column_the_server_normalises_is_said_created_or_not() -> None:  # fix round 1, Task 1's I2
    text = TableColumn(name="doc", type_name="text", family="text", nullable=True, has_default=False, kind="text",
                       compare="none", writable=True)
    into_mysql, into_jsonb = planned_column("doc", "json", Dialect.MYSQL), planned_column("doc", "JSONB",
                                                                                          Dialect.POSTGRES)
    assert MYSQL_JSON_NOTE in conversion(text, into_mysql, Dialect.POSTGRES, Dialect.MYSQL).notes
    assert ("jsonb orders the keys and keeps the last of duplicate keys"
            in conversion(text, into_jsonb, Dialect.TSQL, Dialect.POSTGRES).notes)
    assert conversion(text, into_mysql, Dialect.MYSQL, Dialect.MYSQL).notes == ()  # the same engine's own text
    kept = conversion(text, planned_column("doc", "json", Dialect.POSTGRES), Dialect.MYSQL, Dialect.POSTGRES).notes
    assert not any("orders the keys" in note for note in kept)  # PostgreSQL's json keeps the text as it is
    assert MYSQL_JSON_NOTE == ("MySQL's JSON orders the keys, keeps one of duplicate keys, and rewrites decimals "
                               "(1.10 becomes 1.1) and integers beyond 64 bits as doubles: choose longtext to keep "
                               "the text")  # Task 1 re-review N2: integers up to uint64 stay exact


def test_only_a_time_or_a_date_and_time_target_rounds_a_seventh_digit() -> None:  # Task 1 re-review N1
    seven = TableColumn(name="at", type_name="datetime2(7)", family="timestamp", nullable=True, has_default=False,
                        kind="datetime", compare="none", writable=True, scale=7)
    clock = TableColumn(name="at", type_name="time(7)", family="time", nullable=True, has_default=False,
                        kind="datetime", compare="none", writable=True, scale=7)

    def rounded(source: TableColumn, dialect: Dialect, type_sql: str) -> bool:
        return FRACTION_NOTE in conversion(source, planned_column("at", type_sql, dialect), Dialect.TSQL,
                                           dialect).notes

    assert rounded(seven, Dialect.MYSQL, "datetime(6)") and rounded(seven, Dialect.POSTGRES, "timestamp")
    assert rounded(clock, Dialect.MYSQL, "time(6)")  # MySQL's time is the duration family: its kind says datetime
    assert not any(rounded(seven, dialect, type_sql) for dialect, type_sql in (
        (Dialect.MYSQL, "longtext"), (Dialect.MYSQL, "varchar(40)"), (Dialect.POSTGRES, "text"),
        (Dialect.MYSQL, "date"), (Dialect.POSTGRES, "date")))  # text keeps all 7 digits; a date has no fraction
