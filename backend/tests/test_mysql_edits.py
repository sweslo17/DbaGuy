"""MySQL grid editing (spec ⑤): the edit target from information_schema, and the rows an UPDATE matched (D-05-5, A1).
Scratch objects only (zz_05a_*)."""

import pytest

from dbaguy_backend.adapters.editing import NO_KEY, VIEWS
from dbaguy_backend.adapters.mysql.adapter import rows_matched
from dbaguy_backend.domain.errors import NotFound
from tests.edit_support import EditApp, MysqlScratch, delete, edit_env, insert, items_table, scratch, update, val
from tests.support import requires_mysql

pytestmark = requires_mysql


@pytest.fixture
def my():
    db = MysqlScratch()
    connection = db.open()
    try:
        yield db, connection
    finally:
        connection.close()
        db.close()


def test_rows_matched_is_read_from_an_updates_info():  # A1
    assert rows_matched("Rows matched: 1  Changed: 0  Warnings: 0") == 1
    assert rows_matched("Rows matched: 12  Changed: 3  Warnings: 1") == 12
    assert rows_matched("Datensätze gefunden: 2  Geändert: 0  Warnungen: 0") == 2  # lc_messages (G8)
    assert rows_matched("일치하는 Rows : 3개 변경됨: 2개  경고: 1개") == 3
    assert rows_matched("Rows matched: 1") is None
    assert rows_matched("") is None and rows_matched(None) is None


def test_matched_rows_under_a_translated_lc_messages(my):  # G8
    db, connection = my
    with scratch(db) as make:
        table = db.table(items_table(db, make))
        connection.run("SET SESSION lc_messages = 'de_DE'").close()

        def counts(sql):
            result = connection.run(sql)
            try:
                return result.matched_rows(), result.affected_rows()
            finally:
                result.close()

        assert counts(f"UPDATE {table} SET label = 'a' WHERE id = 1") == (1, 0)
        assert counts(f"INSERT INTO {table} (id, label) VALUES (5, 'e'), (6, 'f')") == (2, 2)  # Records: … not read


def test_an_invisible_key_column_is_not_a_key(my):  # SELECT * leaves it out (a generated invisible primary key)
    db, connection = my
    with scratch(db) as make:
        hidden = make("TABLE", "CREATE TABLE {t} (my_row_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT INVISIBLE "
                               "PRIMARY KEY, code INT NOT NULL, UNIQUE KEY u_code (code))")
        only = make("TABLE", "CREATE TABLE {t} (my_row_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT INVISIBLE "
                             "PRIMARY KEY, v INT)")
        found = {name: connection.edit_target(db.path(name)) for name in (hidden, only)}
    assert (found[hidden].key, found[hidden].key_source) == (["code"], "unique")
    assert found[only].reason == NO_KEY


def test_an_update_to_the_same_value_still_matched_its_row(my):  # D-05-5: affected rows would say 0
    db, connection = my
    with scratch(db) as make:
        table = db.table(items_table(db, make))

        def counts(sql):
            result = connection.run(sql)
            try:
                return result.matched_rows(), result.affected_rows()
            finally:
                result.close()

        assert counts(f"UPDATE {table} SET label = 'a' WHERE id = 1") == (1, 0)
        assert counts(f"UPDATE {table} SET label = 'z' WHERE id IN (2, 3)") == (2, 2)
        assert counts(f"UPDATE {table} SET label = 'z' WHERE id = 99") == (0, 0)
        assert counts(f"INSERT INTO {table} (id, label) VALUES (4, 'd')") == (1, 1)
        assert counts(f"DELETE FROM {table} WHERE id = 99") == (0, 0)


