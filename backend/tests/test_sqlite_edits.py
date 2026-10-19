"""SQLite grid editing (spec ⑤): the edit target from the schema (affinity, keys, views, virtual tables)."""

import pytest

from dbaguy_backend.adapters.editing import NO_KEY, VIEWS
from tests.edit_support import SqliteScratch, scratch


@pytest.fixture
def lite(tmp_path):
    db = SqliteScratch(tmp_path)
    connection = db.open()
    try:
        yield db, connection
    finally:
        connection.close()
        db.close()


def test_columns_take_their_kind_from_the_declared_type(lite):  # ruling E6
    db, connection = lite
    with scratch(db) as make:
        table = make("TABLE", "CREATE TABLE {t} (id INTEGER PRIMARY KEY, name VARCHAR(20) NOT NULL, body TEXT, data BLOB, "
                              "anything, price REAL, amount DECIMAL(10,2), made DATETIME DEFAULT CURRENT_TIMESTAMP, "
                              "ok BOOLEAN, doc JSON, total INT GENERATED ALWAYS AS (id * 2) VIRTUAL)")
        target = connection.edit_target(db.path(table))
    assert (target.editable, target.key, target.key_source) == (True, ["id"], "primary_key")
    assert {c.name: (c.kind, c.compare, c.writable, c.has_default) for c in target.columns} == {
        "id": ("number", "equals", True, True), "name": ("text", "equals", True, False),
        "body": ("text", "equals", True, False), "data": ("binary", "equals", True, False),
        "anything": ("other", "equals", True, False), "price": ("number", "none", True, False),
        "amount": ("number", "equals", True, False), "made": ("datetime", "equals", True, True),
        "ok": ("bool", "equals", True, False), "doc": ("json", "equals", True, False),
        "total": ("number", "equals", False, True),
    }
    assert [c.name for c in target.columns if not c.nullable] == ["id", "name"]


def test_keys_views_and_virtual_tables(lite):
    db, connection = lite
    with scratch(db) as make:
        no_rowid = make("TABLE", "CREATE TABLE {t} (code TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID")
        unique = make("TABLE", "CREATE TABLE {t} (a TEXT NOT NULL, b TEXT NOT NULL, c TEXT, UNIQUE (a, b))")
        partial = make("TABLE", "CREATE TABLE {t} (a TEXT NOT NULL, c TEXT)")
        db.execute(f'CREATE UNIQUE INDEX "{partial}_a" ON {db.q(partial)} (a) WHERE a > \'\'')
        expression = make("TABLE", "CREATE TABLE {t} (a TEXT NOT NULL)")
        db.execute(f'CREATE UNIQUE INDEX "{expression}_x" ON {db.q(expression)} (lower(a))')
        nullable_pk = make("TABLE", "CREATE TABLE {t} (code TEXT PRIMARY KEY, v TEXT)")
        real_pk = make("TABLE", "CREATE TABLE {t} (r REAL PRIMARY KEY)")
        loose = make("TABLE", "CREATE TABLE {t} (a, b)")
        view = make("VIEW", f"CREATE VIEW {{t}} AS SELECT * FROM {db.table(loose)}")
        fts = make("TABLE", "CREATE VIRTUAL TABLE {t} USING fts5(body)")
        found = {name: connection.edit_target(db.path(name))
                 for name in (no_rowid, unique, partial, expression, nullable_pk, real_pk, loose, fts)}
        found[view] = connection.edit_target(db.path(view, "view"))
    assert found[no_rowid].key == ["code"]
    assert (found[unique].key, found[unique].key_source) == (["a", "b"], "unique")
    assert found[partial].reason == NO_KEY and found[expression].reason == NO_KEY and found[loose].reason == NO_KEY
    assert found[nullable_pk].key == ["code"]  # SQLite lets this key hold NULL: such a row is key_incomplete
    assert found[real_pk].reason == "The key column r (REAL) cannot be matched exactly: a row cannot be identified"
    assert (found[view].reason, found[fts].reason) == (VIEWS, "Virtual tables are read-only here")


def test_only_a_rowid_alias_is_never_null_and_assigned_when_left_out(lite):
    db, connection = lite
    with scratch(db) as make:
        alias = make("TABLE", "CREATE TABLE {t} (id INTEGER PRIMARY KEY, v TEXT)")
        no_rowid = make("TABLE", "CREATE TABLE {t} (id INTEGER PRIMARY KEY, v TEXT) WITHOUT ROWID")  # NOT NULL, no value
        descending = make("TABLE", "CREATE TABLE {t} (id INTEGER PRIMARY KEY DESC, v TEXT)")  # not the rowid: may be NULL
        found = {name: connection.edit_target(db.path(name)) for name in (alias, no_rowid, descending)}
    assert {name: (t.key, t.columns[0].nullable, t.columns[0].has_default) for name, t in found.items()} == {
        alias: (["id"], False, True), no_rowid: (["id"], False, False), descending: (["id"], True, False),
    }
