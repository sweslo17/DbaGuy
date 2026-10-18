import sqlite3
import threading
import time
from uuid import uuid4

import pytest

from dbaguy_backend.adapters.base import StatementCancelled, StatementError
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.domain.errors import ConnectionFailed, NotFound, ValidationFailed
from dbaguy_backend.domain.metadata import PathSegment
from dbaguy_backend.domain.profiles import SqliteProfile

SCHEMA = """
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE books (
    id INTEGER PRIMARY KEY,
    author_id INTEGER NOT NULL REFERENCES authors(id),
    title TEXT,
    cover BLOB,
    big INTEGER
);
CREATE INDEX books_author ON books(author_id);
CREATE VIEW book_titles AS SELECT title FROM books;
CREATE TRIGGER books_ai AFTER INSERT ON books BEGIN SELECT 1; END;
"""


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "my db #1 ?.sqlite"  # Review Focus 4: spaces, '#', '?'
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO authors VALUES (1, '林')")
    conn.executemany(
        "INSERT INTO books (author_id, title, cover, big) VALUES (1, ?, ?, ?)",
        [(f"t{i}", b"\x00\xff", 2**60) for i in range(1200)],
    )
    conn.commit()
    conn.close()
    return path


def open_conn(path, read_only=False):
    profile = SqliteProfile(id=uuid4(), name="t", engine="sqlite", path=str(path), read_only=read_only)
    return SqliteAdapter().open(profile, None, None, None)


def seg(kind, name):
    return PathSegment(kind=kind, name=name)


def test_tree_and_ddl(db_path):
    conn = open_conn(db_path)
    try:
        [main] = conn.list_children([])
        assert main.name == "main"
        assert [n.name for n in conn.list_children(main.path)] == ["tables", "views", "indexes", "triggers"]
        tables = conn.list_children([seg("schema", "main"), seg("folder", "tables")])
        assert [t.name for t in tables] == ["authors", "books"]
        assert [n.name for n in conn.list_children(tables[1].path)] == ["columns", "indexes", "triggers"]
        cols = conn.list_children([*tables[1].path, seg("folder", "columns")])
        assert cols[0].name == "id" and "PK" in cols[0].detail
        detail = conn.object_detail(tables[1].path)
        assert detail.ddl.startswith("CREATE TABLE books")
        assert [c.name for c in detail.columns][:2] == ["id", "author_id"]
        view = conn.list_children([seg("schema", "main"), seg("folder", "views")])[0]
        assert conn.object_detail(view.path).ddl.startswith("CREATE VIEW book_titles")
    finally:
        conn.close()


def test_run_pages_and_raw_values(db_path):
    conn = open_conn(db_path)
    try:
        result = conn.run("SELECT id, title, cover, big FROM books ORDER BY id")
        assert [c.category for c in result.columns()] == ["number", "text", "binary", "number"]
        first = result.fetch(500)
        assert len(first) == 500
        assert first[0][2] == b"\x00\xff"  # raw driver values; the session worker encodes them for the client
        assert first[0][3] == 2**60
        assert len(result.fetch(1000)) == 700
        assert result.fetch(10) == []
        result.close()
        insert = conn.run("INSERT INTO authors (name) VALUES ('x')")
        assert insert.columns() is None and insert.affected_rows() == 1
        assert conn.run("SELECT name FROM authors WHERE id = 1").fetch(1) == [["林"]]
    finally:
        conn.close()


def test_errors_and_read_only(db_path):
    conn = open_conn(db_path, read_only=True)
    try:
        with pytest.raises(StatementError, match="no such table"):
            conn.run("SELECT * FROM nope")
        with pytest.raises(StatementError, match="readonly"):
            conn.run("DELETE FROM books")
    finally:
        conn.close()


def test_cancel_long_query(db_path):
    conn = open_conn(db_path)
    sql = (
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 2000000000) "
        "SELECT count(*) FROM c"
    )
    threading.Timer(0.3, conn.cancel).start()
    started = time.monotonic()
    with pytest.raises(StatementCancelled):
        conn.run(sql).fetch(1)
    assert time.monotonic() - started < 5
    assert conn.run("SELECT 1").fetch(1) == [[1]]
    conn.close()


ENDLESS = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"


