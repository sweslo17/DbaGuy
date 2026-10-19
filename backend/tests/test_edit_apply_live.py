"""The apply where only a live server shows the truth (Task 8 fix round 1): a table that cannot roll back keeps what a
statement changed before it failed (ruling G13, MySQL MyISAM), and a SQL Server trigger can commit the apply's
transaction (3609). zz_05a_* objects only, dropped by exact name. The shared flows on every engine are in
test_edit_flows.py (Task 9)."""

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.edits import EditChanges, EditPreview
from dbaguy_backend.domain.execution import ApplyEditsCommand
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.sessions.manager import SessionCreateRequest
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.edit_support import MssqlScratch, MysqlScratch, ScratchDb, items_table, scratch
from tests.support import requires_mssql, requires_mysql, scaled, wait_until
from tests.test_sessions import Recorder, request_events


@dataclass
class Applied:
    events: list

    @property
    def outcome(self):
        [done] = [e for e in self.events if e.type == "edit_outcome"]
        return done

    @property
    def error(self):
        return next((e for e in self.events if e.type == "error"), None)

    @property
    def finished(self):
        return self.events[-1]


class Tab:
    def __init__(self, container: AppContainer, pid, database: str | None) -> None:
        self.sid = container.sessions.create(SessionCreateRequest(profile_id=pid, database=database)).session_id
        self.recorder = Recorder()
        self.worker = container.sessions.attach(self.sid, self.recorder)

    def submit(self, plan_id: str) -> str:
        rid = uuid4().hex[:8]
        self.worker.submit(ApplyEditsCommand(request_id=rid, plan_id=plan_id))
        return rid

    def wait(self, rid: str, timeout: float = 30.0) -> Applied:
        return Applied(request_events(self.recorder, rid, scaled(timeout)))

    def apply(self, plan_id: str) -> Applied:
        return self.wait(self.submit(plan_id))


class Rig:
    """The backend in-process over a temporary data dir (ruling E21): a preview through EditService, its apply through
    a session's worker with the real hooks and adapter."""

    def __init__(self, directory: Path, db: ScratchDb) -> None:
        self.db = db
        settings = Settings(token="t8", data_dir=directory / "data", log_to_file=False)
        self.container = AppContainer(settings, secret_store=InMemorySecretStore())
        self.pid = self.container.profiles.create(ProfileUpsertRequest.model_validate(db.profile())).profile.id
        self.tabs: list[Tab] = []

    def preview(self, table: str, **changes) -> EditPreview:
        preview = self.container.edits.preview(self.pid, self.db.path(table), EditChanges.model_validate(changes))
        assert preview.plan_id is not None, preview.problems
        return preview

    def tab(self) -> Tab:
        tab = Tab(self.container, self.pid, self.db.database)
        self.tabs.append(tab)
        return tab

    def close_tabs(self) -> None:  # before the scratch tables are dropped: an open session may hold a lock
        for tab in self.tabs:
            self.container.sessions.close(tab.sid)
        self.tabs = []

    def close(self) -> None:
        self.close_tabs()
        self.container.close()
        self.db.close()


def update(cid: str, key: int, old: str, new: str) -> dict:
    return {"id": cid, "key": {"id": key}, "set": {"label": {"kind": "value", "text": new}}, "original": {"label": old}}


@pytest.fixture
def my_rig(tmp_path):
    rig = Rig(tmp_path, MysqlScratch())
    yield rig
    rig.close()


@pytest.fixture
def ms_rig(tmp_path):
    rig = Rig(tmp_path, MssqlScratch())
    yield rig
    rig.close()


MYISAM = "CREATE TABLE {t} (id BIGINT PRIMARY KEY, label VARCHAR(100) NOT NULL) ENGINE=MyISAM"


