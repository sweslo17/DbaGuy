"""Grid edits end to end on every relational engine (spec ⑤ §6 整合, §3.5, 成功標準): a preview through EditService,
an apply through the table-data session's worker, and each engine's own driver to check what the server holds. One
table shape for all (`items_table`); engine-specific behaviour is in test_<engine>_edits.py."""

import pytest

from dbaguy_backend.sql.select_builder import build_browse_select
from tests.edit_support import (
    ENGINES, NULL, SERVER_ENGINES, delete, edit_env, insert, items_table, scratch, update, val,
)
from tests.support import scaled, wait_until

DUPLICATE_KEY = {"postgres": "23505", "mysql": "1062", "mssql": "2627", "sqlite": "SQLITE_CONSTRAINT_PRIMARYKEY"}


@pytest.fixture(params=ENGINES)
def env(request, tmp_path):
    with edit_env(request.param, tmp_path) as made:
        yield made


@pytest.fixture(params=SERVER_ENGINES)
def server_env(request, tmp_path):  # the engines whose statements wait on a row lock (SQLite waits in its busy handler)
    with edit_env(request.param, tmp_path) as made:
        yield made


def items(db, table: str) -> list[tuple]:
    return db.rows(f"SELECT id, label, {db.q('order')}, note FROM {db.table(table)} ORDER BY id")


def test_a_commit_applies_deletes_updates_and_inserts_in_that_order(env):  # 成功標準 1, Review Focus 1, 2
    db, app, pid = env
    text = "it's 50% \\ done\nline 2 😀"
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(
            pid, db.path(table),
            updates=[update("u:1", {"id": 1}, {"label": val(text), "order": NULL, "note": val("")},
                            {"label": "a", "order": 1, "note": None})],
            inserts=[insert("i:1", {"id": val("9007199254740993"), "label": val("new")})],
            deletes=[delete("d:3", {"id": 3})],
        )
        assert preview.problems == [] and [s.kind for s in preview.statements] == ["delete", "update", "insert"]
        done = app.tab(pid, db.database).apply(preview.plan_id)
        assert done.types == ["started", "affected", "affected", "affected", "edit_outcome", "session_state",
                              "finished"]
        assert (done.outcome.outcome, done.outcome.applied, done.affected) == (
            "committed", ["d:3", "u:1", "i:1"], [(0, 1), (1, 1), (2, 1)])
        assert items(db, table) == [(1, text, None, ""), (2, "b", 2, "x"), (9007199254740993, "new", None, None)]
        [entry] = [entry for entry in app.history() if entry.sql == preview.script]
        assert (entry.status, entry.rows) == ("ok", 3)


def test_a_key_beyond_two_to_the_53_matches_exactly(env):  # Review Focus 3
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make, [(9007199254740992, "below", None, None), (9007199254740993, "big", None, None)])
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": "9007199254740993"}, {"label": val("bigger")}, {"label": "big"})])
        assert app.tab(pid, db.database).apply(preview.plan_id).outcome.outcome == "committed"
        assert [row[:2] for row in items(db, table)] == [(9007199254740992, "below"), (9007199254740993, "bigger")]


def test_a_cell_changed_since_loading_rolls_everything_back(env):  # 成功標準 2, R-05-5
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"label": val("mine 1")}, {"label": "a"}),
            update("u:2", {"id": 2}, {"label": val("mine 2")}, {"label": "b"})])
        db.execute(f"UPDATE {db.table(table)} SET label = 'theirs' WHERE id = 2")
        done = app.tab(pid, db.database).apply(preview.plan_id)
        assert (done.outcome.outcome, done.outcome.failed_change, done.outcome.statement) == ("rolled_back", "u:2", 1)
        assert done.error.code == "edit_conflict" and done.error.message == (
            "Row id = 2 was changed or deleted since it was loaded (or a trigger skipped it): nothing was applied")
        assert [row[:2] for row in items(db, table)] == [(1, "a"), (2, "theirs"), (3, "c")]


def test_a_change_to_another_column_of_the_same_row_is_kept(env):  # 成功標準 3
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), updates=[update("u:1", {"id": 1}, {"label": val("mine")},
                                                                   {"label": "a"})])
        db.execute(f"UPDATE {db.table(table)} SET note = 'theirs' WHERE id = 1")
        assert app.tab(pid, db.database).apply(preview.plan_id).outcome.outcome == "committed"
        assert items(db, table)[0] == (1, "mine", 1, "theirs")


