import sqlite3
from uuid import uuid4

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.domain.profiles import SqliteProfile
from dbaguy_backend.sql.dialects import Dialect
from tests.support import PG_HOST, PG_PASSWORD, PG_PORT, make_pg_profile, requires_pg


def rel(snapshot, schema, name):
    s = next(s for s in snapshot.schemas if s.name == schema)
    return next(r for r in s.relations if r.name == name)


@requires_pg
def test_postgres_snapshot():
    conn = PostgresAdapter().open(make_pg_profile(), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)
    try:
        snap = conn.schema_snapshot()
    finally:
        conn.close()
    assert snap.database == "dbaguy"
    assert snap.default_schema == "public" and snap.search_path[0] == "public"
    customers = rel(snap, "public", "customers")
    assert customers.kind == "table" and customers.primary_key == ["id"]
    assert ("email", "email") in [(c.name, c.type_name) for c in customers.columns]
    orders = rel(snap, "sales", "orders")
    [fk] = orders.foreign_keys
    assert (fk.columns, fk.ref_schema, fk.ref_table, fk.ref_columns) == (["customer_id"], "public", "customers", ["id"])
    assert rel(snap, "sales", "order_summary").kind == "view"
    assert rel(snap, "sales", "daily_totals").kind == "materialized_view"
    assert rel(snap, "public", "Order Items").columns[1].name == "Select"
    assert "events_2026" not in [r.name for r in next(s for s in snap.schemas if s.name == "sales").relations]
    pg_catalog = next(s for s in snap.schemas if s.name == "pg_catalog")
    assert pg_catalog.system and pg_catalog.relations == [] and "count" in pg_catalog.functions  # spec §3.2
    assert "add_numbers" in next(s for s in snap.schemas if s.name == "public").functions


@requires_pg
def test_postgres_snapshot_of_another_database():
    conn = PostgresAdapter().open(make_pg_profile(), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, "analytics")
    try:
        snap = conn.schema_snapshot()
    finally:
        conn.close()
    assert snap.database == "analytics"
    assert [r.name for r in next(s for s in snap.schemas if s.name == "public").relations] == ["page_views"]


def test_sqlite_snapshot(tmp_path):
    db = tmp_path / "s.db"
    raw = sqlite3.connect(db)
    raw.executescript(
        "CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE books (id INTEGER, author_id INTEGER REFERENCES authors(id), title TEXT, PRIMARY KEY (id));"
        "CREATE VIEW titles AS SELECT title FROM books;"
        "CREATE TABLE seq_user (id INTEGER PRIMARY KEY AUTOINCREMENT);"  # creates sqlite_sequence
    )
    raw.close()
    profile = SqliteProfile(id=uuid4(), name="s", engine="sqlite", path=str(db))
    conn = SqliteAdapter().open(profile, None, None, None)
    try:
        snap = conn.schema_snapshot()
    finally:
        conn.close()
    assert snap.search_path == ["main"] and snap.default_schema == "main"
    main = snap.schemas[0]
    assert [r.name for r in main.relations] == ["authors", "books", "seq_user", "titles"]  # no sqlite_sequence
    books = rel(snap, "main", "books")
    assert books.primary_key == ["id"]
    assert [(f.columns, f.ref_schema, f.ref_table, f.ref_columns) for f in books.foreign_keys] == [
        (["author_id"], "main", "authors", ["id"])
    ]
    assert rel(snap, "main", "titles").kind == "view"


def test_relation_paths_and_dialects():
    pg, lite = PostgresAdapter(), SqliteAdapter()
    assert [(s.kind, s.name) for s in pg.relation_path("dbaguy", "sales", "orders", "table")] == [
        ("database", "dbaguy"), ("schema", "sales"), ("folder", "tables"), ("table", "orders"),
    ]
    assert [(s.kind, s.name) for s in lite.relation_path(None, "main", "v", "view")] == [
        ("schema", "main"), ("folder", "views"), ("view", "v"),
    ]
    assert pg.info().dialect == Dialect.POSTGRES and lite.info().dialect == Dialect.SQLITE


