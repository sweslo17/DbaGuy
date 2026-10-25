import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from dbaguy_backend.domain.errors import StorageError
from dbaguy_backend.domain.history import HistoryRecord
from dbaguy_backend.services.history_service import HistoryService

PID = uuid4()


def rec(sql, status="ok", profile=PID, error=None):
    return HistoryRecord(profile_id=profile, profile_name="p", database=None, sql=sql, started_at=datetime.now(UTC),
                         elapsed_ms=5, rows=1, status=status, error=error)


def sqls(page):
    return [e.sql for e in page.items]


def test_search(tmp_path):
    history = HistoryService(tmp_path / "h.db")
    for sql in ["SELECT * FROM customers", "select email from users", "UPDATE orders SET x = 1", "SELECT 1"]:
        history.record(rec(sql))
    assert sqls(history.search("CUSTOM")) == ["SELECT * FROM customers"]  # trigram, case-insensitive
    assert sqls(history.search("1")) == ["SELECT 1", "UPDATE orders SET x = 1"]  # short: LIKE, newest first
    assert sqls(history.search("%")) == []  # LIKE wildcards are literal
    assert sqls(history.search('a"b"c')) == []  # quotes cannot break the FTS query
    assert sqls(history.search())[0] == "SELECT 1"
    history.close()


def test_paging_profile_filter_and_truncation(tmp_path):
    history = HistoryService(tmp_path / "h.db")
    for i in range(5):
        history.record(rec(f"SELECT {i}"))
    first = history.search(limit=2)
    assert sqls(first) == ["SELECT 4", "SELECT 3"] and first.next_before is not None
    second = history.search(limit=2, before=first.next_before)
    assert sqls(second) == ["SELECT 2", "SELECT 1"]
    assert history.search(limit=10).next_before is None
    other = uuid4()
    history.record(rec("SELECT other", profile=other, status="error", error="x" * 2000))
    [entry] = history.search(profile_id=other).items
    assert entry.status == "error" and len(entry.error) == 500
    history.close()


def test_retention_and_clear(tmp_path):
    history = HistoryService(tmp_path / "h.db", retention=3, prune_every=1)
    for i in range(5):
        history.record(rec(f"SELECT {i}"))
    assert sqls(history.search()) == ["SELECT 4", "SELECT 3", "SELECT 2"]
    assert sqls(history.search("SELECT 0")) == []  # the full-text index follows deletions
    history.clear()
    assert sqls(history.search()) == [] and sqls(history.search("SELECT")) == []
    history.close()


def test_a_corrupt_file_is_moved_aside(tmp_path):
    (tmp_path / "h.db").write_bytes(b"not a database at all" * 100)
    history = HistoryService(tmp_path / "h.db")
    history.record(rec("SELECT 1"))
    assert sqls(history.search()) == ["SELECT 1"]
    assert list(tmp_path.glob("h.db.corrupt-*"))
    history.close()


def test_a_corrupt_database_is_moved_aside_together_with_its_wal_and_shm(tmp_path, monkeypatch):
    (tmp_path / "h.db").write_bytes(b"not a database at all" * 100)
    (tmp_path / "h.db-wal").write_bytes(b"stale wal")  # left next to a new database, SQLite could replay it into it
    (tmp_path / "h.db-shm").write_bytes(b"stale shm")
    real, calls = HistoryService._connect, []

    def connect(self):  # the first open finds the damage before SQLite touches the companions (e.g. a stale -wal)
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real(self)

    monkeypatch.setattr(HistoryService, "_connect", connect)
    history = HistoryService(tmp_path / "h.db")
    history.record(rec("SELECT 1"))
    assert sqls(history.search()) == ["SELECT 1"]
    history.close()
    [backup] = [p for p in tmp_path.glob("h.db.corrupt-*") if not p.name.endswith(("-wal", "-shm"))]
    assert backup.with_name(backup.name + "-wal").read_bytes() == b"stale wal"
    assert backup.with_name(backup.name + "-shm").read_bytes() == b"stale shm"


def test_a_new_history_that_cannot_be_created_after_the_move_is_a_storage_error(tmp_path, monkeypatch):
    (tmp_path / "h.db").write_bytes(b"not a database at all" * 100)
    failures = iter([sqlite3.DatabaseError("file is not a database"), sqlite3.OperationalError("disk I/O error")])

    def connect(self):
        raise next(failures)

    monkeypatch.setattr(HistoryService, "_connect", connect)
    history = HistoryService(tmp_path / "h.db")
    with pytest.raises(StorageError, match="disk I/O error"):
        history.search()


def test_retention_is_applied_when_the_file_is_opened(tmp_path):
    history = HistoryService(tmp_path / "h.db", retention=2, prune_every=1000)
    for i in range(5):
        history.record(rec(f"SELECT {i}"))
    assert len(history.search().items) == 5  # pruning waits for `prune_every` inserts…
    history.close()
    reopened = HistoryService(tmp_path / "h.db", retention=2, prune_every=1000)
    assert sqls(reopened.search()) == ["SELECT 4", "SELECT 3"]  # …or for the next start
    reopened.close()


def test_a_database_that_cannot_be_used_is_not_moved_aside(tmp_path):
    other = tmp_path / "h.db"
    with sqlite3.connect(other) as conn:  # a valid database, but its `history` table is not ours
        conn.execute("CREATE TABLE history (note TEXT)")
    history = HistoryService(other)
    with pytest.raises(StorageError):  # it fails the calls that need it, not the construction
        history.search()
    assert other.exists() and not list(tmp_path.glob("h.db.corrupt-*"))


