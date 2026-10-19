"""Grid-editing tests (05a): scratch objects on the dev databases, made and dropped by exact name.

Every object a test makes is named `zz_05a_<kind>_<random>` (several runs may share a server) and dropped by that name
when its `scratch()` block ends, whatever the test did. Seeded tables are never written, Couchbase is never used, and
each engine's seed is left exactly as it was. SQLite works on a file in the test's tmp_path. Set-up and checks go
through each engine's own driver, never through the code under test."""

import itertools
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import mysql.connector
import psycopg
import pytest
from pymssql import _mssql

from dbaguy_backend.adapters.base import DatabaseConnection, Endpoint
from dbaguy_backend.adapters.mssql.adapter import MssqlAdapter
from dbaguy_backend.adapters.mysql.adapter import MysqlAdapter
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.dialect import Dialect
from dbaguy_backend.domain.edits import EditChanges, EditPreview, EditTarget
from dbaguy_backend.domain.execution import (
    ApplyEditsCommand, EditOutcomeEvent, ErrorEvent, ExecuteCommand, FinishedEvent,
)
from dbaguy_backend.domain.history import HistoryEntry
from dbaguy_backend.domain.metadata import PathSegment
from dbaguy_backend.domain.profiles import ProfileUpsertRequest, SqliteProfile
from dbaguy_backend.sessions.manager import SessionCreateRequest
from dbaguy_backend.sessions.worker import SessionWorker
from dbaguy_backend.sql.dialects import force_quote_ident
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.support import (
    MSSQL_HOST, MSSQL_PASSWORD, MSSQL_PORT, MSSQL_SA_PASSWORD, MSSQL_USER, MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT,
    MYSQL_USER, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, make_mssql_profile, make_mysql_profile, make_pg_profile,
    mssql_connect, mysql_driver_kwargs, requires_mssql, requires_mysql, requires_pg, unique_name,
)
from tests.test_sessions import Recorder, paused, request_events

SCRATCH_PREFIX = "zz_05a_"
ITEM_ROWS: tuple[tuple[Any, ...], ...] = ((1, "a", 1, None), (2, "b", 2, "x"), (3, "c", 3, "y"))
_CHUNK = 500  # rows per INSERT (SQL Server takes at most 1,000 in one VALUES list)


class ScratchDb(ABC):
    """A dev database reached with its own driver in auto-commit mode."""

    engine: str
    dialect: Dialect
    database: str | None  # the explorer path's database segment; None when the engine's paths have none
    schema: str
    bigint: str  # the shared table's types (`items_table`)
    label: str
    int: str
    note: str
    table_options = ""

    @abstractmethod
    def execute(self, *statements: str) -> None: ...

    @abstractmethod
    def rows(self, sql: str) -> list[tuple]: ...

    @abstractmethod
    def profile(self, **fields: Any) -> dict:
        """A ProfileUpsertRequest body for this database, password in `secrets`; `fields` go over the profile's."""

    @abstractmethod
    def open(self, **fields: Any) -> DatabaseConnection:
        """The adapter's own connection to this database, as a metadata connection gets it."""

    @abstractmethod
    def holding(self, sql: str) -> AbstractContextManager[None]:
        """`sql` inside an open transaction on a second connection, rolled back when the block ends: its row locks are
        held meanwhile."""

    @abstractmethod
    def fill(self, table: str, count: int, start: int) -> None:
        """`count` generated rows `(n, 'row n', n, NULL)` of the shared table shape, n from `start`."""

    def waiting(self, table: str) -> bool:
        """Whether a statement of the dev login that names `table` (its bare name) waits for a lock now. Scoped to the
        table, so another run's lock waits (the suite's cancel tests, another agent's flows) never count."""
        raise NotImplementedError(f"{self.engine} has no lock waits to watch")

    def close(self) -> None:
        return None

    def q(self, name: str) -> str:
        return force_quote_ident(self.dialect, name)

    def table(self, name: str) -> str:
        return f"{self.q(self.schema)}.{self.q(name)}"

    def path(self, name: str, kind: str = "table") -> list[PathSegment]:
        head = [] if self.database is None else [PathSegment(kind="database", name=self.database)]
        folder = {"table": "tables", "view": "views", "materialized_view": "materialized_views"}[kind]
        return [*head, PathSegment(kind="schema", name=self.schema), PathSegment(kind="folder", name=folder),
                PathSegment(kind=kind, name=name)]

    def drop(self, kind: str, name: str) -> None:
        assert name.startswith(SCRATCH_PREFIX), f"{name!r} is not a scratch object: never dropped by these helpers"
        self.execute(f"DROP {kind} IF EXISTS {self.table(name)}")

    def exists(self, name: str) -> bool:
        """Whether the catalog lists a table or view of that bare name in `schema` (checked through the driver)."""
        found = self.rows("SELECT COUNT(*) FROM information_schema.TABLES "
                          f"WHERE TABLE_SCHEMA = {self.literal(self.schema)} AND TABLE_NAME = {self.literal(name)}")
        return found[0][0] > 0

    def literal(self, value: Any) -> str:
        """A plain literal for set-up SQL (ASCII text, numbers, NULL only)."""
        if value is None:
            return "NULL"
        return str(value) if isinstance(value, int) else "'" + str(value).replace("'", "''") + "'"


