"""Copy data's job (spec ⑥ §3.5.5, R-06-12, 06c Task 4) on the dev databases: append and replace in one transaction per
table, a key conflict rolled back, identities and their sequences, a trigger that skips rows, a table that cannot roll
back, MySQL's CREATE TABLE that stays, several tables stopping at the first failure, a Cancel, a failing source, a
query tab's own session, a plan run once, and History. Scratch tables `zz_06c_*`, dropped by exact name. Then every way
a table ends on a fake target (Task 4 review M-6), where each branch is reached for sure."""

import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from dbaguy_backend.adapters.base import CONNECTION_LOST, StatementError
from dbaguy_backend.adapters.columns import TableColumn
from dbaguy_backend.domain.execution import CopyDataCommand, ExecuteCommand
from dbaguy_backend.services.transfer_plans import PlannedColumn, PlannedTable, StoredTransfer
from dbaguy_backend.sessions.jobs.context import JOB_ROLLBACK_FAILED, SourceLease
from dbaguy_backend.sessions.jobs.kept_changes import KEPT, SHOW_WARNINGS
from dbaguy_backend.sessions.jobs.transfer import (
    CREATED_CHECK, CREATED_EMPTY, CREATED_KEPT, DELETE_STOPPED, DELETED_STAY, MAY_STAND, NOT_ROLLED_BACK, TRIGGER_ENDED,
    TransferSink,
)
from dbaguy_backend.sql.bulk_insert import InsertBatcher
from dbaguy_backend.sql.dialects import Dialect
from tests.data_support import DataApp, JobTab, Ran, dropping
from tests.edit_support import scratch_db
from tests.fake_db import FakeConnection, FakeResult
from tests.support import requires_mssql, requires_mysql, requires_pg
from tests.test_copy_framework import CopyEnv
from tests.test_job_framework import Env, outcome
from tests.test_sessions import request_events
from tests.transfer_support import profile, query_request, table_name, tables_request


@pytest.fixture
def app(tmp_path: Path):
    app = DataApp(tmp_path)
    yield app
    app.close()


def copy(app: DataApp, request, target_profile: UUID, database: str | None, *,
         source_session: str | None = None) -> Ran:
    plan = app.container.transfers.plan(request)
    assert plan.plan_id is not None, [p.message for t in plan.tables for p in t.problems] + [
        p.message for p in plan.problems]
    tab = app.job_session(target_profile, database)
    return tab.run(lambda rid: CopyDataCommand(request_id=rid, plan_id=plan.plan_id,
                                               source_session_id=source_session))


@requires_pg
def test_append_adds_rows_and_replace_takes_the_tables_place(app: DataApp, tmp_path: Path) -> None:
    lite, pg = scratch_db("sqlite", tmp_path), scratch_db("postgres", tmp_path)
    try:
        with dropping(pg, before_drop=app.close_sessions) as made:
            source, target = table_name("s"), table_name("t")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY, label TEXT)",
                         f"INSERT INTO {lite.table(source)} VALUES (1, 'a'), (2, 'b')")
            pg.execute(f"CREATE TABLE {pg.table(target)} (id int PRIMARY KEY, label text)",
                       f"INSERT INTO {pg.table(target)} VALUES (10, 'old')")
            made.append(("TABLE", target))
            src, dst = profile(app, lite, "lite"), profile(app, pg, "pg")
            appended = copy(app, tables_request(lite, src, [source], pg, dst, "append", table=target), dst, "dbaguy")
            after_append = pg.rows(f"SELECT id, label FROM {pg.table(target)} ORDER BY id")
            replaced = copy(app, tables_request(lite, src, [source], pg, dst, "replace_rows", table=target), dst,
                            "dbaguy")
            after_replace = pg.rows(f"SELECT id, label FROM {pg.table(target)} ORDER BY id")
        assert (appended.outcome.outcome, appended.outcome.rows) == ("completed", 2)
        assert after_append == [(1, "a"), (2, "b"), (10, "old")]
        assert replaced.outcome.outcome == "completed" and after_replace == [(1, "a"), (2, "b")]
        records = app.history()
        assert [(entry.status, entry.sql.splitlines()[0]) for entry in records][:1] == [
            ("ok", f"-- Copy data: lite main.{source} → pg public.{target} (replace_rows)")]
        assert f"DELETE FROM public.{target}" in records[0].sql
    finally:
        lite.close()
        pg.close()


@requires_pg
def test_a_key_conflict_rolls_the_table_back_and_says_so(app: DataApp, tmp_path: Path) -> None:
    lite, pg = scratch_db("sqlite", tmp_path), scratch_db("postgres", tmp_path)
    try:
        with dropping(pg, before_drop=app.close_sessions) as made:
            source, target = table_name("s"), table_name("t")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY, label TEXT)",
                         f"INSERT INTO {lite.table(source)} VALUES (1, 'a'), (10, 'clash')")
            pg.execute(f"CREATE TABLE {pg.table(target)} (id int PRIMARY KEY, label text)",
                       f"INSERT INTO {pg.table(target)} VALUES (10, 'old')")
            made.append(("TABLE", target))
            src, dst = profile(app, lite, "lite"), profile(app, pg, "pg")
            ran = copy(app, tables_request(lite, src, [source], pg, dst, "append", table=target), dst, "dbaguy")
            rows = pg.rows(f"SELECT id, label FROM {pg.table(target)} ORDER BY id")
        assert (ran.outcome.outcome, ran.outcome.objects[0].outcome, ran.error.code) == (
            "rolled_back", "rolled_back", "23505")
        assert rows == [(10, "old")]
        assert app.history()[0].status == "error" and app.history()[0].error.startswith("Rolled back")
    finally:
        lite.close()
        pg.close()