def test_cancel_landing_as_run_starts_is_not_lost(db_path, monkeypatch):
    """sqlite3_interrupt() does nothing while no statement runs, and a statement clears it when it starts: a cancel that
    arrived while run() was still parsing the statement used to vanish, and an endless statement then never stopped."""
    conn = open_conn(db_path)
    # the cancel lands right after run() is entered, before the statement is parsed and stepped
    monkeypatch.setattr(conn, "_begin_if_manual", lambda sql: conn.cancel())
    watchdog = threading.Timer(3, conn.cancel)  # a lost cancel must fail this test, not hang the suite
    watchdog.start()
    started = time.monotonic()
    try:
        with pytest.raises(StatementCancelled):
            conn.run(ENDLESS)
        assert time.monotonic() - started < 2, "the cancel was lost; only the watchdog stopped the statement"
    finally:
        watchdog.cancel()
        conn.close()


def test_a_cancel_with_nothing_running_does_not_cancel_the_next_statement(db_path):
    conn = open_conn(db_path)
    try:
        conn.cancel()
        # long enough for SQLite's progress handler (every 10 000 instructions) to look at a stale flag
        sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 200000) SELECT count(*) FROM c"
        assert conn.run(sql).fetch(1) == [[200000]]
    finally:
        conn.close()


def test_missing_file(tmp_path):
    with pytest.raises(ConnectionFailed):
        open_conn(tmp_path / "missing.db")


def test_parse_url():
    parsed = SqliteAdapter().parse_url("sqlite:///Users/me/My%20App.db")
    assert parsed.profile.path == "/Users/me/My App.db"
    assert parsed.profile.name == "My App.db"


@pytest.mark.parametrize(
    ("url", "path"),
    [
        ("sqlite:////Users/me/app.db", "/Users/me/app.db"),  # SQLAlchemy's absolute form
        ("sqlite:///Users/me/app.db", "/Users/me/app.db"),
        ("sqlite:/abs.db", "/abs.db"),
        ("sqlite://rel.db", "rel.db"),
        ("sqlite:rel.db", "rel.db"),
        ("sqlite:~/app.db", "~/app.db"),
        ("sqlite://~/app.db", "~/app.db"),
    ],
)
def test_parse_url_paths(url, path):
    assert SqliteAdapter().parse_url(url).profile.path == path


def test_trigger_and_table_share_a_name(tmp_path):
    path = tmp_path / "shared.db"
    setup = sqlite3.connect(path)
    setup.executescript("CREATE TABLE t(a); CREATE TRIGGER t AFTER INSERT ON t BEGIN SELECT 1; END;")
    setup.close()
    conn = open_conn(path)
    try:
        trigger = conn.object_detail([seg("schema", "main"), seg("folder", "triggers"), seg("trigger", "t")])
        assert trigger.ddl.startswith("CREATE TRIGGER t")
        table = conn.object_detail([seg("schema", "main"), seg("folder", "tables"), seg("table", "t")])
        assert table.ddl.startswith("CREATE TABLE t")
    finally:
        conn.close()


def test_non_database_file_is_rejected(tmp_path):
    path = tmp_path / "notes.db"
    path.write_text("this is plain text, not a database\n" * 10)
    with pytest.raises(ConnectionFailed):
        open_conn(path)


def test_unknown_folder_is_validation_failed(db_path):
    conn = open_conn(db_path)
    try:
        with pytest.raises(ValidationFailed):
            conn.list_children([seg("schema", "main"), seg("folder", "bogus")])
        with pytest.raises(ValidationFailed):
            conn.list_children([seg("schema", "main"), seg("folder", "columns")])
    finally:
        conn.close()


def test_unknown_schema_is_not_found(db_path):
    conn = open_conn(db_path)
    try:
        with pytest.raises(NotFound):
            conn.list_children([seg("schema", "nope")])
        with pytest.raises(NotFound):
            conn.object_detail([seg("schema", "nope"), seg("folder", "tables"), seg("table", "books")])
    finally:
        conn.close()


def test_sqlite_errors_become_statement_errors(db_path):
    conn = open_conn(db_path)
    conn.close()
    with pytest.raises(StatementError):
        conn.list_children([seg("schema", "main")])