def test_nothing_touches_the_disk_until_the_first_use(tmp_path):
    history = HistoryService(tmp_path / "data" / "h.db")
    assert not (tmp_path / "data").exists()
    history.close()  # closing a history that was never opened is fine
    assert not (tmp_path / "data").exists()
    with pytest.raises(StorageError):
        history.record(rec("SELECT 1"))  # and a closed history does not reopen
    assert not (tmp_path / "data").exists()


def test_a_job_record_is_written_running_and_finished_later(tmp_path):  # spec ⑥ D-06-9
    history = HistoryService(tmp_path / "h.db")
    record_id = history.begin(rec("-- Run SQL file /x.sql (1.0 MB), all or nothing"))
    [entry] = history.search().items
    assert (entry.id, entry.status) == (record_id, "running")
    history.finish(record_id, "error", 1234, 56, "Stopped at line 9: boom")
    [entry] = history.search().items
    assert (entry.status, entry.elapsed_ms, entry.rows, entry.error) == ("error", 1234, 56, "Stopped at line 9: boom")
    history.finish(record_id + 1, "ok", 1, 1, None)  # a record that is gone stays gone
    assert len(history.search().items) == 1
    history.close()


def test_records_still_running_at_the_next_start_are_interrupted(tmp_path):  # D-06-9
    first = HistoryService(tmp_path / "h.db")
    first.record(rec("SELECT 1"))
    first.begin(rec("-- Copy data"))
    first.close()  # the backend ended with the job running
    second = HistoryService(tmp_path / "h.db")
    assert [(e.sql, e.status) for e in second.search().items] == [("-- Copy data", "interrupted"), ("SELECT 1", "ok")]
    record_id = second.begin(rec("-- Run SQL file"))  # this run's own: never marked
    assert second.search().items[0].status == "running"
    second.finish(record_id, "ok", 1, 1, None)
    second.close()


# ---- spec ⑧ D-08-13, CR-08-3: the MCP source, its own cap, refusals ----

_BEFORE_08 = """
CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL, profile_name TEXT NOT NULL,
    database_name TEXT, sql TEXT NOT NULL, started_at TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, rows INTEGER NOT NULL,
    status TEXT NOT NULL, error TEXT);
INSERT INTO history (profile_id, profile_name, database_name, sql, started_at, elapsed_ms, rows, status)
    VALUES ('00000000-0000-0000-0000-000000000001', 'p', NULL, 'SELECT old', '2026-10-01T00:00:00+00:00', 1, 1, 'ok');
"""


def mcp(sql, status="ok", client="claude-code"):
    return rec(sql, status).model_copy(update={"source": "mcp", "client": client})


def test_a_file_from_before_08_gets_the_new_columns_in_place(tmp_path):
    path = tmp_path / "h.db"
    old = sqlite3.connect(path)
    old.executescript(_BEFORE_08)
    old.close()
    history = HistoryService(path)
    [entry] = history.search().items
    assert (entry.sql, entry.source, entry.client, entry.repeats) == ("SELECT old", "app", None, 1)
    history.record(mcp("SELECT new"))
    assert sqls(history.search(source="mcp")) == ["SELECT new"]
    assert sqls(history.search(source="app")) == ["SELECT old"]
    history.close()
    indexes = {row[1] for row in sqlite3.connect(path).execute("PRAGMA index_list(history)")}
    assert "history_by_source" in indexes


def test_each_source_keeps_its_own_newest_records(tmp_path):
    history = HistoryService(tmp_path / "h.db", retention=3, mcp_retention=2, prune_every=1)
    for n in range(6):
        history.record(rec(f"app {n}"))
        history.record(mcp(f"mcp {n}"))
    assert sqls(history.search(source="app")) == ["app 5", "app 4", "app 3"]
    assert sqls(history.search(source="mcp")) == ["mcp 5", "mcp 4"]  # an agent never pushes out the user's


def test_a_refusal_sent_again_within_a_minute_adds_to_its_record(tmp_path):
    now = [100.0]
    history = HistoryService(tmp_path / "h.db", clock=lambda: now[0])
    first = history.refused(mcp("DROP TABLE t"))
    now[0] += 30
    assert history.refused(mcp("DROP TABLE t")) == first
    now[0] += 59  # a minute after the last, not the first
    assert history.refused(mcp("DROP TABLE t")) == first
    other_client = history.refused(mcp("DROP TABLE t", client="other"))
    other_text = history.refused(mcp("DROP TABLE u"))
    assert len({first, other_client, other_text}) == 3
    now[0] += 61
    later = history.refused(mcp("DROP TABLE t"))
    entries = {e.id: e for e in history.search(source="mcp").items}
    assert (entries[first].status, entries[first].repeats) == ("refused", 3)
    assert entries[later].repeats == 1 and later != first
    history.clear()
    assert history.refused(mcp("DROP TABLE t")) not in (first, later)


def test_only_the_last_few_refusals_are_remembered_for_merging(tmp_path):  # an in-process memory: 64 kept
    history = HistoryService(tmp_path / "h.db", clock=lambda: 100.0)
    ids = [history.refused(mcp(f"DROP TABLE t{n}")) for n in range(65)]
    assert history.refused(mcp("DROP TABLE t64")) == ids[64]  # among the last 64: merged
    forgotten = history.refused(mcp("DROP TABLE t0"))  # pushed out of the memory: a new record
    assert forgotten not in ids
    repeats = {e.id: e.repeats for e in history.search(source="mcp", limit=1000).items}
    assert (repeats[ids[64]], repeats[ids[0]], repeats[forgotten]) == (2, 1, 1)