@requires_pg
@requires_mssql
def test_kept_identities_move_their_sequence_on_and_generated_ones_are_new(app: DataApp, tmp_path: Path) -> None:
    lite, pg, ms = scratch_db("sqlite", tmp_path), scratch_db("postgres", tmp_path), scratch_db("mssql", tmp_path)
    try:
        with dropping(pg, before_drop=app.close_sessions) as pg_made, dropping(ms) as ms_made:
            source, into_pg, into_ms = table_name("s"), table_name("p"), table_name("m")
            # A numeric column with a default owns no sequence, and its setval would not resolve (pre-flight H1)
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY, label TEXT, amount NUMERIC)",
                         f"INSERT INTO {lite.table(source)} VALUES (5, 'a', 1.5), (40, 'b', 2)")
            pg.execute(f"CREATE TABLE {pg.table(into_pg)} (id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                       "label text, amount numeric(12,2) DEFAULT 0)")
            ms.execute(f"CREATE TABLE {ms.table(into_ms)} (id int IDENTITY(1,1) PRIMARY KEY, label nvarchar(10))")
            pg_made.append(("TABLE", into_pg))
            ms_made.append(("TABLE", into_ms))
            src, to_pg, to_ms = profile(app, lite, "lite"), profile(app, pg, "pg"), profile(app, ms, "ms")
            key = f"main.{source}"
            kept = copy(app, tables_request(lite, src, [source], pg, to_pg, "append", table=into_pg), to_pg, "dbaguy")
            pg.execute(f"INSERT INTO {pg.table(into_pg)} (label) VALUES ('next')")
            kept_ms = copy(app, tables_request(lite, src, [source], ms, to_ms, "append", table=into_ms), to_ms,
                           "dbaguy")
            ms.execute(f"INSERT INTO {ms.table(into_ms)} (label) VALUES (N'next')")
            generated = copy(app, tables_request(lite, src, [source], ms, to_ms, "append", table=into_ms,
                                                 identity={key: {"id": "generate"}}), to_ms, "dbaguy")
            pg_rows = pg.rows(f"SELECT id, label FROM {pg.table(into_pg)} ORDER BY id")
            ms_rows = ms.rows(f"SELECT id, label FROM {ms.table(into_ms)} ORDER BY id")
        assert [ran.outcome.outcome for ran in (kept, kept_ms, generated)] == ["completed"] * 3
        assert pg_rows == [(5, "a"), (40, "b"), (41, "next")]  # setval moved the identity past 40 (A P2)
        assert ms_rows == [(5, "a"), (40, "b"), (41, "next"), (42, "a"), (43, "b")]
    finally:
        lite.close()
        pg.close()
        ms.close()


@requires_pg
def test_a_trigger_that_skips_rows_rolls_the_table_back(app: DataApp, tmp_path: Path) -> None:
    lite, pg = scratch_db("sqlite", tmp_path), scratch_db("postgres", tmp_path)
    try:
        with dropping(pg, before_drop=app.close_sessions) as made:
            source, target = table_name("s"), table_name("t")
            function = table_name("f")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY)",
                         f"INSERT INTO {lite.table(source)} VALUES (1), (2), (3)")
            pg.execute(f"CREATE TABLE {pg.table(target)} (id int PRIMARY KEY)",
                       f"CREATE FUNCTION {pg.table(function)}() RETURNS trigger LANGUAGE plpgsql AS "
                       "$$ BEGIN IF NEW.id = 2 THEN RETURN NULL; END IF; RETURN NEW; END $$",
                       f"CREATE TRIGGER skip BEFORE INSERT ON {pg.table(target)} FOR EACH ROW EXECUTE FUNCTION "
                       f"{pg.table(function)}()")
            made += [("FUNCTION", function), ("TABLE", target)]
            src, dst = profile(app, lite, "lite"), profile(app, pg, "pg")
            ran = copy(app, tables_request(lite, src, [source], pg, dst, "append", table=target), dst, "dbaguy")
            rows = pg.rows(f"SELECT count(*) FROM {pg.table(target)}")
        assert (ran.outcome.outcome, ran.error.code) == ("rolled_back", "row_count_mismatch")
        assert ran.error.message.startswith("An INSERT of 3 rows reported 2 rows") and rows == [(0,)]
    finally:
        lite.close()
        pg.close()


@requires_mysql
def test_a_table_that_cannot_roll_back_is_unknown_once_a_write_failed(app: DataApp, tmp_path: Path) -> None:
    lite, my = scratch_db("sqlite", tmp_path), scratch_db("mysql", tmp_path)
    try:
        with dropping(my, before_drop=app.close_sessions) as made:
            source, target = table_name("s"), table_name("t")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY)",
                         f"INSERT INTO {lite.table(source)} VALUES (1), (2), (3)")
            my.execute(f"CREATE TABLE {my.table(target)} (id int PRIMARY KEY) ENGINE=MyISAM",
                       f"INSERT INTO {my.table(target)} VALUES (3)")
            made.append(("TABLE", target))
            src, dst = profile(app, lite, "lite"), profile(app, my, "my")
            ran = copy(app, tables_request(lite, src, [source], my, dst, "append", table=target), dst, None)
            rows = my.rows(f"SELECT id FROM {my.table(target)} ORDER BY id")
        assert (ran.outcome.outcome, ran.error.code) == ("unknown", "1062")
        assert NOT_ROLLED_BACK.format(target) in ran.outcome.warnings
        assert rows == [(1,), (2,), (3,)]  # MyISAM kept the rows before the duplicate: what stays is not known
    finally:
        lite.close()
        my.close()


@requires_pg
@requires_mysql
def test_replacing_into_a_table_that_cannot_roll_back_is_unknown_once_its_delete_ran(app: DataApp,
                                                                                      tmp_path: Path) -> None:  # H3
    pg, my = scratch_db("postgres", tmp_path), scratch_db("mysql", tmp_path)
    try:
        with dropping(pg) as pg_made, dropping(my, before_drop=app.close_sessions) as my_made:
            source, target = table_name("s"), table_name("t")
            pg.execute(f"CREATE TABLE {pg.table(source)} (id int PRIMARY KEY, at timestamp)",
                       f"INSERT INTO {pg.table(source)} VALUES (1, 'infinity')")
            pg_made.append(("TABLE", source))
            my.execute(f"CREATE TABLE {my.table(target)} (id int PRIMARY KEY, at datetime(6)) ENGINE=MyISAM",
                       f"INSERT INTO {my.table(target)} VALUES (7, '2020-01-01')")
            my_made.append(("TABLE", target))
            src, dst = profile(app, pg, "pg"), profile(app, my, "my")
            ran = copy(app, tables_request(pg, src, [source], my, dst, "replace_rows", table=target), dst, None)
            rows = my.rows(f"SELECT COUNT(*) FROM {my.table(target)}")
        assert (ran.outcome.outcome, ran.outcome.objects[0].outcome, ran.error.code) == (
            "unknown", "unknown", "value_not_copyable")
        assert DELETED_STAY.format(target) in ran.outcome.warnings and rows == [(0,)]  # its DELETE stayed
    finally:
        pg.close()
        my.close()


