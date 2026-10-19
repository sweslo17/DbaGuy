"""SQLite grid editing (spec ⑤): the edit target from the schema (affinity, keys, views, virtual tables)."""

import pytest

from dbaguy_backend.adapters.editing import NO_KEY, VIEWS
from tests.edit_support import SqliteScratch, edit_env, insert, items_table, scratch, update, val
from tests.support import scaled


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


def test_any_in_a_strict_table_keeps_values_as_given(lite):  # review G4: STRICT ANY has no affinity
    db, connection = lite
    with scratch(db) as make:
        strict = make("TABLE", "CREATE TABLE {t} (id INTEGER PRIMARY KEY, a ANY, n INT, s TEXT) STRICT")
        loose = make("TABLE", "CREATE TABLE {t} (id INTEGER PRIMARY KEY, a ANY)")
        found = {name: connection.edit_target(db.path(name)) for name in (strict, loose)}
        db.execute(*(f"INSERT INTO {db.table(name)} (a) VALUES ('007')" for name in (strict, loose)))
        stored = [db.rows(f"SELECT typeof(a), a FROM {db.table(name)}")[0] for name in (strict, loose)]
    assert {c.name: (c.kind, c.compare) for c in found[strict].columns} == {
        "id": ("number", "equals"), "a": ("other", "equals"), "n": ("number", "equals"), "s": ("text", "equals"),
    }
    assert [(c.name, c.kind) for c in found[loose].columns] == [("id", "number"), ("a", "number")]
    assert stored == [("text", "007"), ("integer", 7)]  # why: STRICT ANY keeps the text, NUMERIC affinity converts it


def test_a_virtual_tables_internal_tables_are_read_only_and_names_match_in_any_case(lite):  # review G4
    db, connection = lite
    with scratch(db) as make:
        fts = make("TABLE", "CREATE VIRTUAL TABLE {t} USING fts5(body)")
        keyed = make("TABLE", "CREATE TABLE {t} (id INTEGER PRIMARY KEY)")
        shadows = [f"{fts}_{part}" for part in ("data", "idx", "content", "docsize", "config")]
        found = {name: connection.edit_target(db.path(name)) for name in (fts, *shadows)}
        upper = connection.edit_target(db.path(keyed.upper()))  # SQLite finds a table whatever the case
    assert {name: (t.editable, t.reason) for name, t in found.items()} == {
        fts: (False, "Virtual tables are read-only here"),
        **dict.fromkeys(shadows, (False, "Internal table of a virtual table: read-only here")),
    }
    assert (upper.editable, upper.key) == (True, ["id"])


# --- live applies (Task 9) ------------------------------------------------------------------------------------------


@pytest.fixture
def lite_env(tmp_path):
    with edit_env("sqlite", tmp_path) as made:
        yield made


def test_a_commit_blocked_by_another_sessions_reader_rolls_back(lite_env):  # A14, ruling E10
    db, app, pid = lite_env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        db.fill(db.table(table), 3000, 100)
        app.tab(pid, None).browse(f"SELECT * FROM {db.table(table)}")  # paused: its read lock stays
        preview = app.preview(pid, db.path(table), updates=[update("u:1", {"id": 1}, {"label": val("x")},
                                                                   {"label": "a"})])
        done = app.tab(pid, None).apply(preview.plan_id, timeout=scaled(30))  # the 5 s busy timeout, then the error
        assert (done.outcome.outcome, done.outcome.statement, done.error.code) == ("rolled_back", None, "SQLITE_BUSY")
        assert done.error.message == "database is locked\nThe transaction was rolled back: nothing was applied"
        assert not done.finished.in_transaction
        assert db.rows(f"SELECT label FROM {db.table(table)} WHERE id = 1") == [("a",)]


