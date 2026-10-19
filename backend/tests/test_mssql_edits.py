"""SQL Server grid editing (spec ⑤): the edit target from the catalog views. Scratch objects only (zz_05a_*)."""

import pytest

from dbaguy_backend.adapters.editing import NO_KEY, VIEWS
from dbaguy_backend.adapters.mssql.editing import GRAPH_TABLE, HISTORY_TABLE, LEDGER_TABLE, table_flags_reason
from dbaguy_backend.domain.errors import NotFound
from tests.edit_support import MssqlScratch, delete, edit_env, insert, items_table, scratch, update, val
from tests.support import requires_mssql

pytestmark = requires_mssql


@pytest.fixture
def ms():
    db = MssqlScratch()
    connection = db.open()
    try:
        yield db, connection
    finally:
        connection.close()
        db.close()


def test_a_table_with_a_primary_key_and_every_kind_of_column(ms):
    db, connection = ms
    with scratch(db) as make:
        alias = make("TYPE", "CREATE TYPE {t} FROM nvarchar(200)")
        table = make("TABLE", "CREATE TABLE {t} (id int IDENTITY PRIMARY KEY, code varchar(20) NOT NULL, "
                              "name nvarchar(50), note nvarchar(max), amount decimal(10,2), f float, r real, at datetime, "
                              "at7 datetime2(7), at3 datetime2(3), dto datetimeoffset, tm time, u uniqueidentifier, "
                              "flag bit, data varbinary(16), old text, oldn ntext, img image, x xml, v sql_variant, "
                              f"h hierarchyid, mail {db.table(alias)}, total AS (amount * 2), ver rowversion)")
        period = make("TABLE", "CREATE TABLE {t} (id int PRIMARY KEY, v int, "
                               "vf datetime2 GENERATED ALWAYS AS ROW START NOT NULL, "
                               "vt datetime2 GENERATED ALWAYS AS ROW END NOT NULL, PERIOD FOR SYSTEM_TIME (vf, vt))")
        target = connection.edit_target(db.path(table))
        versioned = connection.edit_target(db.path(period))
    assert (target.editable, target.key, target.key_source) == (True, ["id"], "primary_key")
    assert {c.name: (c.kind, c.compare, c.writable, c.unicode) for c in target.columns} == {
        "id": ("number", "equals", False, True), "code": ("text", "equals", True, False),
        "name": ("text", "equals", True, True), "note": ("text", "equals", True, True),
        "amount": ("number", "equals", True, True), "f": ("number", "none", True, True),
        "r": ("number", "none", True, True), "at": ("datetime", "equals", True, True),
        "at7": ("datetime", "none", True, True), "at3": ("datetime", "equals", True, True),
        "dto": ("datetime", "none", True, True), "tm": ("datetime", "none", True, True),
        "u": ("text", "equals", True, True), "flag": ("bool", "equals", True, True),
        "data": ("binary", "equals", True, True), "old": ("text", "none", True, False),
        "oldn": ("text", "none", True, True), "img": ("binary", "none", True, True), "x": ("text", "none", True, True),
        "v": ("binary", "none", False, True), "h": ("binary", "none", False, True), "mail": ("text", "equals", True, True),
        "total": ("number", "equals", False, True), "ver": ("binary", "equals", False, True),
    }
    by_name = {c.name: c for c in target.columns}
    assert [by_name[n].read_only_reason for n in ("id", "total", "ver", "v", "h")] == [
        "IDENTITY column", "computed column", "rowversion column", "sql_variant values are not edited here",
        "hierarchyid values are not edited here"]
    assert [by_name[n].type_name for n in ("code", "name", "amount", "at7", "mail")] == [
        "varchar(20)", "nvarchar(50)", "decimal(10,2)", "datetime2(7)", alias]
    assert [c.name for c in target.columns if c.has_default] == ["id", "total", "ver"]
    assert {c.name: (c.writable, c.read_only_reason) for c in versioned.columns if c.name in ("vf", "vt")} == {
        "vf": (False, "period column (system-versioned)"), "vt": (False, "period column (system-versioned)")}