@requires_pg
@requires_mysql
def test_mysql_keeps_the_table_it_created_when_a_value_fails_its_row(app: DataApp, tmp_path: Path) -> None:
    pg, my = scratch_db("postgres", tmp_path), scratch_db("mysql", tmp_path)
    try:
        with dropping(pg) as pg_made, dropping(my, before_drop=app.close_sessions) as my_made:
            source, target = table_name("s"), table_name("t")
            pg.execute(f"CREATE TABLE {pg.table(source)} (id int PRIMARY KEY, at timestamp)",
                       f"INSERT INTO {pg.table(source)} VALUES (1, '2026-10-05'), (2, 'infinity')")
            pg_made.append(("TABLE", source))
            my_made.append(("TABLE", target))
            src, dst = profile(app, pg, "pg"), profile(app, my, "my")
            ran = copy(app, tables_request(pg, src, [source], my, dst, table=target), dst, None)
            rows = my.rows(f"SELECT COUNT(*) FROM {my.table(target)}")
        assert (ran.outcome.outcome, ran.error.code) == ("rolled_back", "value_not_copyable")
        assert ran.error.message == (f"Row 2 of public.{source}, column at: is 'infinity', which MySQL cannot hold")
        assert CREATED_EMPTY.format(target) in ran.outcome.warnings and rows == [(0,)]
    finally:
        pg.close()
        my.close()


def test_several_tables_stop_at_the_first_that_fails(app: DataApp, tmp_path: Path) -> None:
    (tmp_path / "s").mkdir()
    (tmp_path / "t").mkdir()
    source, target = scratch_db("sqlite", tmp_path / "s"), scratch_db("sqlite", tmp_path / "t")
    try:
        names = [table_name("a"), table_name("b"), table_name("c")]
        for name in names:
            source.execute(f"CREATE TABLE {source.table(name)} (id INTEGER PRIMARY KEY)",
                           f"INSERT INTO {source.table(name)} VALUES (1), (2)")
            target.execute(f"CREATE TABLE {target.table(name)} (id INTEGER PRIMARY KEY)")
        target.execute(f"INSERT INTO {target.table(names[1])} VALUES (2)")  # b's second row clashes
        src, dst = profile(app, source, "s"), profile(app, target, "t")
        ran = copy(app, tables_request(source, src, names, target, dst, "append"), dst, None)
        counts = [target.rows(f"SELECT COUNT(*) FROM {target.table(name)}")[0][0] for name in names]
        assert ran.outcome.outcome == "partial"
        assert [(item.outcome, item.rows) for item in ran.outcome.objects] == [
            ("copied", 2), ("rolled_back", 0), ("not_started", 0)]
        assert counts == [2, 1, 0] and ran.error.code == "SQLITE_CONSTRAINT_PRIMARYKEY"
    finally:
        source.close()
        target.close()


@requires_pg
def test_a_cancel_rolls_the_table_back(app: DataApp, tmp_path: Path) -> None:
    lite, pg = scratch_db("sqlite", tmp_path), scratch_db("postgres", tmp_path)
    try:
        with dropping(pg, before_drop=app.close_sessions) as made:
            source, target = table_name("s"), table_name("t")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY, label TEXT)",
                         f"WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 300000) "
                         f"INSERT INTO {lite.table(source)} SELECT n, 'row ' || n FROM s")
            made.append(("TABLE", target))
            src, dst = profile(app, lite, "lite"), profile(app, pg, "pg")
            plan = app.container.transfers.plan(tables_request(lite, src, [source], pg, dst, table=target))
            tab = app.job_session(dst, "dbaguy")
            rid = tab.submit(lambda r: CopyDataCommand(request_id=r, plan_id=plan.plan_id))
            tab.wait_for_progress(rid, lambda event: event.rows > 0)
            tab.worker.cancel(rid)
            ran = tab.wait(rid)
            exists = pg.rows(f"SELECT to_regclass('public.{target}') IS NOT NULL")
        assert (ran.outcome.outcome, ran.outcome.cancelled, ran.outcome.objects[0].outcome) == (
            "rolled_back", True, "rolled_back")
        assert exists == [(False,)]  # its CREATE TABLE was in the rolled-back transaction
    finally:
        lite.close()
        pg.close()


@requires_pg
def test_a_query_tabs_copy_reads_on_its_session_and_a_failing_read_is_the_sources(app: DataApp,
                                                                                   tmp_path: Path) -> None:
    pg, lite = scratch_db("postgres", tmp_path), scratch_db("sqlite", tmp_path)
    try:
        src, dst = profile(app, pg, "pg"), profile(app, lite, "lite")
        info = app.job_session(src, "dbaguy")
        tab = JobTab(info.worker, info.recorder, info.session_id)
        tab.run(lambda rid: ExecuteCommand(request_id=rid, sql="CREATE TEMP TABLE zz_06c_tmp AS SELECT 7 AS n"))
        columns = [{"name": "n", "type_name": "int4", "category": "number"}]
        ran = copy(app, query_request(pg, src, "SELECT n FROM zz_06c_tmp", columns, lite, dst, "zz_06c_q"), dst, None,
                   source_session=tab.session_id)
        failing = copy(app, query_request(pg, src, "SELECT 1 / (n - 500) AS n FROM generate_series(1, 1000) n",
                                          columns, lite, dst, "zz_06c_q2"), dst, None, source_session=tab.session_id)
        assert ran.outcome.outcome == "completed" and lite.rows("SELECT n FROM zz_06c_q") == [(7,)]
        assert (failing.outcome.outcome, failing.error.code) == ("rolled_back", "source_failed")
        assert failing.error.message.startswith("Reading Query 1 failed: division by zero")
        assert lite.rows("SELECT name FROM sqlite_master WHERE name = 'zz_06c_q2'") == []
    finally:
        pg.close()
        lite.close()


