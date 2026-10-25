import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from loguru import logger

from ..domain.errors import StorageError
from ..domain.history import HistoryEntry, HistoryPage, HistoryRecord, HistorySource, HistoryStatus
from ..storage.json_files import move_aside

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL,
    profile_name TEXT NOT NULL,
    database_name TEXT,
    sql TEXT NOT NULL,
    started_at TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    source TEXT NOT NULL DEFAULT 'app',
    client TEXT,
    repeats INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS history_by_profile ON history (profile_id, id);
CREATE VIRTUAL TABLE IF NOT EXISTS history_fts
    USING fts5(sql, content='history', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS history_ai AFTER INSERT ON history BEGIN
    INSERT INTO history_fts (rowid, sql) VALUES (new.id, new.sql);
END;
CREATE TRIGGER IF NOT EXISTS history_ad AFTER DELETE ON history BEGIN
    INSERT INTO history_fts (history_fts, rowid, sql) VALUES ('delete', old.id, old.sql);
END;
"""
_DATA_COLUMNS = (
    "profile_id, profile_name, database_name, sql, started_at, elapsed_ms, rows, status, error, source, client, repeats"
)
# spec ⑧ D-08-13: columns an older file lacks, added in place when it is opened
_ADDED_COLUMNS = (
    ("source", "TEXT NOT NULL DEFAULT 'app'"), ("client", "TEXT"), ("repeats", "INTEGER NOT NULL DEFAULT 1"),
)
_SOURCE_INDEX = "CREATE INDEX IF NOT EXISTS history_by_source ON history (source, id)"
APP_RETENTION = 50_000
MCP_RETENTION = 5_000  # CR-08-3: an agent in a loop never pushes out the user's own records
REFUSAL_MERGE_S = 60.0  # CR-08-3: the same refused text from the same client within this long adds to one record
_REFUSALS_KEPT = 64
_MAX_ERROR = 500
_TRIGRAM = 3  # the full-text index matches from this many characters; shorter searches scan with LIKE
_SQLITE_COMPANIONS = ("-wal", "-shm")  # a database's write-ahead log and its index: they go wherever it goes


def _like_pattern(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _entry(row: tuple) -> HistoryEntry:
    (id_, profile_id, profile_name, database, sql, started_at, elapsed_ms, rows, status, error, source, client,
     repeats) = row
    return HistoryEntry(
        id=id_, profile_id=profile_id, profile_name=profile_name, database=database, sql=sql, started_at=started_at,
        elapsed_ms=elapsed_ms, rows=rows, status=status, error=error, source=source, client=client, repeats=repeats,
    )


class HistoryService:
    """Executed statements in a local SQLite database, searchable by substring.

    One connection shared by every thread, serialised by a lock, opened by the first use: building the app touches no
    file, and a history that cannot be opened fails the calls that need it, not the start of the app. A file that is
    not a database is moved aside and replaced by an empty one; other open failures (locked, read-only, no FTS5
    trigram) leave the file alone. A file from before ⑧ gets its new columns in place.

    The user's records and MCP agents' records are kept apart (spec ⑧ D-08-13): each source keeps its own newest
    `retention` / `mcp_retention`.
    """

    def __init__(self, path: Path, retention: int = APP_RETENTION, prune_every: int = 500,
                 mcp_retention: int = MCP_RETENTION, clock: Callable[[], float] = time.monotonic) -> None:
        self._path = path
        self._retention = retention
        self._mcp_retention = mcp_retention
        self._clock = clock
        # The last refusal of each (profile, database, client, text): its record and when (CR-08-3); the newest few only
        self._refusals: OrderedDict[tuple[str, str | None, str | None, str], tuple[int, float]] = OrderedDict()
        self._prune_every = prune_every
        self._inserts = 0
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._closed = False

    def _connection(self) -> sqlite3.Connection:
        """The open connection; the caller holds the lock."""
        if self._closed:
            raise StorageError("The history is closed")
        if self._conn is None:
            self._conn = self._open()
            logger.debug("History database {} is open", self._path)
            self._prune(self._conn)  # a short-lived run may never reach `prune_every` inserts
            self._interrupt(self._conn)
        return self._conn

    @staticmethod
    def _interrupt(conn: sqlite3.Connection) -> None:
        """Records still `running` belong to a backend that ended mid-job (D-06-9): one process uses the history, and
        it opens the database before it writes anything, so none of them can be this run's."""
        marked = conn.execute("UPDATE history SET status = 'interrupted' WHERE status = 'running'").rowcount
        if marked > 0:
            logger.warning("{} history record(s) of an earlier run were still running: marked interrupted", marked)

    def _open(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._connect()
        except sqlite3.OperationalError as exc:  # a database we cannot use, not one that is damaged
            logger.error("Cannot open the history {}: {}", self._path, exc)
            raise StorageError(f"Cannot open the history: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            move_aside(self._path, str(exc), StorageError, companions=_SQLITE_COMPANIONS)
        try:
            return self._connect()
        except sqlite3.Error as exc:
            logger.error("Cannot create a new history {}: {}", self._path, exc)
            raise StorageError(f"Cannot open the history: {exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._upgrade(conn)
        except sqlite3.DatabaseError:
            conn.close()
            raise
        return conn

    @staticmethod
    def _upgrade(conn: sqlite3.Connection) -> None:
        """Add the columns a file from before ⑧ lacks (its rows are the user's: `source` 'app'), then their index."""
        have = {row[1] for row in conn.execute("PRAGMA table_info(history)")}
        for name, declaration in _ADDED_COLUMNS:
            if name not in have:
                conn.execute(f"ALTER TABLE history ADD COLUMN {name} {declaration}")
                logger.info("History: added the column {}", name)
        conn.execute(_SOURCE_INDEX)

    def record(self, rec: HistoryRecord) -> None:
        self._insert(rec)

    def begin(self, rec: HistoryRecord) -> int:
        """A job's record, written as it starts (status `running`, D-06-9): its id, for `finish`."""
        return self._insert(rec.model_copy(update={"status": "running"}))

    def finish(self, record_id: int, status: HistoryStatus, elapsed_ms: int, rows: int, error: str | None) -> None:
        """How the job of `begin` ended. A record that is gone (cleared or pruned meanwhile) is left gone."""
        with self._lock:
            self._connection().execute(
                "UPDATE history SET status = ?, elapsed_ms = ?, rows = ?, error = ? WHERE id = ?",
                (status, elapsed_ms, rows, error[:_MAX_ERROR] if error else None, record_id),
            )

    def refused(self, rec: HistoryRecord) -> int:
        """An MCP agent's refused statement (status `refused`, CR-08-3): when the same client sent the same text to the
        same connection and database less than REFUSAL_MERGE_S ago, that record's `repeats` grows instead of a new one
        being written. Its id."""
        key = (str(rec.profile_id), rec.database, rec.client, rec.sql)
        now = self._clock()
        with self._lock:
            conn = self._connection()
            last = self._refusals.get(key)
            if last is not None and now - last[1] < REFUSAL_MERGE_S and conn.execute(
                    "UPDATE history SET repeats = repeats + 1 WHERE id = ?", (last[0],)).rowcount:
                record_id = last[0]
                logger.debug("History: a refusal sent again, merged into record {}", record_id)
            else:  # the first, or its record was pruned or cleared meanwhile
                record_id = self._insert_locked(conn, rec.model_copy(update={"status": "refused", "repeats": 1}))
            self._refusals[key] = (record_id, now)
            self._refusals.move_to_end(key)
            while len(self._refusals) > _REFUSALS_KEPT:
                self._refusals.popitem(last=False)
            return record_id

    def _insert(self, rec: HistoryRecord) -> int:
        with self._lock:
            return self._insert_locked(self._connection(), rec)

    def _insert_locked(self, conn: sqlite3.Connection, rec: HistoryRecord) -> int:
        error = rec.error[:_MAX_ERROR] if rec.error else None
        cursor = conn.execute(
            f"INSERT INTO history ({_DATA_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(rec.profile_id), rec.profile_name, rec.database, rec.sql, rec.started_at.isoformat(),
             rec.elapsed_ms, rec.rows, rec.status, error, rec.source, rec.client, rec.repeats),
        )
        self._inserts += 1
        if self._inserts % self._prune_every == 0:
            self._prune(conn)
        return int(cursor.lastrowid or 0)

    def _prune(self, conn: sqlite3.Connection) -> None:
        """Drop each source's entries beyond its retention (D-08-13). The caller holds the lock."""
        for source, keep in (("app", self._retention), ("mcp", self._mcp_retention)):
            oldest_kept = conn.execute(
                "SELECT id FROM history WHERE source = ? ORDER BY id DESC LIMIT 1 OFFSET ?", (source, keep - 1),
            ).fetchone()
            if oldest_kept is None:
                continue
            removed = conn.execute(
                "DELETE FROM history WHERE source = ? AND id < ?", (source, oldest_kept[0]),
            ).rowcount
            if removed > 0:
                logger.debug("Pruned {} old {} history entries", removed, source)

    def search(
        self, q: str = "", profile_id: UUID | None = None, limit: int = 100, before: int | None = None,
        source: HistorySource | None = None,
    ) -> HistoryPage:
        where: list[str] = []
        params: list[object] = []
        if profile_id is not None:
            where.append("profile_id = ?")
            params.append(str(profile_id))
        if source is not None:
            where.append("source = ?")
            params.append(source)
        if before is not None:
            where.append("id < ?")
            params.append(before)
        q = q.strip()
        if len(q) >= _TRIGRAM:
            where.append("id IN (SELECT rowid FROM history_fts WHERE history_fts MATCH ?)")
            params.append('"' + q.replace('"', '""') + '"')  # one phrase: a substring, whatever characters it holds
        elif q:
            where.append("sql LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(q))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            rows = self._connection().execute(
                f"SELECT id, {_DATA_COLUMNS} FROM history {clause} ORDER BY id DESC LIMIT ?", (*params, limit + 1)
            ).fetchall()
        items = [_entry(row) for row in rows[:limit]]
        return HistoryPage(items=items, next_before=items[-1].id if len(rows) > limit else None)

    def clear(self) -> None:
        with self._lock:
            self._connection().execute("DELETE FROM history")
            self._refusals.clear()
        logger.info("History cleared")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._conn is not None:
                self._conn.close()
                self._conn = None