def test_a_table_with_a_primary_key_and_every_kind_of_column(my):
    db, connection = my
    with scratch(db) as make:
        table = make("TABLE", "CREATE TABLE {t} (id BIGINT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(20) NOT NULL, "
                              "amount DECIMAL(10,2), f FLOAT, d DOUBLE, flag TINYINT(1), bits BIT(8), kind ENUM('a','b'), "
                              "doc JSON, data BLOB, raw VARBINARY(16), day DATE, at DATETIME(6), "
                              "made TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, shape GEOMETRY, "
                              "twice INT GENERATED ALWAYS AS (CHAR_LENGTH(label)) VIRTUAL, "
                              "loud VARCHAR(40) GENERATED ALWAYS AS (CONCAT(label, '!')) STORED) ENGINE=InnoDB")
        target = connection.edit_target(db.path(table))
    assert (target.editable, target.key, target.transactional, target.backslash_escapes) == (True, ["id"], True, True)
    assert {c.name: (c.kind, c.compare, c.writable, c.has_default) for c in target.columns} == {
        "id": ("number", "equals", True, True), "label": ("text", "equals", True, False),
        "amount": ("number", "equals", True, False), "f": ("number", "none", True, False),
        "d": ("number", "none", True, False), "flag": ("number", "equals", True, False),
        "bits": ("number", "equals", True, False), "kind": ("text", "equals", True, False),
        "doc": ("json", "equals", True, False), "data": ("binary", "equals", True, False),
        "raw": ("binary", "equals", True, False), "day": ("datetime", "equals", True, False),
        "at": ("datetime", "equals", True, False), "made": ("datetime", "equals", True, True),
        "shape": ("binary", "none", False, False), "twice": ("number", "equals", False, True),
        "loud": ("text", "equals", False, True),
    }
    by_name = {c.name: c for c in target.columns}
    assert (by_name["twice"].read_only_reason, by_name["shape"].read_only_reason) == (
        "generated column", "geometry values are not edited here")
    assert (by_name["amount"].type_name, by_name["label"].nullable, by_name["at"].nullable) == ("decimal(10,2)", False, True)


def test_keys_engines_views_and_the_sql_mode(my):  # D-05-6, D-05-13, ruling E4
    db, connection = my
    with scratch(db) as make:
        unique = make("TABLE", "CREATE TABLE {t} (a INT NOT NULL, b INT NOT NULL, c INT, label VARCHAR(20) NOT NULL, "
                               "UNIQUE KEY u_ab (a, b), UNIQUE KEY u_c (c), UNIQUE KEY u_fn ((lower(label)))) "
                               "ENGINE=InnoDB")
        functional = make("TABLE", "CREATE TABLE {t} (label VARCHAR(20) NOT NULL, UNIQUE KEY u_fn ((lower(label)))) "
                                   "ENGINE=InnoDB")
        myisam = make("TABLE", "CREATE TABLE {t} (id INT PRIMARY KEY, v INT) ENGINE=MyISAM")
        view = make("VIEW", f"CREATE VIEW {{t}} AS SELECT * FROM {db.table(myisam)}")
        found = {name: connection.edit_target(db.path(name)) for name in (unique, functional, myisam)}
        found[view] = connection.edit_target(db.path(view, "view"))
        no_escapes = db.open(options={"sql_mode": "STRICT_TRANS_TABLES,NO_BACKSLASH_ESCAPES"})
        try:
            assert no_escapes.edit_target(db.path(myisam)).backslash_escapes is False
        finally:
            no_escapes.close()
    assert (found[unique].key, found[unique].key_source) == (["a", "b"], "unique")
    assert found[functional].reason == NO_KEY
    assert (found[myisam].editable, found[myisam].transactional) == (True, False)
    assert found[view].reason == VIEWS
    with pytest.raises(NotFound):
        connection.edit_target(db.path("zz_05a_never_there"))


# --- live applies (Task 10) -----------------------------------------------------------------------------------------


@pytest.fixture
def my_env(tmp_path):
    with edit_env("mysql", tmp_path) as made:
        yield made


def test_a_decimal_set_to_an_equal_value_is_matched_not_a_conflict(my_env):  # D-05-5, A1, Review Focus 3
    db, app, pid = my_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id INT PRIMARY KEY, amount DECIMAL(10,2)) ENGINE=InnoDB")
        db.execute(f"INSERT INTO {db.table(table)} VALUES (1, 1.10)")
        preview = app.preview(pid, db.path(table), updates=[update("u:1", {"id": 1}, {"amount": val("1.1")},
                                                                   {"amount": "1.10"})])
        done = app.tab(pid, None).apply(preview.plan_id)
        assert (done.outcome.outcome, done.affected) == ("committed", [(0, 1)])  # matched 1, changed 0


def test_zero_dates_and_json_originals_match(my_env):  # D-05-2
    db, app, pid = my_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id INT PRIMARY KEY, d DATE NOT NULL, doc JSON) ENGINE=InnoDB")
        db.execute("SET SESSION sql_mode = ''",
                   f"INSERT INTO {db.table(table)} VALUES (1, '0000-00-00', '{{\"b\": 1, \"a\": [1.10, 2]}}')",
                   "SET SESSION sql_mode = DEFAULT")
        tab = app.tab(pid, None)
        [[day, doc]] = tab.rows(f"SELECT d, doc FROM {db.table(table)}")
        assert day == "0000-00-00"
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"d": val("2026-10-19"), "doc": val('{"c": 3}')}, {"d": day, "doc": doc})])
        sql = preview.statements[0].sql
        assert "CAST(d AS CHAR) = '0000-00-00'" in sql and "doc = CAST(" in sql
        assert tab.apply(preview.plan_id).outcome.outcome == "committed"
        assert db.rows(f"SELECT CAST(d AS CHAR), CAST(doc AS CHAR) FROM {db.table(table)}") == [
            ("2026-10-19", '{"c": 3}')]


