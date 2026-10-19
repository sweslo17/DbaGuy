"""The apply where only a live server shows the truth (Task 8 fix round 1): a table that cannot roll back keeps what a
statement changed before it failed (ruling G13, MySQL MyISAM), and a SQL Server trigger can commit the apply's
transaction (3609). zz_05a_* objects only, dropped by exact name. The shared flows on every engine are in
test_edit_flows.py (Task 9)."""

import pytest

from tests.edit_support import edit_env, insert, items_table, scratch, update, val
from tests.support import requires_mssql, requires_mysql, scaled, wait_until


@pytest.fixture
def my_env(tmp_path):
    with edit_env("mysql", tmp_path) as made:
        yield made


@pytest.fixture
def ms_env(tmp_path):
    with edit_env("mssql", tmp_path) as made:
        yield made


MYISAM = "CREATE TABLE {t} (id BIGINT PRIMARY KEY, label VARCHAR(100) NOT NULL) ENGINE=MyISAM"


@requires_mysql
def test_a_myisam_update_whose_after_trigger_fails_is_unknown_and_its_change_stays(my_env):  # ruling G13 (a)
    db, app, pid = my_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", MYISAM)
        audit = make("TABLE", "CREATE TABLE {t} (id INT PRIMARY KEY) ENGINE=InnoDB")
        db.execute(f"INSERT INTO {db.table(table)} VALUES (1, 'a'), (2, 'b')",
                   f"INSERT INTO {db.table(audit)} VALUES (1)")
        db.execute(f"CREATE TRIGGER {db.q(table + '_tr')} AFTER UPDATE ON {db.table(table)} FOR EACH ROW "
                   f"INSERT INTO {db.table(audit)} VALUES (1)")  # 1062 after the row has changed
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"label": val("one")}, {"label": "a"}),
            update("u:2", {"id": 2}, {"label": val("two")}, {"label": "b"})])
        assert [w.code for w in preview.warnings] == ["non_transactional"]
        done = app.tab(pid, db.database).apply(preview.plan_id)
        rows = db.rows(f"SELECT id, label FROM {db.table(table)} ORDER BY id")
    assert (done.outcome.outcome, done.outcome.applied, done.outcome.failed_change, done.error.code) == (
        "unknown", [], "u:1", "1062")
    assert rows == [(1, "one"), (2, "b")] and not done.finished.in_transaction  # what is true: u:1 took effect


@requires_mysql
def test_a_cancel_during_a_myisam_after_trigger_is_unknown_and_its_change_stays(my_env):  # ruling G13 (b)
    db, app, pid = my_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", MYISAM)
        db.execute(f"INSERT INTO {db.table(table)} VALUES (1, 'a'), (2, 'b')")
        db.execute(f"CREATE TRIGGER {db.q(table + '_tr')} AFTER UPDATE ON {db.table(table)} FOR EACH ROW "
                   f"SET @{table}_slept = SLEEP(10)")
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"label": val("one")}, {"label": "a"})])
        tab = app.tab(pid, db.database)
        rid = tab.submit_apply(preview.plan_id)

        def sleeping() -> bool:  # the process list shows the trigger's own statement, not the UPDATE
            found = db.rows("SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE STATE = 'User sleep' "
                            f"AND INFO LIKE {db.literal(f'%{table}_slept%')}")
            return found[0][0] > 0

        # Inside the AFTER trigger's SLEEP: the MyISAM row has been written already
        assert wait_until(sleeping, scaled(8)), "the UPDATE never reached its trigger"
        tab.worker.cancel(rid)
        done = tab.wait(rid)
        rows = db.rows(f"SELECT id, label FROM {db.table(table)} ORDER BY id")
    assert (done.outcome.outcome, done.outcome.failed_change, done.error.code) == ("unknown", "u:1", "cancelled")
    assert done.error.message == "Cancelled: this change may have taken effect (this table cannot roll back)"
    assert rows == [(1, "one"), (2, "b")] and not done.finished.in_transaction


@requires_mysql
def test_a_myisam_insert_whose_after_trigger_fails_is_unknown_every_time_it_is_committed(my_env):  # ruling G13 (c)
    db, app, pid = my_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id BIGINT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(100) NOT NULL) "
                              "ENGINE=MyISAM")
        audit = make("TABLE", "CREATE TABLE {t} (id INT PRIMARY KEY) ENGINE=InnoDB")
        db.execute(f"INSERT INTO {db.table(audit)} VALUES (1)")
        db.execute(f"CREATE TRIGGER {db.q(table + '_tr')} AFTER INSERT ON {db.table(table)} FOR EACH ROW "
                   f"INSERT INTO {db.table(audit)} VALUES (1)")
        tab = app.tab(pid, db.database)
        outcomes = []
        for _ in range(3):  # the user commits the kept insert again: each time one more row is really inserted
            preview = app.preview(pid, db.path(table), inserts=[insert("i:1", {"label": val("n")})])
            outcomes.append(tab.apply(preview.plan_id).outcome.outcome)
        rows = db.rows(f"SELECT label FROM {db.table(table)}")
    assert outcomes == ["unknown"] * 3  # never "rolled_back": the client keeps Commit off until a Refresh (R-05-6)
    assert rows == [("n",)] * 3


@requires_mssql
def test_a_trigger_that_commits_the_transaction_makes_the_outcome_unknown(ms_env):  # 3609, fix round 1 item 2
    db, app, pid = ms_env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        db.execute(f"CREATE TRIGGER {db.q(table + '_tr')} ON {db.table(table)} AFTER UPDATE AS COMMIT TRANSACTION")
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"label": val("one")}, {"label": "a"}),
            update("u:2", {"id": 2}, {"label": val("two")}, {"label": "b"})])
        done = app.tab(pid, db.database).apply(preview.plan_id)
        rows = db.rows(f"SELECT id, label FROM {db.table(table)} ORDER BY id")
    assert (done.outcome.outcome, done.outcome.failed_change, done.outcome.statement, done.error.code) == (
        "unknown", "u:1", 0, "3609")
    assert rows[:2] == [(1, "one"), (2, "b")] and not done.finished.in_transaction  # the trigger committed u:1