@requires_mysql
def test_a_myisam_update_whose_after_trigger_fails_is_unknown_and_its_change_stays(my_rig):  # ruling G13 (a)
    db = my_rig.db
    with scratch(db, my_rig.close_tabs) as make:
        table = make("TABLE", MYISAM)
        audit = make("TABLE", "CREATE TABLE {t} (id INT PRIMARY KEY) ENGINE=InnoDB")
        db.execute(f"INSERT INTO {db.table(table)} VALUES (1, 'a'), (2, 'b')",
                   f"INSERT INTO {db.table(audit)} VALUES (1)")
        db.execute(f"CREATE TRIGGER {db.q(table + '_tr')} AFTER UPDATE ON {db.table(table)} FOR EACH ROW "
                   f"INSERT INTO {db.table(audit)} VALUES (1)")  # 1062 after the row has changed
        preview = my_rig.preview(table, updates=[update("u:1", 1, "a", "one"), update("u:2", 2, "b", "two")])
        assert [w.code for w in preview.warnings] == ["non_transactional"]
        done = my_rig.tab().apply(preview.plan_id)
        rows = db.rows(f"SELECT id, label FROM {db.table(table)} ORDER BY id")
    assert (done.outcome.outcome, done.outcome.applied, done.outcome.failed_change, done.error.code) == (
        "unknown", [], "u:1", "1062")
    assert rows == [(1, "one"), (2, "b")] and not done.finished.in_transaction  # what is true: u:1 took effect


@requires_mysql
def test_a_cancel_during_a_myisam_after_trigger_is_unknown_and_its_change_stays(my_rig):  # ruling G13 (b)
    db = my_rig.db
    with scratch(db, my_rig.close_tabs) as make:
        table = make("TABLE", MYISAM)
        db.execute(f"INSERT INTO {db.table(table)} VALUES (1, 'a'), (2, 'b')")
        db.execute(f"CREATE TRIGGER {db.q(table + '_tr')} AFTER UPDATE ON {db.table(table)} FOR EACH ROW "
                   f"SET @{table}_slept = SLEEP(10)")
        preview = my_rig.preview(table, updates=[update("u:1", 1, "a", "one")])
        tab = my_rig.tab()
        rid = tab.submit(preview.plan_id)

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
def test_a_myisam_insert_whose_after_trigger_fails_is_unknown_every_time_it_is_committed(my_rig):  # ruling G13 (c)
    db = my_rig.db
    with scratch(db, my_rig.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id BIGINT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(100) NOT NULL) "
                              "ENGINE=MyISAM")
        audit = make("TABLE", "CREATE TABLE {t} (id INT PRIMARY KEY) ENGINE=InnoDB")
        db.execute(f"INSERT INTO {db.table(audit)} VALUES (1)")
        db.execute(f"CREATE TRIGGER {db.q(table + '_tr')} AFTER INSERT ON {db.table(table)} FOR EACH ROW "
                   f"INSERT INTO {db.table(audit)} VALUES (1)")
        tab = my_rig.tab()
        outcomes = []
        for _ in range(3):  # the user commits the kept insert again: each time one more row is really inserted
            new_row = {"id": "i:1", "values": {"label": {"kind": "value", "text": "n"}}}
            preview = my_rig.preview(table, inserts=[new_row])
            outcomes.append(tab.apply(preview.plan_id).outcome.outcome)
        rows = db.rows(f"SELECT label FROM {db.table(table)}")
    assert outcomes == ["unknown"] * 3  # never "rolled_back": the client keeps Commit off until a Refresh (R-05-6)
    assert rows == [("n",)] * 3


@requires_mssql
def test_a_trigger_that_commits_the_transaction_makes_the_outcome_unknown(ms_rig):  # 3609, fix round 1 item 2
    db = ms_rig.db
    with scratch(db, ms_rig.close_tabs) as make:
        table = items_table(db, make)
        db.execute(f"CREATE TRIGGER {db.q(table + '_tr')} ON {db.table(table)} AFTER UPDATE AS COMMIT TRANSACTION")
        preview = ms_rig.preview(table, updates=[update("u:1", 1, "a", "one"), update("u:2", 2, "b", "two")])
        done = ms_rig.tab().apply(preview.plan_id)
        rows = db.rows(f"SELECT id, label FROM {db.table(table)} ORDER BY id")
    assert (done.outcome.outcome, done.outcome.failed_change, done.outcome.statement, done.error.code) == (
        "unknown", "u:1", 0, "3609")
    assert rows[:2] == [(1, "one"), (2, "b")] and not done.finished.in_transaction  # the trigger committed u:1
