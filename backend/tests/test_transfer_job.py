"""Copy data's job (spec ⑥ §3.5.5, R-06-12, 06c Task 4) on the dev databases: append and replace in one transaction per
table, a key conflict rolled back, identities and their sequences, a trigger that skips rows, a table that cannot roll
back, MySQL's CREATE TABLE that stays, several tables stopping at the first failure, a Cancel, a failing source, a
query tab's own session, a plan run once, and History. Scratch tables `zz_06c_*`, dropped by exact name."""

import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from dbaguy_backend.adapters.columns import TableColumn
from dbaguy_backend.domain.execution import CopyDataCommand, ExecuteCommand
from dbaguy_backend.sessions.jobs.transfer import CREATED_EMPTY, DELETED_STAY, NOT_ROLLED_BACK
from dbaguy_backend.sql.bulk_insert import InsertBatcher
from dbaguy_backend.sql.dialects import Dialect
from tests.data_support import DataApp, JobTab, Ran, dropping
from tests.edit_support import scratch_db
from tests.support import requires_mssql, requires_mysql, requires_pg
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