def test_a_plan_runs_once(app: DataApp, tmp_path: Path) -> None:
    (tmp_path / "s").mkdir()
    (tmp_path / "t").mkdir()
    source, target = scratch_db("sqlite", tmp_path / "s"), scratch_db("sqlite", tmp_path / "t")
    try:
        name = table_name()
        source.execute(f"CREATE TABLE {source.table(name)} (id INTEGER PRIMARY KEY)")
        src, dst = profile(app, source, "s"), profile(app, target, "t")
        plan = app.container.transfers.plan(tables_request(source, src, [name], target, dst))
        tab = app.job_session(dst)
        first = tab.run(lambda rid: CopyDataCommand(request_id=rid, plan_id=plan.plan_id))
        again = tab.run(lambda rid: CopyDataCommand(request_id=rid, plan_id=plan.plan_id))
        assert first.outcome.outcome == "completed"
        assert (again.outcome.outcome, again.error.code) == ("not_started", "plan_expired")
    finally:
        source.close()
        target.close()


def test_a_copy_command_whose_plan_is_gone_never_starts(app: DataApp, tmp_path: Path) -> None:
    lite = scratch_db("sqlite", tmp_path)
    try:
        tab = app.job_session(profile(app, lite, "lite"))
        ran = tab.run(lambda rid: CopyDataCommand(request_id=rid, plan_id="nothing"))
        assert (ran.outcome.outcome, ran.error.code) == ("not_started", "plan_expired")
    finally:
        lite.close()


# ---- 06c pre-flight H2, M1, M4, L-b, L-c; Task 3 review M-1; ⑤ I1 ----


@requires_pg
def test_a_failing_target_never_aborts_the_query_tabs_transaction(app: DataApp, tmp_path: Path) -> None:  # H2
    pg, lite = scratch_db("postgres", tmp_path), scratch_db("sqlite", tmp_path)
    try:
        src, dst = profile(app, pg, "pg"), profile(app, lite, "lite")
        info = app.job_session(src, "dbaguy")
        tab = JobTab(info.worker, info.recorder, info.session_id)
        tab.run(lambda rid: ExecuteCommand(request_id=rid, sql="BEGIN"))
        tab.run(lambda rid: ExecuteCommand(request_id=rid, sql="CREATE TEMP TABLE zz_06c_tmp AS "
                                                                "SELECT g AS n FROM generate_series(1, 20000) g"))
        lite.execute("CREATE TABLE zz_06c_into (n INTEGER PRIMARY KEY, s TEXT)",
                     "INSERT INTO zz_06c_into VALUES (3, 'x')")
        columns = [{"name": "n", "type_name": "int4", "category": "number"},
                   {"name": "s", "type_name": "text", "category": "text"}]
        slow = "SELECT n, CASE WHEN n % 50 = 0 THEN pg_sleep(0.02)::text END AS s FROM zz_06c_tmp ORDER BY n"
        ran = copy(app, query_request(pg, src, slow, columns, lite, dst, "zz_06c_into", "append"), dst, None,
                   source_session=tab.session_id)  # the third row clashes while the source reads its next chunk
        after = tab.run(lambda rid: ExecuteCommand(request_id=rid, sql="SELECT count(*) FROM zz_06c_tmp"))
        assert (ran.outcome.outcome, ran.error.code) == ("rolled_back", "SQLITE_CONSTRAINT_PRIMARYKEY")
        assert [event.rows for event in after.events if event.type == "rows"] == [[["20000"]]]  # the tab's work lives
    finally:
        pg.close()
        lite.close()


@requires_pg
def test_a_source_that_never_starts_ends_the_copy(app: DataApp, tmp_path: Path) -> None:  # M1: the helper never ran
    pg, lite = scratch_db("postgres", tmp_path), scratch_db("sqlite", tmp_path)
    try:
        src, dst = profile(app, pg, "pg"), profile(app, lite, "lite")
        info = app.job_session(src, "dbaguy")
        tab = JobTab(info.worker, info.recorder, info.session_id)
        columns = [{"name": "n", "type_name": "int4", "category": "number"}]
        plan = app.container.transfers.plan(query_request(pg, src, "SELECT 1 AS n", columns, lite, dst, "zz_06c_q"))
        tab.submit(lambda rid: ExecuteCommand(request_id=rid, sql="SELECT pg_sleep(2)"))  # the tab is busy
        time.sleep(0.3)
        target = app.job_session(dst)
        rid = target.submit(lambda r: CopyDataCommand(request_id=r, plan_id=plan.plan_id,
                                                      source_session_id=tab.session_id))
        time.sleep(0.5)  # its source waits in the tab's queue
        threading.Thread(target=app.container.sessions.close, args=(info.session_id,), daemon=True).start()
        ran = target.wait(rid, timeout=15)
        assert (ran.outcome.outcome, ran.error.code) == ("rolled_back", "source_failed")
    finally:
        pg.close()
        lite.close()


@requires_pg
def test_a_cancelled_copy_withdraws_its_read_still_queued_on_the_tab(app: DataApp, tmp_path: Path) -> None:  # M-1
    pg, lite = scratch_db("postgres", tmp_path), scratch_db("sqlite", tmp_path)
    try:
        src, dst = profile(app, pg, "pg"), profile(app, lite, "lite")
        tab = app.job_session(src, "dbaguy")
        columns = [{"name": "n", "type_name": "int4", "category": "number"}]
        plan = app.container.transfers.plan(query_request(pg, src, "SELECT 1 AS n", columns, lite, dst, "zz_06c_q"))
        busy = tab.submit(lambda rid: ExecuteCommand(request_id=rid, sql="SELECT pg_sleep(1.5)"))
        time.sleep(0.3)
        target = app.job_session(dst)
        rid = target.submit(lambda r: CopyDataCommand(request_id=r, plan_id=plan.plan_id,
                                                      source_session_id=tab.session_id))
        time.sleep(0.4)  # its read waits behind the tab's own query
        target.worker.cancel(rid)
        ran = target.wait(rid, timeout=15)
        read = request_events(tab.recorder, f"{rid}:source:1", 15)
        tabs_own = request_events(tab.recorder, busy, 15)
        assert (ran.outcome.outcome, ran.outcome.cancelled) == ("rolled_back", True)
        assert [event.outcome for event in read if event.type == "job_outcome"] == ["not_started"]
        assert "Query cancelled before it started" in [event.text for event in read if event.type == "message"]
        assert not tabs_own[-1].cancelled  # the tab's query ran to its end: never cancelled for the copy (H2)
    finally:
        pg.close()
        lite.close()