class PgScratch(ScratchDb):
    engine, dialect, database, schema = "postgres", Dialect.POSTGRES, "dbaguy", "public"
    bigint, label, int, note = "bigint", "varchar(100)", "integer", "text"

    def __init__(self) -> None:
        self._conn = self._connect(autocommit=True)

    @staticmethod
    def _connect(autocommit: bool) -> psycopg.Connection:
        return psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy",
                               autocommit=autocommit)

    def execute(self, *statements: str) -> None:
        for statement in statements:
            self._conn.execute(statement)

    def rows(self, sql: str) -> list[tuple]:
        return [tuple(row) for row in self._conn.execute(sql).fetchall()]

    def profile(self, **fields: Any) -> dict:
        profile = {"engine": "postgres", "name": "pg edits", "host": PG_HOST, "port": PG_PORT, "database": "dbaguy",
                   "username": PG_USER, "ssl": {"mode": "disable"}}
        return {"profile": {**profile, **fields}, "secrets": {"password": PG_PASSWORD}}

    def open(self, **fields: Any) -> DatabaseConnection:
        return PostgresAdapter().open(make_pg_profile(**fields), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)

    @contextmanager
    def holding(self, sql: str) -> Iterator[None]:
        conn = self._connect(autocommit=False)
        try:
            conn.execute(sql)
            yield
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()

    def fill(self, table: str, count: int, start: int) -> None:
        self.execute(f"INSERT INTO {table} (id, label, {self.q('order')}) "
                     f"SELECT g, 'row ' || g, g FROM generate_series({start}, {start + count - 1}) g")

    def waiting(self, table: str) -> bool:
        found = self.rows("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                          f"AND wait_event_type = 'Lock' AND query LIKE {self.literal(f'%{table}%')}")
        return found[0][0] > 0

    def close(self) -> None:
        self._conn.close()


class MysqlScratch(ScratchDb):
    engine, dialect, database, schema = "mysql", Dialect.MYSQL, None, "dbaguy"
    bigint, label, int, note = "BIGINT", "VARCHAR(100)", "INT", "TEXT"
    table_options = " ENGINE=InnoDB"

    def __init__(self) -> None:
        # TLS (the kwargs' default) fills the server's caching_sha2 cache, so the `ssl: disable` profile below logs in
        self._conn = mysql.connector.connect(**mysql_driver_kwargs(autocommit=True))

    def execute(self, *statements: str) -> None:
        for statement in statements:
            cursor = self._conn.cursor()
            try:
                cursor.execute(statement)
                if cursor.with_rows:
                    cursor.fetchall()
            finally:
                cursor.close()

    def rows(self, sql: str) -> list[tuple]:
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql)
            return [tuple(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def profile(self, **fields: Any) -> dict:
        profile = {"engine": "mysql", "name": "mysql edits", "host": MYSQL_HOST, "port": MYSQL_PORT,
                   "database": "dbaguy", "username": MYSQL_USER, "ssl": {"mode": "disable"}}
        return {"profile": {**profile, **fields}, "secrets": {"password": MYSQL_PASSWORD}}

    def open(self, **fields: Any) -> DatabaseConnection:
        return MysqlAdapter().open(make_mysql_profile(**fields), Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None)

    @contextmanager
    def holding(self, sql: str) -> Iterator[None]:
        conn = mysql.connector.connect(**mysql_driver_kwargs())
        try:
            conn.start_transaction()
            cursor = conn.cursor()
            cursor.execute(sql)
            cursor.close()
            yield
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()

    def fill(self, table: str, count: int, start: int) -> None:
        self.execute(
            f"SET SESSION cte_max_recursion_depth = {count + 1}",
            f"INSERT INTO {table} (id, label, {self.q('order')}) WITH RECURSIVE s (n) AS "
            f"(SELECT {start} UNION ALL SELECT n + 1 FROM s WHERE n < {start + count - 1}) "
            "SELECT n, CONCAT('row ', n), n FROM s",
        )

    def waiting(self, table: str) -> bool:
        """MySQL shows a row-lock wait as a plain running UPDATE (State `updating`) and the dev login may not see
        InnoDB's lock tables, so a statement counts as waiting once it has been running more than a second: TIME is
        whole seconds on the wall clock, so `TIME >= 2` is never reached by a statement shorter than a second (`>= 1`
        is, 0.4 s measured), and an UPDATE of the table that is merely slow (2 s or more) counts too. A lock wait is
        therefore seen 1 to 2 s after it starts."""
        # `_` in a LIKE pattern is a wildcard, which only widens the match: the name is unique
        found = self.rows("SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE ID <> CONNECTION_ID() "
                          f"AND COMMAND = 'Query' AND TIME >= 2 AND INFO LIKE {self.literal(f'UPDATE %{table}%')}")
        return found[0][0] > 0

    def close(self) -> None:
        self._conn.close()


class MssqlScratch(ScratchDb):
    engine, dialect, database, schema = "mssql", Dialect.TSQL, "dbaguy", "dbo"
    bigint, label, int, note = "bigint", "nvarchar(100)", "int", "nvarchar(max)"

    def __init__(self) -> None:
        self._conn = mssql_connect()
        self._sa: _mssql.MSSQLConnection | None = None  # opened by the first waiting()

    def execute(self, *statements: str) -> None:
        for statement in statements:
            self._conn.execute_non_query(statement)

    def rows(self, sql: str) -> list[tuple]:
        return self._rows(self._conn, sql)

    @staticmethod
    def _rows(conn: _mssql.MSSQLConnection, sql: str) -> list[tuple]:
        conn.execute_query(sql)
        found = [tuple(row) for row in conn.get_iterator(_mssql.ROW_FORMAT_TUPLE)]
        while conn.nextresult():
            for _ in conn.get_iterator(_mssql.ROW_FORMAT_TUPLE):
                pass
        return found

    def profile(self, **fields: Any) -> dict:
        profile = {"engine": "mssql", "name": "mssql edits", "host": MSSQL_HOST, "port": MSSQL_PORT,
                   "database": "dbaguy", "username": MSSQL_USER, "ssl": {"mode": "prefer"}}
        return {"profile": {**profile, **fields}, "secrets": {"password": MSSQL_PASSWORD}}

    def open(self, **fields: Any) -> DatabaseConnection:
        return MssqlAdapter().open(make_mssql_profile(**fields), Endpoint(MSSQL_HOST, MSSQL_PORT), MSSQL_PASSWORD, None)

    @contextmanager
    def holding(self, sql: str) -> Iterator[None]:
        conn = mssql_connect()
        try:
            conn.execute_non_query(f"BEGIN TRANSACTION; {sql}")
            yield
        finally:
            try:
                conn.execute_non_query("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION")
            finally:
                conn.close()

    def fill(self, table: str, count: int, start: int) -> None:
        self.execute(
            f"WITH s AS (SELECT TOP ({count}) {start - 1} + ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n "
            "FROM sys.all_objects a CROSS JOIN sys.all_objects b CROSS JOIN sys.all_objects c) "
            f"INSERT INTO {table} (id, label, {self.q('order')}) SELECT n, CONCAT(N'row ', n), n FROM s"
        )

    def waiting(self, table: str) -> bool:
        # dm_exec_requests shows other sessions only with VIEW SERVER STATE, which the dbaguy login lacks: ask as sa,
        # on one connection kept for every poll
        if self._sa is None:
            self._sa = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database="master")
        found = self._rows(self._sa, "SELECT COUNT(*) FROM sys.dm_exec_requests r "
                                     "CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t "
                                     f"WHERE r.blocking_session_id <> 0 AND t.text LIKE N{self.literal(f'%{table}%')}")
        return found[0][0] > 0

    def close(self) -> None:
        try:
            if self._sa is not None:
                self._sa.close()
        finally:
            self._sa = None
            self._conn.close()


class SqliteScratch(ScratchDb):
    engine, dialect, database, schema = "sqlite", Dialect.SQLITE, None, "main"
    bigint, label, int, note = "INTEGER", "TEXT", "INTEGER", "TEXT"

    def __init__(self, directory: Path) -> None:
        self.file = directory / "edits.db"
        self._conn = sqlite3.connect(self.file, isolation_level=None)

    def execute(self, *statements: str) -> None:
        for statement in statements:
            self._conn.execute(statement)

    def rows(self, sql: str) -> list[tuple]:
        return [tuple(row) for row in self._conn.execute(sql).fetchall()]

    def exists(self, name: str) -> bool:
        return self.rows(f"SELECT COUNT(*) FROM sqlite_master WHERE name = {self.literal(name)}")[0][0] > 0

    def profile(self, **fields: Any) -> dict:
        return {"profile": {"engine": "sqlite", "name": "sqlite edits", "path": str(self.file), **fields}}

    def open(self, **fields: Any) -> DatabaseConnection:
        profile = SqliteProfile(id=uuid4(), engine="sqlite", name="s", path=str(self.file), **fields)
        return SqliteAdapter().open(profile, None, None, None)

    @contextmanager
    def holding(self, sql: str) -> Iterator[None]:
        conn = sqlite3.connect(self.file, isolation_level=None, timeout=0.1)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(sql)
            yield
        finally:
            try:
                conn.execute("ROLLBACK")
            finally:
                conn.close()

    def fill(self, table: str, count: int, start: int) -> None:
        self.execute(f"WITH RECURSIVE s (n) AS (SELECT {start} UNION ALL SELECT n + 1 FROM s WHERE n < "
                     f"{start + count - 1}) INSERT INTO {table} (id, label, {self.q('order')}) "
                     "SELECT n, 'row ' || n, n FROM s")

    def close(self) -> None:
        self._conn.close()


ENGINES = (
    pytest.param("postgres", marks=requires_pg, id="postgres"),
    pytest.param("mysql", marks=requires_mysql, id="mysql"),
    pytest.param("mssql", marks=requires_mssql, id="mssql"),
    pytest.param("sqlite", id="sqlite"),
)
SERVER_ENGINES = ENGINES[:3]


def scratch_db(engine: str, directory: Path) -> ScratchDb:
    if engine == "sqlite":
        return SqliteScratch(directory)
    return {"postgres": PgScratch, "mysql": MysqlScratch, "mssql": MssqlScratch}[engine]()


@contextmanager
def scratch(db: ScratchDb, before_drop: Callable[[], None] | None = None) -> Iterator[Callable[[str, str], str]]:
    """`make(kind, ddl)` creates one object and returns its bare name: in `ddl`, `{t}` is its qualified name and `{n}`
    its bare name. On exit `before_drop` runs first (close the sessions that could hold a lock the DROP waits for),
    then every object is dropped by exact name, newest first; every drop is tried before the failures are reported.
    A failed drop fails the test only when its body succeeded: when the body raised, the failures become a note on
    that exception, so the test's own failure is the one pytest shows."""
    made: list[tuple[str, str]] = []

    def make(kind: str, ddl: str) -> str:
        name = unique_name(SCRATCH_PREFIX + kind.lower().replace(" ", "_"))
        made.append((kind, name))
        db.execute(ddl.replace("{t}", db.table(name)).replace("{n}", name))
        return name

    def clean_up() -> str:
        """The failures of before_drop and of the drops, as one text ('' when all went well)."""
        failures = []
        if before_drop is not None:
            try:
                before_drop()
            except Exception as exc:
                failures.append(f"before_drop: {exc}")
        for kind, name in reversed(made):
            try:
                db.drop(kind, name)
            except Exception as exc:
                failures.append(f"{kind} {name}: {exc}")
        return "; ".join(failures)

    try:
        yield make
    except BaseException as exc:
        if failed := clean_up():
            exc.add_note("dropping scratch objects failed: " + failed)
        raise
    else:
        failed = clean_up()
        assert not failed, "dropping scratch objects failed: " + failed


def items_table(db: ScratchDb, make: Callable[[str, str], str], rows: Sequence[tuple] = ITEM_ROWS) -> str:
    """The table the shared flows use: a bigint key, a NOT NULL label, a nullable int named `order` (reserved in every
    dialect) and a nullable note."""
    name = make("TABLE", f"CREATE TABLE {{t}} (id {db.bigint} PRIMARY KEY, label {db.label} NOT NULL, "
                         f"{db.q('order')} {db.int} NULL, note {db.note} NULL){db.table_options}")
    for start in range(0, len(rows), _CHUNK):
        values = ", ".join("(" + ", ".join(db.literal(v) for v in row) + ")" for row in rows[start:start + _CHUNK])
        db.execute(f"INSERT INTO {db.table(name)} (id, label, {db.q('order')}, note) VALUES {values}")
    return name


# --- the app in-process (Task 9) -------------------------------------------------------------------------------------


def val(text: str) -> dict:
    return {"kind": "value", "text": text}


NULL = {"kind": "null"}
DEFAULT = {"kind": "default"}


def update(change_id: str, key: dict, set_: dict, original: dict) -> dict:
    return {"id": change_id, "key": key, "set": set_, "original": original}


def insert(change_id: str, values: dict) -> dict:
    return {"id": change_id, "values": values}


def delete(change_id: str, key: dict) -> dict:
    return {"id": change_id, "key": key}


@dataclass
class Applied:
    """One request's events, from its `started` to its `finished`."""

    events: list

    @property
    def types(self) -> list[str]:
        return [event.type for event in self.events]

    @property
    def outcome(self) -> EditOutcomeEvent:
        [event] = [event for event in self.events if event.type == "edit_outcome"]  # exactly one per apply (E1)
        return event

    @property
    def error(self) -> ErrorEvent | None:
        return next((event for event in self.events if event.type == "error"), None)

    @property
    def affected(self) -> list[tuple[int, int]]:
        return [(event.index, event.rows) for event in self.events if event.type == "affected"]

    @property
    def messages(self) -> list[str]:
        return [event.text for event in self.events if event.type == "message"]

    @property
    def finished(self) -> FinishedEvent:
        return self.events[-1]


class Tab:
    """A table-data tab's session: its worker, with the events it sent."""

    def __init__(self, worker: SessionWorker, recorder: Recorder, database: str | None) -> None:
        self.worker = worker
        self.recorder = recorder
        self.database = database
        self._ids = itertools.count(1)

    def _rid(self, what: str) -> str:
        return f"{what}{next(self._ids)}"

    def run(self, sql: str) -> Applied:
        """A statement of the test's own (a session setting), run to its end without an error."""
        rid = self._rid("run")
        self.worker.submit(ExecuteCommand(request_id=rid, sql=sql, history=False, page_size=10_000))
        done = self.wait(rid)
        assert done.error is None, done.error.message
        return done

    def rows(self, sql: str) -> list[list]:
        """The cells of a SELECT exactly as the grid gets them (encode_row)."""
        done = self.run(sql)
        return [row for event in done.events if event.type == "rows" for row in event.rows]

    def browse(self, sql: str, page_size: int = 500) -> str:
        """A SELECT left paused after its first page, as the table-data grid leaves it."""
        rid = self._rid("browse")
        self.worker.submit(ExecuteCommand(request_id=rid, sql=sql, history=False, page_size=page_size))
        self.recorder.wait_for(paused(rid, page_size), timeout=30)
        return rid

    def submit_apply(self, plan_id: str | None) -> str:
        rid = self._rid("apply")
        self.worker.submit(ApplyEditsCommand(request_id=rid, plan_id=plan_id or "none"))
        return rid

    def apply(self, plan_id: str | None, timeout: float = 60.0) -> Applied:
        return self.wait(self.submit_apply(plan_id), timeout)

    def wait(self, rid: str, timeout: float = 60.0) -> Applied:
        return Applied(request_events(self.recorder, rid, timeout))  # the slice Task 8's tests use too (L18)


class EditApp:
    """The backend in-process — an AppContainer over a temporary data dir with secrets in memory — so a test previews
    through EditService and applies through a session's worker with its real hooks and adapter (ruling E21)."""

    def __init__(self, directory: Path) -> None:
        settings = Settings(token="edits", data_dir=directory / "data", log_to_file=False)
        self.container = AppContainer(settings, secret_store=InMemorySecretStore())

    def add_profile(self, body: dict) -> UUID:
        return self.container.profiles.create(ProfileUpsertRequest.model_validate(body)).profile.id

    def update_profile(self, profile_id: UUID, body: dict) -> None:
        self.container.profiles.update(profile_id, ProfileUpsertRequest.model_validate(body))

    def target(self, profile_id: UUID, path: list[PathSegment]) -> EditTarget:
        return self.container.edits.target(profile_id, path)

    def preview(self, profile_id: UUID, path: list[PathSegment], *, updates=(), inserts=(), deletes=()) -> EditPreview:
        changes = EditChanges.model_validate({"updates": list(updates), "inserts": list(inserts),
                                              "deletes": list(deletes)})
        return self.container.edits.preview(profile_id, path, changes)

    def tab(self, profile_id: UUID, database: str | None) -> Tab:
        info = self.container.sessions.create(SessionCreateRequest(profile_id=profile_id, database=database))
        recorder = Recorder()
        return Tab(self.container.sessions.attach(info.session_id, recorder), recorder, info.database)

    def history(self) -> list[HistoryEntry]:
        return self.container.history.search().items

    def close_tabs(self) -> None:
        """Every session closed: nothing holds a lock a scratch DROP would wait for."""
        self.container.sessions.close_all()

    def close(self) -> None:
        self.container.close()


@contextmanager
def edit_env(engine: str, directory: Path) -> Iterator[tuple[ScratchDb, EditApp, UUID]]:
    """A scratch database, the app, and a profile for it (password saved in memory)."""
    db = scratch_db(engine, directory)
    app = EditApp(directory)
    try:
        yield db, app, app.add_profile(db.profile())
    finally:
        app.close()
        db.close()
