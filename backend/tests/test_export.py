import csv
import json
import sqlite3
from uuid import uuid4

import pytest

from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.domain.execution import ExecuteCommand, ExportCommand
from dbaguy_backend.domain.formats import FormatKind
from dbaguy_backend.domain.profiles import SqliteProfile
from dbaguy_backend.services.safety import SafetyPolicy
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from dbaguy_backend.sql.dialects import Dialect
from tests.fake_db import FakeLease
from tests.test_sessions import Recorder

BIG = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 5000000) SELECT x, 'row ' || x FROM c"
# 20,000 rows, but row 15,000 raises "integer overflow": the export fails after its first progress event
FAILS_MID_WAY = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 20000) "
    "SELECT CASE WHEN x = 15000 THEN abs(-9223372036854775808) ELSE x END FROM c"
)


@pytest.fixture
def session(tmp_path):
    db = tmp_path / "e.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, note TEXT, data BLOB)")
    raw.executemany("INSERT INTO t (note, data) VALUES (?, ?)", [(f"n{i}", b"\x01\x02") for i in range(25_000)])
    raw.commit()
    raw.close()
    profile = SqliteProfile(id=uuid4(), name="e", engine="sqlite", path=str(db))
    # Opened by the worker thread itself: sqlite3 connections refuse use from another thread.
    worker = SessionWorker("export", lambda: FakeLease(SqliteAdapter().open(profile, None, None, None)))
    worker.start()
    policy = SafetyPolicy()
    worker.configure(WorkerHooks(
        dialect=Dialect.SQLITE,
        check=lambda sql, confirmed: policy.check(sql, Dialect.SQLITE, "standard", False, confirmed),
    ))
    recorder = Recorder()
    worker.set_emitter(recorder)
    yield worker, recorder, tmp_path
    worker.close()


def export(worker, recorder, rid, sql, path, fmt=FormatKind.CSV):
    worker.submit(ExportCommand(request_id=rid, sql=sql, format=fmt, path=str(path)))
    return recorder.wait_for(lambda e: e.type == "finished" and e.request_id == rid)


def test_export_csv_with_progress(session):
    worker, rec, tmp = session
    target = tmp / "out.csv"
    done = export(worker, rec, "x", "SELECT id, note, data FROM t ORDER BY id", target)
    assert not done.cancelled and [p.rows for p in rec.of("x", "progress")] == [10_000, 20_000]
    rows = list(csv.reader(target.open(encoding="utf-8", newline="")))
    assert rows[0] == ["id", "note", "data"] and rows[1] == ["1", "n0", "0x0102"] and len(rows) == 25_001
    assert "Exported 25,000 rows" in rec.of("x", "message")[-1].text
    assert not (tmp / "out.csv.part").exists()


@pytest.mark.parametrize(("sql", "fmt", "path", "code"), [
    ("DELETE FROM t", FormatKind.CSV, "out.csv", "export_requires_select"),
    ("SELECT 1", FormatKind.IN_LIST, "out.txt", "unsupported_format"),
    ("SELECT 1", FormatKind.CSV, "relative.csv", "invalid_path"),
    ("SELECT 1", FormatKind.CSV, "missing-dir/out.csv", "invalid_path"),
    ("SELECT 1", FormatKind.CSV, "", "invalid_path"),  # the folder itself, not a file in it
])
def test_export_rejections(session, monkeypatch, sql, fmt, path, code):
    worker, rec, tmp = session
    monkeypatch.chdir(tmp)  # a relative path wrongly accepted would land here
    target = path if path == "relative.csv" else tmp / path
    done = export(worker, rec, "r", sql, target, fmt)
    assert rec.of("r", "error")[0].code == code and not done.cancelled
    assert [p.name for p in tmp.iterdir()] == ["e.db"]  # nothing written, not even a .part
    assert len(rec.of("r", "finished")) == 1 and rec.of("r", "progress") == []


def test_cancel_removes_the_partial_file(session):
    worker, rec, tmp = session
    target = tmp / "big.jsonl"
    worker.submit(ExportCommand(request_id="c", sql=BIG, format=FormatKind.JSONL, path=str(target)))
    rec.wait_for(lambda e: e.type == "progress" and e.request_id == "c")
    worker.cancel("c")
    assert rec.wait_for(lambda e: e.type == "finished" and e.request_id == "c").cancelled
    assert not target.exists() and not (tmp / "big.jsonl.part").exists()


def test_export_ends_a_paused_result(session):
    worker, rec, tmp = session
    worker.submit(ExecuteCommand(request_id="grid", sql="SELECT * FROM t", page_size=10))
    rec.wait_for(lambda e: e.type == "result_set_end" and e.has_more)
    export(worker, rec, "x", "SELECT id FROM t", tmp / "ids.csv")
    assert rec.of("grid", "finished")


def test_a_failed_export_leaves_no_file_and_keeps_the_one_it_would_have_replaced(session):
    worker, rec, tmp = session
    target = tmp / "keep.csv"
    target.write_text("old", encoding="utf-8")
    done = export(worker, rec, "f", FAILS_MID_WAY, target)
    assert rec.of("f", "error")[0].message == "integer overflow" and not done.cancelled
    assert [p.rows for p in rec.of("f", "progress")] == [10_000]
    assert target.read_text(encoding="utf-8") == "old" and not (tmp / "keep.csv.part").exists()


def test_a_successful_export_replaces_the_existing_file(session):
    worker, rec, tmp = session
    target = tmp / "ids.csv"
    target.write_text("old", encoding="utf-8")
    export(worker, rec, "x", "SELECT 1 AS n UNION ALL SELECT 2", target)
    assert target.read_text(encoding="utf-8") == "n\n1\n2\n"


def test_export_finishes_the_document_exactly_once(session):
    worker, rec, tmp = session
    target = tmp / "ids.json"
    export(worker, rec, "x", "SELECT id, note FROM t ORDER BY id LIMIT 3", target, FormatKind.JSON)
    assert [r["note"] for r in json.loads(target.read_text(encoding="utf-8"))] == ["n0", "n1", "n2"]


def test_export_is_not_written_to_history(session):
    worker, rec, tmp = session
    records = []
    policy = SafetyPolicy()
    worker.configure(WorkerHooks(
        dialect=Dialect.SQLITE, on_record=records.append,
        check=lambda sql, confirmed: policy.check(sql, Dialect.SQLITE, "standard", False, confirmed),
    ))
    export(worker, rec, "x", "SELECT 1", tmp / "one.csv")
    worker.submit(ExecuteCommand(request_id="q", sql="SELECT 1"))
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "q")
    assert [r.request_id for r in records] == ["q"]