@requires_mssql
def test_a_batch_with_more_than_one_result_is_never_copied(app: DataApp, tmp_path: Path) -> None:  # M4
    ms, lite = scratch_db("mssql", tmp_path), scratch_db("sqlite", tmp_path)
    try:
        src, dst = profile(app, ms, "ms"), profile(app, lite, "lite")
        info = app.job_session(src, "dbaguy")
        columns = [{"name": "a", "type_name": "int", "category": "number"}]
        ran = copy(app, query_request(ms, src, "SELECT 1 AS a; SELECT 2 AS a", columns, lite, dst, "zz_06c_q"), dst,
                   None, source_session=info.session_id)
        assert (ran.outcome.outcome, ran.error.code) == ("rolled_back", "source_failed")
        assert lite.rows("SELECT name FROM sqlite_master WHERE name = 'zz_06c_q'") == []
    finally:
        ms.close()
        lite.close()


def test_a_profile_made_read_only_during_a_copy_starts_no_next_table(app: DataApp, tmp_path: Path) -> None:  # L-b
    (tmp_path / "s").mkdir()
    (tmp_path / "t").mkdir()
    source, target = scratch_db("sqlite", tmp_path / "s"), scratch_db("sqlite", tmp_path / "t")
    try:
        names = [table_name("a"), table_name("b")]
        for name in names:
            source.execute(f"CREATE TABLE {source.table(name)} (id INTEGER PRIMARY KEY)",
                           f"INSERT INTO {source.table(name)} VALUES (1), (2)")
        src, dst = profile(app, source, "s"), profile(app, target, "t")
        plan = app.container.transfers.plan(tables_request(source, src, names, target, dst))
        tab = app.job_session(dst)
        answers = iter([False])  # writable when the copy starts, read-only from its second table on
        hooks = tab.worker._hooks
        tab.worker.configure(replace(hooks, jobs=replace(hooks.jobs, read_only=lambda: next(answers, True))))
        ran = tab.run(lambda rid: CopyDataCommand(request_id=rid, plan_id=plan.plan_id))
        made = target.rows("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        assert (ran.outcome.outcome, ran.error.code) == ("partial", "read_only")
        assert [(item.outcome, item.rows) for item in ran.outcome.objects] == [("copied", 2), ("not_started", 0)]
        assert made == [(names[0],)]  # the second table's CREATE TABLE never went out
    finally:
        source.close()
        target.close()


def test_a_target_table_dropped_since_the_preview_rolls_its_table_back(app: DataApp, tmp_path: Path) -> None:  # L-c
    (tmp_path / "s").mkdir()
    (tmp_path / "t").mkdir()
    source, target = scratch_db("sqlite", tmp_path / "s"), scratch_db("sqlite", tmp_path / "t")
    try:
        name = table_name()
        source.execute(f"CREATE TABLE {source.table(name)} (id INTEGER PRIMARY KEY)",
                       f"INSERT INTO {source.table(name)} VALUES (1)")
        target.execute(f"CREATE TABLE {target.table(name)} (id INTEGER PRIMARY KEY)")
        src, dst = profile(app, source, "s"), profile(app, target, "t")
        plan = app.container.transfers.plan(tables_request(source, src, [name], target, dst, "append"))
        target.execute(f"DROP TABLE {target.table(name)}")
        ran = app.job_session(dst).run(lambda rid: CopyDataCommand(request_id=rid, plan_id=plan.plan_id))
        assert (ran.outcome.outcome, ran.outcome.objects[0].outcome, ran.error.code) == (
            "rolled_back", "rolled_back", "columns_changed")
    finally:
        source.close()
        target.close()


def test_a_sqlite_tables_own_conflict_clause_never_replaces_a_row(app: DataApp, tmp_path: Path) -> None:  # ⑤ I1
    (tmp_path / "s").mkdir()
    (tmp_path / "t").mkdir()
    source, target = scratch_db("sqlite", tmp_path / "s"), scratch_db("sqlite", tmp_path / "t")
    try:
        name = table_name()
        source.execute(f"CREATE TABLE {source.table(name)} (id INTEGER PRIMARY KEY, label TEXT)",
                       f"INSERT INTO {source.table(name)} VALUES (1, 'a'), (2, 'b')")
        target.execute(f"CREATE TABLE {target.table(name)} (id INTEGER PRIMARY KEY ON CONFLICT REPLACE, label TEXT)",
                       f"INSERT INTO {target.table(name)} VALUES (2, 'old')")
        src, dst = profile(app, source, "s"), profile(app, target, "t")
        ran = copy(app, tables_request(source, src, [name], target, dst, "append"), dst, None)
        rows = target.rows(f"SELECT id, label FROM {target.table(name)} ORDER BY id")
        assert (ran.outcome.outcome, ran.error.code) == ("rolled_back", "SQLITE_CONSTRAINT_PRIMARYKEY")
        assert rows == [(2, "old")]  # INSERT OR ABORT: the table's REPLACE never deleted the row it holds
    finally:
        source.close()
        target.close()


def test_a_copys_insert_aborts_on_a_conflict_and_says_how_many_rows_it_holds() -> None:
    column = TableColumn(name="id", type_name="integer", family="int64", nullable=False, has_default=False,
                         kind="number", compare="equals", writable=True)
    lite = InsertBatcher(Dialect.SQLITE, '"t"', [column], max_rows=2, abort_on_conflict=True)
    assert lite.add([1]) is None and lite.add([2]) is None
    first = lite.add([3])
    assert (first, lite.flushed_rows) == ('INSERT OR ABORT INTO "t" (id) VALUES\n(1),\n(2)', 2)
    assert (lite.flush(), lite.flushed_rows) == ('INSERT OR ABORT INTO "t" (id) VALUES\n(3)', 1)
    dumped = InsertBatcher(Dialect.SQLITE, '"t"', [column])  # a dump's: the restore's own conflict handling
    dumped.add([1])
    assert dumped.flush() == 'INSERT INTO "t" (id) VALUES\n(1)'
    pg = InsertBatcher(Dialect.POSTGRES, '"t"', [column], abort_on_conflict=True)
    pg.add([1])
    assert pg.flush() == 'INSERT INTO "t" (id) VALUES\n(1)'


# ---- 06c Task 4 review I-1: a MySQL table that cannot roll back, whatever the plan said ----


@requires_pg
@requires_mysql
def test_a_table_created_with_an_engine_that_cannot_roll_back_keeps_its_rows_and_says_so(app: DataApp,
                                                                                          tmp_path: Path) -> None:
    pg, my = scratch_db("postgres", tmp_path), scratch_db("mysql", tmp_path)
    try:
        with dropping(pg) as pg_made, dropping(my, before_drop=app.close_sessions) as my_made:
            source, target = table_name("s"), table_name("t")
            pg.execute(f"CREATE TABLE {pg.table(source)} (id int PRIMARY KEY, at timestamp)",
                       f"INSERT INTO {pg.table(source)} SELECT g, CASE WHEN g = 1200 THEN 'infinity'::timestamp "
                       "ELSE '2026-10-05'::timestamp END FROM generate_series(1, 1500) g")
            pg_made.append(("TABLE", source))
            my_made.append(("TABLE", target))
            src, dst = profile(app, pg, "pg"), profile(app, my, "my")
            plan = app.container.transfers.plan(tables_request(pg, src, [source], my, dst, table=target))
            tab = app.job_session(dst)
            # The server's default engine for its CREATE TABLE (a server configured so, as some are)
            tab.run(lambda rid: ExecuteCommand(request_id=rid, sql="SET SESSION default_storage_engine = MyISAM"))
            ran = tab.run(lambda rid: CopyDataCommand(request_id=rid, plan_id=plan.plan_id))
            rows = my.rows(f"SELECT COUNT(*) FROM {my.table(target)}")
            engine = my.rows("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND "
                             f"TABLE_NAME = '{target}'")
        assert (ran.outcome.outcome, ran.outcome.objects[0].outcome, ran.error.code) == (
            "unknown", "unknown", "value_not_copyable")  # the first INSERT of 1,000 rows went out; row 1,200 failed
        assert ran.outcome.warnings[:2] == [CREATED_KEPT.format(target), MAY_STAND.format(1000, target)]
        assert not [said for said in ran.outcome.warnings if "empty" in said or "rolled back" in said]
        assert (engine, rows) == ([("MyISAM",)], [(1000,)])  # what the outcome says may stand, stands
    finally:
        pg.close()
        my.close()


@requires_mysql
def test_an_append_into_a_table_that_cannot_roll_back_any_more_is_unknown(app: DataApp, tmp_path: Path) -> None:
    lite, my = scratch_db("sqlite", tmp_path), scratch_db("mysql", tmp_path)
    try:
        with dropping(my, before_drop=app.close_sessions) as made:
            source, target = table_name("s"), table_name("t")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY)",
                         f"INSERT INTO {lite.table(source)} VALUES (1), (2), (3)")
            my.execute(f"CREATE TABLE {my.table(target)} (id int PRIMARY KEY) ENGINE=InnoDB",
                       f"INSERT INTO {my.table(target)} VALUES (3)")
            made.append(("TABLE", target))
            src, dst = profile(app, lite, "lite"), profile(app, my, "my")
            plan = app.container.transfers.plan(tables_request(lite, src, [source], my, dst, "append", table=target))
            my.execute(f"ALTER TABLE {my.table(target)} ENGINE=MyISAM")  # after the preview, which said InnoDB
            ran = app.job_session(dst).run(lambda rid: CopyDataCommand(request_id=rid, plan_id=plan.plan_id))
            rows = my.rows(f"SELECT id FROM {my.table(target)} ORDER BY id")
        assert (ran.outcome.outcome, ran.error.code) == ("unknown", "1062")
        assert NOT_ROLLED_BACK.format(target) in ran.outcome.warnings  # its engine read when the table started
        assert rows == [(1,), (2,), (3,)]
    finally:
        lite.close()
        my.close()