def test_keys_views_and_missing_tables(ms):  # ruling E4
    db, connection = ms
    with scratch(db) as make:
        unique = make("TABLE", "CREATE TABLE {t} (a int NOT NULL, b int NOT NULL, c int NULL, d int NOT NULL, "
                               "e int NOT NULL)")
        qualified = db.table(unique)
        db.execute(
            f"CREATE UNIQUE INDEX u_abd ON {qualified} (a, b, d)",
            f"CREATE UNIQUE INDEX u_c ON {qualified} (c)",  # a nullable column: not a key
            f"CREATE UNIQUE INDEX u_d ON {qualified} (d) WHERE d > 0",  # filtered
            f"CREATE UNIQUE INDEX u_ea ON {qualified} (e, a) INCLUDE (b)",  # INCLUDE is not part of the key
            f"CREATE UNIQUE INDEX u_b ON {qualified} (b)",
            f"ALTER INDEX u_b ON {qualified} DISABLE",  # a disabled index guarantees nothing
        )
        floaty = make("TABLE", "CREATE TABLE {t} (f float PRIMARY KEY, v int)")
        loose = make("TABLE", "CREATE TABLE {t} (a int, b int)")
        view = make("VIEW", f"CREATE VIEW {{t}} AS SELECT a, b FROM {db.table(loose)}")
        found = {name: connection.edit_target(db.path(name)) for name in (unique, floaty, loose)}
        found[view] = connection.edit_target(db.path(view, "view"))
    assert (found[unique].key, found[unique].key_source) == (["e", "a"], "unique")
    assert found[floaty].reason == "The key column f (float) cannot be matched exactly: a row cannot be identified"
    assert (found[loose].reason, found[view].reason) == (NO_KEY, VIEWS)
    with pytest.raises(NotFound):
        connection.edit_target(db.path("zz_05a_never_there"))


def test_a_hypothetical_index_is_not_a_key(ms):  # a Tuning Advisor leftover: unique in the catalog, enforces nothing
    db, connection = ms
    with scratch(db) as make:
        table = make("TABLE", "CREATE TABLE {t} (a int NOT NULL, b int NOT NULL)")
        db.execute(f"CREATE UNIQUE INDEX u_a ON {db.table(table)} (a) WITH STATISTICS_ONLY = -1")
        target = connection.edit_target(db.path(table))
    assert target.reason == NO_KEY


def test_graph_and_history_tables_are_read_only(ms):  # G9
    db, connection = ms
    with scratch(db) as make:
        node = make("TABLE", "CREATE TABLE {t} (id int NOT NULL PRIMARY KEY, name nvarchar(50)) AS NODE")
        edge = make("TABLE", "CREATE TABLE {t} (w int) AS EDGE")
        plain = make("TABLE", "CREATE TABLE {t} (id int NOT NULL PRIMARY KEY)")
        found = {name: connection.edit_target(db.path(name)) for name in (node, edge, plain)}
        history = make("TABLE", "CREATE TABLE {t} (id int NOT NULL, v int, vf datetime2 NOT NULL, vt datetime2 NOT NULL)")
        current = make("TABLE", "CREATE TABLE {t} (id int NOT NULL PRIMARY KEY, v int, "
                                "vf datetime2 GENERATED ALWAYS AS ROW START NOT NULL, "
                                "vt datetime2 GENERATED ALWAYS AS ROW END NOT NULL, PERIOD FOR SYSTEM_TIME (vf, vt)) "
                                f"WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = {db.table(history)}))")
        try:
            found[history] = connection.edit_target(db.path(history))
            found[current] = connection.edit_target(db.path(current))
        finally:
            db.execute(f"ALTER TABLE {db.table(current)} SET (SYSTEM_VERSIONING = OFF)")
    assert (found[node].reason, found[edge].reason) == ("Graph node and edge tables are read-only here",) * 2
    assert found[history].reason == "History table of a system-versioned table: read-only here"
    assert found[current].editable and found[plain].editable


def test_the_table_flags_that_make_a_table_read_only():  # G9; a ledger table cannot be dropped, so none is made
    assert table_flags_reason(0, False, False, 0) is None
    assert table_flags_reason(2, False, False, 0) is None  # the current table of a system-versioned pair
    assert table_flags_reason(1, False, False, 0) == HISTORY_TABLE
    assert table_flags_reason(0, True, False, 0) == table_flags_reason(0, False, True, 0) == GRAPH_TABLE
    # sys.tables.ledger_type: 1 the ledger's history table, 2 updatable, 3 append-only
    assert [table_flags_reason(0, False, False, kind) for kind in (1, 2, 3)] == [LEDGER_TABLE] * 3


# --- live applies (Task 10) -----------------------------------------------------------------------------------------