def test_sqlite_snapshot_resolves_foreign_keys_without_column_names(tmp_path):
    db = tmp_path / "f.db"
    raw = sqlite3.connect(db)
    raw.executescript(
        "CREATE TABLE Parent (a INTEGER, b INTEGER, PRIMARY KEY (a, b));"
        "CREATE TABLE child (x, y, FOREIGN KEY (x, y) REFERENCES parent);"  # implicit PK, different case
        "CREATE TABLE dangling (x REFERENCES missing);"
    )
    raw.close()
    profile = SqliteProfile(id=uuid4(), name="f", engine="sqlite", path=str(db))
    conn = SqliteAdapter().open(profile, None, None, None)
    try:
        snap = conn.schema_snapshot()
    finally:
        conn.close()
    [fk] = rel(snap, "main", "child").foreign_keys
    assert (fk.columns, fk.ref_table, fk.ref_columns) == (["x", "y"], "Parent", ["a", "b"])
    [dangling] = rel(snap, "main", "dangling").foreign_keys
    assert (dangling.ref_table, dangling.ref_columns) == ("missing", [""])


@requires_pg
def test_postgres_snapshot_has_no_phantom_foreign_keys_for_partitions():
    # PG clones an FK to a partitioned table onto every partition; partitions are not relations in the snapshot.
    conn = PostgresAdapter().open(make_pg_profile(), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)
    try:
        for sql in (
            "BEGIN",  # rolled back below: nothing is left in the shared dev database
            "CREATE SCHEMA snap_probe",
            "CREATE TABLE snap_probe.part (id int PRIMARY KEY) PARTITION BY RANGE (id)",
            "CREATE TABLE snap_probe.part_1 PARTITION OF snap_probe.part FOR VALUES FROM (0) TO (100)",
            "CREATE TABLE snap_probe.part_2 PARTITION OF snap_probe.part FOR VALUES FROM (100) TO (200)",
            "CREATE TABLE snap_probe.child2 (pid int REFERENCES snap_probe.part (id))",
        ):
            conn.run(sql).close()
        snap = conn.schema_snapshot()
    finally:
        conn.run("ROLLBACK").close()
        conn.close()
    assert [(f.ref_schema, f.ref_table, f.ref_columns) for f in rel(snap, "snap_probe", "child2").foreign_keys] == [
        ("snap_probe", "part", ["id"])
    ]
    probe = next(s for s in snap.schemas if s.name == "snap_probe")
    assert rel(snap, "snap_probe", "part").kind == "table"
    assert [r.name for r in probe.relations] == ["child2", "part"]  # no part_1 / part_2


def test_sqlite_snapshot_survives_a_virtual_table_with_a_missing_module(tmp_path):
    db = tmp_path / "g.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE normal (id INTEGER PRIMARY KEY, name TEXT)")
    # A virtual table whose module is not loaded cannot be created normally: write its definition directly.
    raw.execute("PRAGMA writable_schema = ON")
    raw.execute(
        "INSERT INTO sqlite_master (type, name, tbl_name, rootpage, sql) "
        "VALUES ('table', 'ghost', 'ghost', 0, 'CREATE VIRTUAL TABLE ghost USING nosuchmod(a)')"
    )
    raw.execute("PRAGMA writable_schema = OFF")
    raw.commit()
    raw.close()
    profile = SqliteProfile(id=uuid4(), name="g", engine="sqlite", path=str(db))
    conn = SqliteAdapter().open(profile, None, None, None)
    try:
        snap = conn.schema_snapshot()
    finally:
        conn.close()
    normal = rel(snap, "main", "normal")
    assert [c.name for c in normal.columns] == ["id", "name"] and normal.primary_key == ["id"]
    ghost = rel(snap, "main", "ghost")
    assert (ghost.columns, ghost.primary_key, ghost.foreign_keys) == ([], [], [])