@requires_mysql
def test_a_rollback_that_left_a_triggers_changes_is_unknown(app: DataApp, tmp_path: Path) -> None:  # 1196, R3
    lite, my = scratch_db("sqlite", tmp_path), scratch_db("mysql", tmp_path)
    try:
        with dropping(my, before_drop=app.close_sessions) as made:
            source, target, log, trigger = table_name("s"), table_name("t"), table_name("l"), table_name("g")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY)",
                         f"WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 1001) "
                         f"INSERT INTO {lite.table(source)} SELECT n FROM s")
            my.execute(f"CREATE TABLE {my.table(log)} (id int) ENGINE=MyISAM",
                       f"CREATE TABLE {my.table(target)} (id int PRIMARY KEY) ENGINE=InnoDB",
                       f"INSERT INTO {my.table(target)} VALUES (1001)",
                       f"CREATE TRIGGER {my.table(trigger)} AFTER INSERT ON {my.table(target)} FOR EACH ROW "
                       f"INSERT INTO {my.table(log)} VALUES (NEW.id)")
            made += [("TABLE", log), ("TABLE", target)]  # the trigger goes with its table
            src, dst = profile(app, lite, "lite"), profile(app, my, "my")
            # The first INSERT (1,000 rows) goes in and its trigger writes the MyISAM log; the second meets 1001
            ran = copy(app, tables_request(lite, src, [source], my, dst, "append", table=target), dst, None)
            counts = [my.rows(f"SELECT COUNT(*) FROM {my.table(name)}")[0][0] for name in (target, log)]
        assert (ran.outcome.outcome, ran.error.code) == ("unknown", "1062")
        assert KEPT in ran.outcome.warnings and counts == [1, 1000]  # the table rolled back; the trigger's rows stayed
    finally:
        lite.close()
        my.close()