@pytest.mark.parametrize("sql_mode", [None, "STRICT_TRANS_TABLES,NO_BACKSLASH_ESCAPES"])
def test_backslashes_are_stored_as_typed_in_both_sql_modes(tmp_path, sql_mode):  # A9, D-05-13, Review Focus 1
    db = MysqlScratch()
    app = EditApp(tmp_path)
    try:
        pid = app.add_profile(db.profile(options={"sql_mode": sql_mode} if sql_mode else {}))
        typed = "C:\\tmp\\new\\"
        with scratch(db, app.close_tabs) as make:
            table = items_table(db, make)
            preview = app.preview(pid, db.path(table), updates=[update("u:1", {"id": 1}, {"label": val(typed)},
                                                                       {"label": "a"})])
            written = "'C:\\tmp\\new\\'" if sql_mode else "'C:\\\\tmp\\\\new\\\\'"
            assert f"SET label = {written}" in preview.statements[0].sql
            assert app.tab(pid, None).apply(preview.plan_id).outcome.outcome == "committed"
            assert db.rows(f"SELECT label FROM {db.table(table)} WHERE id = 1") == [(typed,)]
    finally:
        app.close()
        db.close()


def test_a_partial_apply_on_myisam_says_what_took_effect(my_env):  # D-05-6, Review Focus 5
    db, app, pid = my_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id BIGINT PRIMARY KEY, label VARCHAR(100) NOT NULL) ENGINE=MyISAM")
        db.execute(f"INSERT INTO {db.table(table)} VALUES (1, 'a'), (2, 'b')")
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"label": val("one")}, {"label": "a"}),
            update("u:2", {"id": 2}, {"label": val("two")}, {"label": "b"})])
        assert preview.warnings[0].code == "non_transactional"
        db.execute(f"UPDATE {db.table(table)} SET label = 'theirs' WHERE id = 2")
        done = app.tab(pid, None).apply(preview.plan_id)
        assert (done.outcome.outcome, done.outcome.applied, done.outcome.failed_change) == ("partial", ["u:1"], "u:2")
        assert done.error.message.endswith(": the statements before it stay applied (this table cannot roll back)")
        assert db.rows(f"SELECT id, label FROM {db.table(table)} ORDER BY id") == [(1, "one"), (2, "theirs")]
        [entry] = [entry for entry in app.history() if entry.sql == preview.script]
        assert entry.status == "error" and entry.error.startswith("Partly applied (1 of 2): ")


def test_the_server_refuses_a_write_on_a_read_only_session(my_env):  # R-05-7: MySQL answers 1792
    db, app, pid = my_env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), deletes=[delete("d:1", {"id": 1})])
        app.update_profile(pid, db.profile(read_only=True))
        tab = app.tab(pid, None)  # SET SESSION TRANSACTION READ ONLY at connect
        app.update_profile(pid, db.profile())  # the policy lets it pass now: only the server stops it
        done = tab.apply(preview.plan_id)
        assert (done.outcome.outcome, done.error.code) == ("rolled_back", "1792")
        assert db.rows(f"SELECT COUNT(*) FROM {db.table(table)}") == [(3,)]


def test_defaults_auto_increment_bits_enums_and_generated_columns(my_env):
    db, app, pid = my_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id INT AUTO_INCREMENT PRIMARY KEY, "
                              "kind ENUM('a','b') NOT NULL DEFAULT 'a', bits BIT(8) NULL, "
                              "loud VARCHAR(10) GENERATED ALWAYS AS (UPPER(kind)) VIRTUAL) ENGINE=InnoDB")
        preview = app.preview(pid, db.path(table), inserts=[insert("i:1", {}),
                                                            insert("i:2", {"kind": val("b"), "bits": val("5")})])
        assert preview.statements[0].sql == f"INSERT INTO dbaguy.{table} () VALUES ()"
        assert app.tab(pid, None).apply(preview.plan_id).outcome.outcome == "committed"
        assert db.rows(f"SELECT id, kind, bits + 0, loud FROM {db.table(table)} ORDER BY id") == [
            (1, "a", None, "A"), (2, "b", 5, "B")]
        refused = app.preview(pid, db.path(table), updates=[update("u:1", {"id": 1}, {"loud": val("X")},
                                                                   {"loud": "A"})])
        assert [(p.column, p.code) for p in refused.problems] == [("loud", "read_only_column")]