def test_a_big_integer_in_a_column_without_a_type_is_found(lite_env):  # ruling E7, Review Focus 3
    db, app, pid = lite_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id PRIMARY KEY, label TEXT)")
        db.execute(f"INSERT INTO {db.table(table)} VALUES (9007199254740993, 'big'), ('9007199254740993x', 'text')")
        tab = app.tab(pid, None)
        [[cell]] = tab.rows(f"SELECT id FROM {db.table(table)} WHERE label = 'big'")
        assert cell == "9007199254740993"  # what the grid holds: encode_value made the integer a string
        preview = app.preview(pid, db.path(table), updates=[update("u:1", {"id": cell}, {"label": val("bigger")},
                                                                   {"label": "big"})])
        assert "WHERE id IN (9007199254740993, '9007199254740993')" in preview.statements[0].sql
        assert tab.apply(preview.plan_id).outcome.outcome == "committed"
        assert db.rows(f"SELECT typeof(id), label FROM {db.table(table)} ORDER BY label") == [
            ("integer", "bigger"), ("text", "text")]


def test_declared_types_shape_values_defaults_and_checks(lite_env):  # ruling E6
    db, app, pid = lite_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id INTEGER PRIMARY KEY, made DATETIME DEFAULT CURRENT_TIMESTAMP, "
                              "ok BOOLEAN NOT NULL DEFAULT 0, price REAL, twice INT GENERATED ALWAYS AS (id * 2) VIRTUAL)")
        preview = app.preview(pid, db.path(table), inserts=[
            insert("i:1", {"made": val("2026-10-19 12:00:00"), "ok": val("yes"), "price": val("1.5")}),
            insert("i:2", {})])
        assert [s.sql for s in preview.statements] == [
            f"INSERT OR ABORT INTO main.{table} (made, ok, price) VALUES ('2026-10-19 12:00:00', 1, 1.5)",
            f"INSERT OR ABORT INTO main.{table} DEFAULT VALUES"]
        tab = app.tab(pid, None)
        assert tab.apply(preview.plan_id).outcome.outcome == "committed"
        assert db.rows(f"SELECT id, made, ok, price, twice FROM {db.table(table)} WHERE id = 1") == [
            (1, "2026-10-19 12:00:00", 1, 1.5, 2)]
        update_price = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"price": val("2")}, {"price": 1.5})])
        assert [(u.column, u.reason) for u in update_price.statements[0].unchecked] == [
            ("price", "REAL values cannot be compared exactly")]
        refused = app.preview(pid, db.path(table), updates=[update("u:1", {"id": 1}, {"twice": val("9")},
                                                                   {"twice": 2})])
        assert [(p.column, p.code) for p in refused.problems] == [("twice", "read_only_column")]


@pytest.mark.parametrize("clause", ["REPLACE", "IGNORE"])
def test_a_constraints_own_conflict_clause_never_applies(lite_env, clause):  # 05a final review I1
    # Without the statement's OR ABORT, REPLACE deleted rows 2 and 3 and the apply said committed; IGNORE skipped the
    # change and read as a conflict
    db, app, pid = lite_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", f"CREATE TABLE {{t}} (id INTEGER PRIMARY KEY ON CONFLICT {clause}, "
                              f"code TEXT NOT NULL UNIQUE ON CONFLICT {clause}, label TEXT)")
        rows = [(1, "a", "one"), (2, "b", "two"), (3, "c", "three")]
        db.execute(f"INSERT INTO {db.table(table)} VALUES {', '.join(map(str, rows))}")
        tab = app.tab(pid, None)
        for change, errors in (
            ({"updates": [update("u:1", {"id": 1}, {"code": val("b")}, {"code": "a"})]},
             ("SQLITE_CONSTRAINT_UNIQUE", "UNIQUE constraint failed")),
            ({"inserts": [insert("i:1", {"id": val("3"), "code": val("z"), "label": val("new")})]},
             ("SQLITE_CONSTRAINT_PRIMARYKEY", "UNIQUE constraint failed")),
        ):
            preview = app.preview(pid, db.path(table), **change)
            assert preview.statements[0].sql.startswith(("UPDATE OR ABORT ", "INSERT OR ABORT INTO "))
            done = tab.apply(preview.plan_id)
            assert (done.outcome.outcome, done.outcome.statement, done.outcome.applied) == ("rolled_back", 0, [])
            assert (done.error.code, done.error.message.split(":")[0]) == errors
            assert db.rows(f"SELECT id, code, label FROM {db.table(table)} ORDER BY id") == rows