# ---- every way a table ends, on a fake target (Task 4 review M-6) ----


def _number(name: str) -> TableColumn:
    return TableColumn(name=name, type_name="int", family="int64", nullable=True, has_default=False, kind="number",
                       compare="equals", writable=True)


class _Answer(FakeResult):
    """A statement's answer on the fakes: its rows as they are, and the rows it matched."""

    def __init__(self, rows: list[list] | None = None, matched: int | None = None) -> None:
        super().__init__(0)
        self._answer = list(rows or [])
        self._matched = matched

    def fetch(self, n: int) -> list[list]:
        taken, self._answer = self._answer[:n], self._answer[n:]
        return taken

    def matched_rows(self) -> int | None:
        return self._matched


class _Target(FakeConnection):
    """A copy's target: BEGIN opens a transaction and an INSERT matches its rows. `fail`: a statement's first word →
    what it raises (a StatementError, or a function of the connection that returns one); `commit`: what COMMIT does
    (ok, lost, left_open, crash); `engine`: whether MySQL says the table rolls back (YES, NO); `warnings`: what SHOW
    WARNINGS holds; `ends`: an INSERT ends the transaction; `rollback_fails`; `check_crashes`: the check before COMMIT
    raises a bug's exception once; `hold`: COMMIT waits (reached, go)."""

    def __init__(self, *, fail: dict | None = None, commit: str = "ok", engine: str = "YES", warnings: tuple = (),
                 ends: bool = False, rollback_fails: bool = False, check_crashes: bool = False,
                 hold: tuple[threading.Event, threading.Event] | None = None) -> None:
        super().__init__()
        self.fail = fail or {}
        self.commit_does = commit
        self.engine = engine
        self.warnings = warnings
        self.ends = ends
        self.rollback_fails = rollback_fails
        self.check_crashes = check_crashes
        self.hold = hold

    def table_columns(self, path: list) -> list[TableColumn]:
        return [_number("n")]

    def statement(self, sql: str, options) -> _Answer:
        self.runs.append(sql)
        word = sql.split()[0].upper()
        if word in self.fail:
            failure = self.fail[word]
            raise failure(self) if callable(failure) else failure
        if "BEGIN" in sql or sql.startswith("START TRANSACTION"):
            self.tx = True
        elif word == "INSERT":
            if self.ends:
                self.tx = False
            return _Answer(matched=sql.count("\n("))
        elif word == "COMMIT":
            return self._commit()
        elif sql == SHOW_WARNINGS:
            return _Answer([list(warning) for warning in self.warnings])
        elif "information_schema.TABLES" in sql:
            return _Answer([[self.engine]])
        return _Answer()

    def _commit(self) -> _Answer:
        if self.hold is not None:
            reached, go = self.hold
            reached.set()
            assert go.wait(5)
        if self.commit_does == "lost":
            self.tx = False
            raise StatementError("Connection to the server was lost", code=CONNECTION_LOST)
        if self.commit_does == "crash":
            raise RuntimeError("a driver bug")
        if self.commit_does != "left_open":
            self.tx = False
        return _Answer()

    def transaction_failed(self) -> bool:
        if self.check_crashes and self.tx:
            self.check_crashes = False
            raise RuntimeError("a driver bug")
        return super().transaction_failed()

    def rollback(self) -> None:
        if self.rollback_fails:
            raise StatementError("The rollback failed", code="2013")
        super().rollback()


def _lost(target: _Target) -> StatementError:
    target.tx = False  # the server ended the transaction with the connection
    return StatementError("Connection to the server was lost", code=CONNECTION_LOST)


def _trigger_commit(target: _Target) -> StatementError:
    target.tx = False
    return StatementError("The transaction ended in the trigger", code="3609")


def _planned(name: str, **fields) -> PlannedTable:
    return PlannedTable(**{
        "source_name": f"s.{name}", "target_name": name, "target_schema": None, "target_table": name,
        "target_path": (), "select": "rows 3", "columns": (_number("n"),), "written": (PlannedColumn(0, "n"),),
        "before": (), "after": (), "created": False, **fields})


class _Copying:
    """A copy on fakes: the target's worker runs TransferSink on `target`; its source is another worker whose `rows 3`
    gives three rows."""

    def __init__(self, target: _Target, dialect: Dialect, *tables: PlannedTable) -> None:
        self.target = target
        self.plan = StoredTransfer(plan_id="p", profile_id=uuid4(), database=None, source_profile_id=uuid4(),
                                   source_database=None, query=False, tables=tables, summary="-- Copy data",
                                   title="Copy")
        self.source = CopyEnv()
        self.sink = Env(conn=target)
        hooks = self.sink.worker._hooks
        jobs = replace(hooks.jobs, build=lambda command: TransferSink(command, self.plan, dialect),
                       source=lambda *_: SourceLease(self.source.worker, lambda: None))
        self.sink.worker.configure(replace(hooks, dialect=dialect, jobs=jobs))

    def run(self) -> list:
        self.submit()
        return self.events()

    def submit(self) -> None:
        self.sink.worker.submit(CopyDataCommand(request_id="c", plan_id="p"))

    def events(self) -> list:
        return request_events(self.sink.recorder, "c")

    def close(self) -> None:
        self.sink.worker.close()
        self.source.worker.close()


@pytest.fixture
def fakes():
    made: list[_Copying] = []

    def make(target: _Target, dialect: Dialect = Dialect.SQLITE, *tables: PlannedTable) -> _Copying:
        made.append(_Copying(target, dialect, *(tables or (_planned("t"),))))
        return made[-1]

    yield make
    for each in made:
        each.close()


def _ended(events: list) -> tuple:
    done = outcome(events)
    return done.outcome, [item.outcome for item in done.objects], done.error.code if done.error else None


def test_a_commit_whose_answer_was_lost_is_unknown_and_says_what_may_stand(fakes) -> None:
    events = fakes(_Target(commit="lost")).run()
    assert _ended(events) == ("unknown", ["unknown"], CONNECTION_LOST)
    assert outcome(events).warnings == [MAY_STAND.format(3, "t")]