@pytest.fixture
def ms_env(tmp_path):
    with edit_env("mssql", tmp_path) as made:
        yield made


def test_a_varchar_key_goes_without_n_and_unicode_text_with_it(ms_env):  # D-05-2, A8, ruling E17, Review Focus 1
    db, app, pid = ms_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (code varchar(20) NOT NULL PRIMARY KEY, label varchar(50), "
                              "name nvarchar(50))")
        db.execute(f"INSERT INTO {db.table(table)} VALUES ('k42', 'old', N'old')")
        preview = app.preview(pid, db.path(table), updates=[update(
            "u:1", {"code": "k42"}, {"label": val("café 50%"), "name": val("名字")}, {"label": "old", "name": "old"})])
        assert preview.statements[0].sql == (
            f"UPDATE dbo.{table} SET label = N'café 50%', name = N'名字' "
            "WHERE code = 'k42' AND label = 'old' AND name = N'old'")
        assert app.tab(pid, db.database).apply(preview.plan_id).outcome.outcome == "committed"
        assert db.rows(f"SELECT label, name FROM {db.table(table)}") == [("café 50%", "名字")]


def test_types_round_trip_and_datetime2_7_is_not_checked(ms_env):  # D-05-2, spec §3.2.2
    db, app, pid = ms_env
    names = ["amount", "at", "at7", "u", "flag", "data", "note"]
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id int PRIMARY KEY, amount decimal(10,2), at datetime, "
                              "at7 datetime2(7), u uniqueidentifier, flag bit, data varbinary(16), note nvarchar(max))")
        db.execute(f"INSERT INTO {db.table(table)} VALUES (1, 1.10, '2026-10-19T12:34:56.123', "
                   "'2026-10-19T12:34:56.1234567', 'A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11', 1, 0x00ff, N'50% done')")
        tab = app.tab(pid, db.database)
        [row] = tab.rows(f"SELECT {', '.join(names)} FROM {db.table(table)}")
        new = {"amount": "2.25", "at": "2026-10-20 01:02:03", "at7": "2026-10-20 01:02:03.1234567",
               "u": "00000000-0000-0000-0000-000000000001", "flag": "false", "data": "0x0102", "note": "it's 100%"}
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {name: val(text) for name, text in new.items()}, dict(zip(names, row)))])
        assert [u.column for u in preview.statements[0].unchecked] == ["at7"]
        done = tab.apply(preview.plan_id)
        assert done.outcome.outcome == "committed", done.error and done.error.message
        assert db.rows(f"SELECT CONVERT(varchar(10), amount), CONVERT(varchar(27), at7, 121), data, note "
                       f"FROM {db.table(table)}") == [("2.25", "2026-10-20 01:02:03.1234567", b"\x01\x02", "it's 100%")]


def test_triggers_count_the_statements_own_row_and_their_prints_reach_the_apply(ms_env):  # A3, spec §3.5
    db, app, pid = ms_env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        log = make("TABLE", "CREATE TABLE {t} (n int)")
        db.execute(f"CREATE TRIGGER {db.q(table + '_after')} ON {db.table(table)} AFTER UPDATE AS BEGIN "
                   f"INSERT INTO {db.table(log)} VALUES (1), (2), (3); PRINT 'logged three rows' END")
        instead = make("TABLE", "CREATE TABLE {t} (id int PRIMARY KEY, v nvarchar(20))")
        db.execute(f"INSERT INTO {db.table(instead)} VALUES (1, N'a')",
                   f"CREATE TRIGGER {db.q(instead + '_instead')} ON {db.table(instead)} INSTEAD OF UPDATE AS "
                   f"UPDATE t SET v = i.v FROM {db.table(instead)} t JOIN inserted i ON i.id = t.id")
        tab = app.tab(pid, db.database)
        after = tab.apply(app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"label": val("x")}, {"label": "a"})]).plan_id)
        assert (after.outcome.outcome, after.affected) == ("committed", [(0, 1)])
        assert "logged three rows" in after.messages
        replaced = tab.apply(app.preview(pid, db.path(instead), updates=[
            update("u:1", {"id": 1}, {"v": val("b")}, {"v": "a"})]).plan_id)
        assert (replaced.outcome.outcome, replaced.affected) == ("committed", [(0, 1)])
        assert db.rows(f"SELECT v FROM {db.table(instead)}") == [("b",)]


