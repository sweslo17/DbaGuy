import sqlite3
import threading
from pathlib import Path
from uuid import UUID

from loguru import logger

from ..domain.errors import StorageError
from ..domain.history import HistoryEntry, HistoryPage, HistoryRecord
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
    error TEXT
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
_DATA_COLUMNS = "profile_id, profile_name, database_name, sql, started_at, elapsed_ms, rows, status, error"
_MAX_ERROR = 500
_TRIGRAM = 3  # the full-text index matches from this many characters; shorter searches scan with LIKE


def _like_pattern(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _entry(row: tuple) -> HistoryEntry:
    id_, profile_id, profile_name, database, sql, started_at, elapsed_ms, rows, status, error = row
    return HistoryEntry(
        id=id_, profile_id=profile_id, profile_name=profile_name, database=database, sql=sql, started_at=started_at,
        elapsed_ms=elapsed_ms, rows=rows, status=status, error=error,
    )


class HistoryService:
    """Executed statements in a local SQLite database, searchable by substring.

    One connection shared by every thread, serialised by a lock, opened by the first use: building the app touches no
    file, and a history that cannot be opened fails the calls that need it, not the start of the app. A file that is
    not a database is moved aside and replaced by an empty one; other open failures (locked, read-only, no FTS5
    trigram) leave the file alone.
    """

    def __init__(self, path: Path, retention: int = 50_000, prune_every: int = 500) -> None:
        self._path = path
        self._retention = retention
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
        return self._conn

    def _open(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._connect()
        except sqlite3.OperationalError as exc:  # a database we cannot use, not one that is damaged
            logger.error("Cannot open the history {}: {}", self._path, exc)
            raise StorageError(f"Cannot open the history: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            move_aside(self._path, str(exc), StorageError)
            return self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        except sqlite3.DatabaseError:
            conn.close()
            raise
        return conn

    def record(self, rec: HistoryRecord) -> None:
        error = rec.error[:_MAX_ERROR] if rec.error else None
        with self._lock:
            conn = self._connection()
            conn.execute(
                f"INSERT INTO history ({_DATA_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(rec.profile_id), rec.profile_name, rec.database, rec.sql, rec.started_at.isoformat(),
                 rec.elapsed_ms, rec.rows, rec.status, error),
            )
            self._inserts += 1
            if self._inserts % self._prune_every == 0:
                self._prune(conn)

    def _prune(self, conn: sqlite3.Connection) -> None:
        """Drop the entries beyond the retention. The caller holds the lock."""
        removed = conn.execute(
            "DELETE FROM history WHERE id <= (SELECT max(id) FROM history) - ?", (self._retention,)
        ).rowcount
        if removed > 0:
            logger.debug("Pruned {} old history entries", removed)

    def search(
        self, q: str = "", profile_id: UUID | None = None, limit: int = 100, before: int | None = None
    ) -> HistoryPage:
        where: list[str] = []
        params: list[object] = []
        if profile_id is not None:
            where.append("profile_id = ?")
            params.append(str(profile_id))
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
        logger.info("History cleared")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._conn is not None:
                self._conn.close()
                self._conn = None