def test_a_commit_that_left_a_transaction_open_is_rolled_back_and_unknown(fakes) -> None:
    copying = fakes(_Target(commit="left_open"))
    events = copying.run()
    assert _ended(events) == ("unknown", ["unknown"], "commit_unconfirmed")
    assert (copying.target.rollbacks, copying.target.tx) == (1, False)


def test_a_transaction_that_ended_before_its_commit_is_unknown(fakes) -> None:
    copying = fakes(_Target(ends=True))
    events = copying.run()
    assert _ended(events) == ("unknown", ["unknown"], "transaction_ended")
    assert not [sql for sql in copying.target.runs if sql.startswith("COMMIT")]  # no COMMIT for it


def test_a_connection_lost_before_the_commit_rolled_the_table_back(fakes) -> None:  # the server ends it
    copying = fakes(_Target(fail={"INSERT": _lost}))
    events = copying.run()
    assert _ended(events) == ("rolled_back", ["rolled_back"], CONNECTION_LOST)
    assert copying.target.rollbacks == 0 and outcome(events).warnings == []


def test_a_triggers_commit_is_unknown(fakes) -> None:  # SQL Server 3609
    events = fakes(_Target(fail={"INSERT": _trigger_commit}), Dialect.TSQL).run()
    assert _ended(events) == ("unknown", ["unknown"], "3609")
    assert outcome(events).warnings == [TRIGGER_ENDED.format("t"), MAY_STAND.format(3, "t")]


def test_a_cancel_during_a_commit_stops_the_next_table_and_history_says_part_was_copied(fakes) -> None:
    reached, go = threading.Event(), threading.Event()
    copying = fakes(_Target(hold=(reached, go)), Dialect.SQLITE, _planned("a"), _planned("b"))
    copying.submit()
    assert reached.wait(5)
    copying.sink.worker.cancel("c")  # the COMMIT cannot be cancelled: held for the next table
    go.set()
    events = copying.events()
    assert _ended(events) == ("partial", ["copied", "not_started"], "cancelled") and outcome(events).cancelled
    assert [sql for sql in copying.target.runs if "BEGIN" in sql] == ["BEGIN IMMEDIATE"]
    [finished] = [record for record in copying.sink.records if record[0] == "finish"]
    assert finished[2:] == ("error", finished[3], 3, "Partly applied (cancelled): Query cancelled")  # review M-1


def test_a_cancel_before_a_tables_first_statement_leaves_it_not_started(fakes) -> None:  # review M-4
    copying = fakes(_Target(), Dialect.SQLITE, _planned("a"), _planned("b"))
    asked: list[int] = []

    def read_only() -> bool:  # the copy asks it right after the next table may be cancelled again
        asked.append(1)
        if len(asked) == 2:
            copying.sink.worker.cancel("c")
        return False

    hooks = copying.sink.worker._hooks
    copying.sink.worker.configure(replace(hooks, jobs=replace(hooks.jobs, read_only=read_only)))
    events = copying.run()
    assert _ended(events) == ("partial", ["copied", "not_started"], "cancelled")
    assert [sql for sql in copying.target.runs if "BEGIN" in sql] == ["BEGIN IMMEDIATE"]  # b's BEGIN never went out


def test_a_bug_around_the_commit_ends_its_table_not_the_copy(fakes) -> None:  # review M-3
    checked = fakes(_Target(check_crashes=True)).run()
    assert _ended(checked)[:2] == ("rolled_back", ["rolled_back"])
    assert outcome(checked).error.message.startswith("Internal error")
    committed = fakes(_Target(commit="crash")).run()  # the COMMIT may have gone out
    assert _ended(committed)[:2] == ("unknown", ["unknown"])
    assert outcome(committed).error.message.startswith("Internal error")


def _duplicate(target: _Target) -> StatementError:
    return StatementError("Duplicate entry", code="1062")


def test_a_mysql_table_is_what_the_server_says_when_it_starts(fakes) -> None:  # review I-1
    created = _planned("t", create_first="CREATE TABLE t (n int)", created=True)
    kept = fakes(_Target(fail={"INSERT": _duplicate}, engine="NO"), Dialect.MYSQL, created).run()
    assert _ended(kept) == ("unknown", ["unknown"], "1062")
    assert outcome(kept).warnings == [CREATED_KEPT.format("t"), MAY_STAND.format(3, "t")]
    warned = fakes(_Target(fail={"INSERT": _duplicate}, warnings=(("Warning", 1196, "…"),)), Dialect.MYSQL,
                   created).run()  # InnoDB, but its ROLLBACK left another table's changes (a trigger's: R3)
    assert _ended(warned) == ("unknown", ["unknown"], "1062")
    assert outcome(warned).warnings == [KEPT, CREATED_CHECK.format("t"), MAY_STAND.format(3, "t")]
    undone = fakes(_Target(fail={"INSERT": _duplicate}), Dialect.MYSQL, created).run()
    assert _ended(undone) == ("rolled_back", ["rolled_back"], "1062")
    assert outcome(undone).warnings == [CREATED_EMPTY.format("t")]  # empty: only once it rolled back


def test_a_delete_that_stopped_part_way_is_said_as_such(fakes) -> None:  # review M-5
    replacing = _planned("t", before=("DELETE FROM t",))
    stopped = fakes(_Target(fail={"DELETE": _duplicate}, engine="NO"), Dialect.MYSQL, replacing).run()
    assert outcome(stopped).outcome == "unknown" and outcome(stopped).warnings == [DELETE_STOPPED.format("t")]
    deleted = fakes(_Target(fail={"INSERT": _duplicate}, engine="NO"), Dialect.MYSQL, replacing).run()
    assert outcome(deleted).warnings == [DELETED_STAY.format("t"), MAY_STAND.format(3, "t")]


def test_a_rollback_that_failed_is_said_where_the_table_is_unknown(fakes) -> None:  # review M-10
    events = fakes(_Target(fail={"INSERT": _duplicate}, engine="NO", rollback_fails=True), Dialect.MYSQL).run()
    assert outcome(events).warnings == [NOT_ROLLED_BACK.format("t"), JOB_ROLLBACK_FAILED, MAY_STAND.format(3, "t")]