def test_a_session_with_nocount_on_still_counts_inside_the_frame(ms_env):  # A2
    db, app, pid = ms_env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        tab = app.tab(pid, db.database)
        tab.run("SET NOCOUNT ON")  # what a server whose `user options` turn NOCOUNT on gives every session
        done = tab.apply(app.preview(pid, db.path(table), deletes=[delete("d:1", {"id": 1})]).plan_id)
        assert (done.outcome.outcome, done.affected) == ("committed", [(0, 1)])


def test_a_transaction_ending_error_rolls_back(ms_env):  # A15, Review Focus 5
    db, app, pid = ms_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id int PRIMARY KEY, label nvarchar(20), day date)")
        db.execute(f"INSERT INTO {db.table(table)} VALUES (1, N'a', '2026-01-01'), (2, N'b', '2026-01-01')")
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"label": val("changed")}, {"label": "a"}),
            update("u:2", {"id": 2}, {"day": val("not a date")}, {"day": "2026-01-01"})])
        done = app.tab(pid, db.database).apply(preview.plan_id)
        assert (done.outcome.outcome, done.outcome.failed_change, done.error.code) == ("rolled_back", "u:2", "241")
        assert not done.finished.in_transaction
        assert db.rows(f"SELECT label FROM {db.table(table)} WHERE id = 1") == [("a",)]


def test_identity_computed_and_rowversion_columns_and_default_values(ms_env):
    db, app, pid = ms_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id int IDENTITY PRIMARY KEY, label nvarchar(20) NOT NULL "
                              "DEFAULT N'none', twice AS (id * 2), ver rowversion)")
        preview = app.preview(pid, db.path(table), inserts=[insert("i:1", {}), insert("i:2", {"label": val("given")})])
        assert preview.statements[0].sql == f"INSERT INTO dbo.{table} DEFAULT VALUES"
        assert app.tab(pid, db.database).apply(preview.plan_id).outcome.outcome == "committed"
        assert db.rows(f"SELECT id, label, twice FROM {db.table(table)} ORDER BY id") == [(1, "none", 2), (2, "given", 4)]
        refused = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"twice": val("9"), "ver": val("0x01")},
                   {"twice": 2, "ver": {"$type": "bytes", "size": 8, "preview": "0000000000000001"}})])
        assert sorted((p.column, p.code) for p in refused.problems) == [
            ("twice", "read_only_column"), ("ver", "read_only_column")]


def test_a_trigger_that_answers_with_rows_fails_closed(ms_env):  # pre-flight M6, E9, A3, Review Focus 5
    db, app, pid = ms_env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        trigger = f"{db.q(db.schema)}.{db.q(table + '_rows')}"
        db.execute(f"CREATE TRIGGER {trigger} ON {db.table(table)} AFTER UPDATE AS SELECT 1 AS x")
        tab = app.tab(pid, db.database)
        done = tab.apply(app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"label": val("x")}, {"label": "a"})]).plan_id)
        assert (done.outcome.outcome, done.outcome.failed_change, done.error.code) == (
            "rolled_back", "u:1", "row_count_unknown")
        assert done.error.message == "Could not confirm how many rows changed: nothing was applied"
        assert not done.finished.in_transaction
        assert db.rows(f"SELECT label FROM {db.table(table)} WHERE id = 1") == [("a",)]
        db.execute(f"DROP TRIGGER {trigger}")  # the trigger's rows were read to the end: the session goes on
        again = tab.apply(app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"label": val("x")}, {"label": "a"})]).plan_id)
        assert (again.outcome.outcome, again.affected) == ("committed", [(0, 1)])
        assert db.rows(f"SELECT label FROM {db.table(table)} WHERE id = 1") == [("x",)]


DATE_COLUMNS = {"d": "date", "dt": "datetime", "sdt": "smalldatetime", "dt2": "datetime2", "dto": "datetimeoffset"}
LANGUAGES = {"British": "dmy", "us_english": "mdy"}  # the language sets DATEFORMAT
DATE_TABLE = ("CREATE TABLE {t} (id int PRIMARY KEY, "
              + ", ".join(f"{name} {kind}" for name, kind in DATE_COLUMNS.items()) + ")")


def _language_tab(app, pid, db, language: str):
    tab = app.tab(pid, db.database)
    tab.run(f"SET LANGUAGE {language}")
    assert tab.rows("SELECT date_format FROM sys.dm_exec_sessions WHERE session_id = @@SPID") == [
        [LANGUAGES[language]]]
    return tab