def test_a_row_deleted_since_loading_is_already_gone(env):  # R-05-5
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), deletes=[delete("d:3", {"id": 3})])
        db.execute(f"DELETE FROM {db.table(table)} WHERE id = 3")
        done = app.tab(pid, db.database).apply(preview.plan_id)
        assert (done.outcome.outcome, done.outcome.already_gone, done.affected) == ("committed", ["d:3"], [(0, 0)])


def test_an_error_half_way_applies_nothing(env):  # spec §6 中途錯誤, Review Focus 5
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table),
                              updates=[update("u:1", {"id": 1}, {"label": val("changed")}, {"label": "a"})],
                              inserts=[insert("i:2", {"id": val("2"), "label": val("duplicate")})])
        done = app.tab(pid, db.database).apply(preview.plan_id)
        assert (done.outcome.outcome, done.outcome.failed_change, done.outcome.statement) == ("rolled_back", "i:2", 1)
        assert done.error.code == DUPLICATE_KEY[db.engine] and not done.finished.in_transaction
        assert items(db, table)[0][1] == "a"


def test_a_plan_is_applied_once(env):  # Review Focus 4, R-05-6
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), inserts=[insert("i:4", {"id": val("4"), "label": val("once")})])
        tab = app.tab(pid, db.database)
        assert tab.apply(preview.plan_id).outcome.outcome == "committed"
        again = tab.apply(preview.plan_id)
        other = app.tab(pid, db.database).apply(preview.plan_id)
        for refused in (again, other):
            assert refused.types == ["started", "error", "edit_outcome", "session_state", "finished"]
            assert (refused.error.code, refused.outcome.outcome) == ("plan_expired", "rolled_back")
        assert [row[0] for row in items(db, table)] == [1, 2, 3, 4]


def test_a_paused_browse_is_ended_before_the_apply(env):  # R-05-4: one connection, the browse goes first
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        db.fill(db.table(table), 2000, 100)
        tab = app.tab(pid, db.database)
        browse = tab.browse(build_browse_select(db.dialect, db.path(table), None, []))
        preview = app.preview(pid, db.path(table), updates=[update("u:1", {"id": 1}, {"label": val("x")},
                                                                   {"label": "a"})])
        done = tab.apply(preview.plan_id)
        order = [(e.type, e.request_id) for e in tab.recorder.events if e.type in ("started", "finished")]
        assert order.index(("finished", browse)) < order.index(("started", done.events[0].request_id))
        assert done.outcome.outcome == "committed" and items(db, table)[0][1] == "x"


def test_a_profile_made_read_only_after_the_preview_sends_nothing(env):  # R-05-7: the policy blocks first
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), deletes=[delete("d:1", {"id": 1})])
        tab = app.tab(pid, db.database)
        app.update_profile(pid, db.profile(read_only=True))
        done = tab.apply(preview.plan_id)
        assert (done.error.code, done.outcome.outcome) == ("read_only", "rolled_back")
        assert len(items(db, table)) == 3 and not [e for e in app.history() if e.sql == preview.script]


def test_cancel_while_waiting_for_a_lock_rolls_back(server_env):  # spec §6 等鎖時 Cancel, Review Focus 5
    db, app, pid = server_env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), updates=[
            update("u:2", {"id": 2}, {"label": val("mine")}, {"label": "b"}),
            update("u:1", {"id": 1}, {"label": val("mine")}, {"label": "a"})])
        tab = app.tab(pid, db.database)
        with db.holding(f"UPDATE {db.table(table)} SET label = 'held' WHERE id = 1"):
            rid = tab.submit_apply(preview.plan_id)
            # scoped to the table (M5); MySQL sees the wait 1–2 s after it starts (TIME >= 2), well inside the budget
            assert wait_until(lambda: db.waiting(table), scaled(10)), "the apply never waited for the row lock"
            tab.worker.cancel(rid)
            done = tab.wait(rid)
        assert (done.error.code, done.error.message) == ("cancelled", "Cancelled: nothing was applied")
        assert (done.outcome.outcome, done.outcome.failed_change) == ("rolled_back", "u:1")
        assert done.finished.cancelled and not done.finished.in_transaction
        assert [row[:2] for row in items(db, table)] == [(1, "a"), (2, "b"), (3, "c")]


def test_a_key_column_can_be_changed(env):  # CR-4: the WHERE uses the loaded key
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), updates=[update("u:2", {"id": 2}, {"id": val("20")}, {"id": 2})])
        assert app.tab(pid, db.database).apply(preview.plan_id).outcome.outcome == "committed"
        assert [row[0] for row in items(db, table)] == [1, 3, 20]