def test_dates_are_read_alike_under_a_dmy_and_an_mdy_language(ms_env):  # ruling F1 (G5)
    db, app, pid = ms_env
    names = list(DATE_COLUMNS)
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", DATE_TABLE)

        def stored(*ids: int) -> list[tuple]:  # style 121 does not depend on the language
            return db.rows(f"SELECT {', '.join(f'CONVERT(varchar(40), {n}, 121)' for n in names)} "
                           f"FROM {db.table(table)} WHERE id IN ({', '.join(map(str, ids))}) ORDER BY id")

        found = {}
        for step, language in enumerate(LANGUAGES):
            tab = _language_tab(app, pid, db, language)
            day, noon = 10 * step + 1, 10 * step + 2
            preview = app.preview(pid, db.path(table), inserts=[
                insert("i:1", {"id": val(str(day)), **{name: val("2026-10-05") for name in names}}),
                insert("i:2", {"id": val(str(noon)), **{name: val("2026-10-05 12:00:00") for name in names}})])
            assert [s.sql.count("N'20261005'") for s in preview.statements] == [5, 0]
            assert [s.sql.count("N'2026-10-05T12:00:00'") for s in preview.statements] == [0, 5]
            assert tab.apply(preview.plan_id).outcome.outcome == "committed"
            inserted = stored(day, noon)
            # the originals as this session loaded them are matched in this language (date, datetime, smalldatetime
            # are checked; datetime2(7) and datetimeoffset are not)
            loaded = {row[0]: dict(zip(names, row[1:]))
                      for row in tab.rows(f"SELECT id, {', '.join(names)} FROM {db.table(table)} "
                                          f"WHERE id IN ({day}, {noon})")}
            preview = app.preview(pid, db.path(table), updates=[
                update("u:1", {"id": day}, {name: val("2026-11-07") for name in names}, loaded[day]),
                update("u:2", {"id": noon}, {name: val("2026-11-07 13:14:15") for name in names}, loaded[noon])])
            assert [[u.column for u in s.unchecked] for s in preview.statements] == [["dt2", "dto"]] * 2
            done = tab.apply(preview.plan_id)
            assert (done.outcome.outcome, done.affected) == ("committed", [(0, 1), (1, 1)]), done.error
            found[language] = (inserted, stored(day, noon))
    zero = ("2026-10-05", "2026-10-05 00:00:00.000", "2026-10-05 00:00:00.000", "2026-10-05 00:00:00.0000000",
            "2026-10-05 00:00:00.0000000 +00:00")
    twelve = ("2026-10-05", "2026-10-05 12:00:00.000", "2026-10-05 12:00:00.000", "2026-10-05 12:00:00.0000000",
              "2026-10-05 12:00:00.0000000 +00:00")
    updated = [("2026-11-07", "2026-11-07 00:00:00.000", "2026-11-07 00:00:00.000", "2026-11-07 00:00:00.0000000",
                "2026-11-07 00:00:00.0000000 +00:00"),
               ("2026-11-07", "2026-11-07 13:14:15.000", "2026-11-07 13:14:00.000", "2026-11-07 13:14:15.0000000",
                "2026-11-07 13:14:15.0000000 +00:00")]  # smalldatetime keeps minutes
    assert found["British"] == found["us_english"] == ([zero, twelve], updated)


def test_a_one_digit_hour_is_refused_alike_under_a_dmy_and_an_mdy_language(ms_env):  # ruling F1 (G5)
    db, app, pid = ms_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", DATE_TABLE)
        found = {}
        for step, language in enumerate(LANGUAGES):
            tab = _language_tab(app, pid, db, language)
            found[language] = {}
            for offset, name in enumerate(DATE_COLUMNS):
                preview = app.preview(pid, db.path(table), inserts=[
                    insert("i:1", {"id": val(str(10 * step + offset)), name: val("2026-10-05 1:02:03")})])
                assert "N'2026-10-05T1:02:03'" in preview.statements[0].sql
                done = tab.apply(preview.plan_id)
                assert not done.finished.in_transaction
                found[language][name] = (done.outcome.outcome, done.error and done.error.code)
        assert db.rows(f"SELECT COUNT(*) FROM {db.table(table)}") == [(0,)]
    # the server refuses the T form with a one-digit hour (conversion failed; smalldatetime: 295) in either language:
    # never read as another date
    refused = {name: ("rolled_back", "241") for name in DATE_COLUMNS} | {"sdt": ("rolled_back", "295")}
    assert found["British"] == found["us_english"] == refused
