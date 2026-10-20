"""Data-tools tests (⑥): scratch objects on the dev databases, made and dropped by exact name, and the backend in-process
running jobs on sessions of their own.

Every object a test makes is named `zz_06_<kind>_<random>` (several runs may share a server) and dropped by that name
when its `dropping()` block ends, whatever the test did. Seeded tables are never written; Couchbase is written only
under `zz_06_` keys in `dbaguy.app.orders`, removed by exact key. Set-up and checks go through each engine's own driver
(`tests.edit_support`'s ScratchDb), never through the code under test."""

import itertools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import mysql.connector
import psycopg
from pymssql import _mssql

from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.data_jobs import FileInspection, JobCommand, JobOutcomeEvent, JobProgressEvent
from dbaguy_backend.domain.execution import ErrorEvent, FinishedEvent
from dbaguy_backend.domain.history import HistoryEntry
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.sessions.manager import SessionCreateRequest
from dbaguy_backend.sessions.worker import SessionWorker
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.edit_support import ScratchDb
from tests.support import (
    MSSQL_SA_PASSWORD, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, mssql_connect, mysql_driver_kwargs, unique_name,
)
from tests.test_sessions import Recorder, request_events

SCRATCH_PREFIX = "zz_06_"
_ROW_TUPLE = _mssql.ROW_FORMAT_TUPLE


def scratch_name(kind: str) -> str:
    return unique_name(SCRATCH_PREFIX + kind)


def drop_statement(db: ScratchDb, kind: str, name: str) -> str:
    """A scratch schema goes with everything in it (its objects are all the test's); anything else by its name in
    `db`'s schema."""
    if kind == "SCHEMA":
        return f"DROP SCHEMA IF EXISTS {db.q(name)}" + (" CASCADE" if db.engine == "postgres" else "")
    return f"DROP {kind} IF EXISTS {db.table(name)}"


@contextmanager
def dropping(db: ScratchDb, before_drop: Callable[[], None] | None = None) -> Iterator[list[tuple[str, str]]]:
    """A list the test appends `(kind, bare name)` to for every object it (or a job) creates in `db`'s schema. On
    exit `before_drop` runs (close the sessions that could hold a lock the DROP waits for), then each is dropped by
    exact name, newest first; every drop is tried before the failures are reported, and a failure of the body wins."""
    made: list[tuple[str, str]] = []

    def clean_up() -> str:
        failures = []
        if before_drop is not None:
            try:
                before_drop()
            except Exception as exc:
                failures.append(f"before_drop: {exc}")
        for kind, name in reversed(made):
            assert name.startswith(SCRATCH_PREFIX), f"{name!r} is not a scratch object: never dropped here"
            try:
                db.execute(drop_statement(db, kind, name))
            except Exception as exc:
                failures.append(f"{kind} {name}: {exc}")
        return "; ".join(failures)

    try:
        yield made
    except BaseException as exc:
        if failed := clean_up():
            exc.add_note("dropping scratch objects failed: " + failed)
        raise
    else:
        failed = clean_up()
        assert not failed, "dropping scratch objects failed: " + failed


class _Admin:
    """Creates and drops a scratch database as the engine's administrator (PostgreSQL: the dev login is one; MySQL:
    root; SQL Server: sa), giving the dev login every right in it."""

    def __init__(self, engine: str) -> None:
        self.engine = engine

    def create(self, name: str) -> None:
        self._run(*{
            "postgres": [f'CREATE DATABASE "{name}"'],
            "mysql": [f"CREATE DATABASE `{name}`", f"GRANT ALL ON `{name}`.* TO 'dbaguy'@'%'"],
            "mssql": [f"CREATE DATABASE [{name}]",
                      f"USE [{name}]; CREATE USER dbaguy FOR LOGIN dbaguy; ALTER ROLE db_owner ADD MEMBER dbaguy"],
        }[self.engine])

    def drop(self, name: str) -> None:
        assert name.startswith(SCRATCH_PREFIX), f"{name!r} is not a scratch database: never dropped here"
        self._run(*{
            "postgres": [f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'],
            # the grant create() gave goes with it (a grant outlives its database)
            "mysql": [f"DROP DATABASE IF EXISTS `{name}`",
                      f"REVOKE IF EXISTS ALL ON `{name}`.* FROM 'dbaguy'@'%'"],
            "mssql": [f"IF DB_ID(N'{name}') IS NOT NULL ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE",
                      f"DROP DATABASE IF EXISTS [{name}]"],
        }[self.engine])

    def run_in(self, name: str, *statements: str) -> None:
        """Set-up statements as the administrator, in database `name`."""
        self._run(*statements, database=name)

    def rows(self, database: str, sql: str) -> list[tuple]:
        """A read as the administrator, in database `database` (checks compare the source and the restore with it)."""
        return self._run(sql, database=database)

    def _run(self, *statements: str, database: str | None = None) -> list[tuple]:
        """The statements in order; the rows of the last one."""
        found: list[tuple] = []
        if self.engine == "postgres":
            with psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD,
                                 dbname=database or "postgres", autocommit=True) as conn:
                for statement in statements:
                    cursor = conn.execute(statement)
                    found = [tuple(row) for row in cursor.fetchall()] if cursor.description else []
        elif self.engine == "mysql":
            conn = mysql.connector.connect(**mysql_driver_kwargs(user="root", password="dbaguy", autocommit=True,
                                                                 database=database or "mysql"))
            try:
                for statement in statements:
                    cursor = conn.cursor()
                    cursor.execute(statement)
                    found = [tuple(row) for row in cursor.fetchall()] if cursor.with_rows else []
                    cursor.close()
            finally:
                conn.close()
        else:
            conn = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database=database or "master")
            try:
                for statement in statements:
                    conn.execute_query(statement)
                    found = [tuple(row) for row in conn.get_iterator(_ROW_TUPLE)]
                    while conn.nextresult():
                        for _ in conn.get_iterator(_ROW_TUPLE):
                            pass
            finally:
                conn.close()
        return found


@contextmanager
def scratch_database(engine: str, kind: str = "db") -> Iterator[tuple[str, "_Admin"]]:
    """A database `zz_06_<kind>_<random>` the dev login may use (PostgreSQL, MySQL, SQL Server), dropped by its exact
    name when the block ends, whatever happened."""
    admin = _Admin(engine)
    name = scratch_name(kind)
    admin.create(name)
    try:
        yield name, admin
    finally:
        admin.drop(name)


@dataclass
class Ran:
    """One job's events, from its `started` to its `finished`."""

    events: list

    @property
    def types(self) -> list[str]:
        return [event.type for event in self.events]

    @property
    def outcome(self) -> JobOutcomeEvent:
        [event] = [event for event in self.events if event.type == "job_outcome"]  # exactly one per job
        return event

    @property
    def error(self) -> ErrorEvent | None:
        return next((event for event in self.events if event.type == "error"), None)

    @property
    def messages(self) -> list[str]:
        return [event.text for event in self.events if event.type == "message"]

    @property
    def progress(self) -> list[JobProgressEvent]:
        return [event for event in self.events if event.type == "job_progress"]

    @property
    def finished(self) -> FinishedEvent:
        return self.events[-1]


class JobTab:
    """A job's session: its worker, with the events it sent."""

    def __init__(self, worker: SessionWorker, recorder: Recorder, session_id: str) -> None:
        self.worker = worker
        self.recorder = recorder
        self.session_id = session_id
        self._ids = itertools.count(1)

    def submit(self, make: Callable[[str], JobCommand]) -> str:
        """`make(request_id)` builds the command."""
        rid = f"job{next(self._ids)}"
        self.worker.submit(make(rid))
        return rid

    def run(self, make: Callable[[str], JobCommand], timeout: float = 120.0) -> Ran:
        return self.wait(self.submit(make), timeout)

    def wait(self, rid: str, timeout: float = 120.0) -> Ran:
        return Ran(request_events(self.recorder, rid, timeout))

    def wait_for_progress(self, rid: str, test: Callable[[JobProgressEvent], bool], timeout: float = 60.0) -> None:
        self.recorder.wait_for(lambda e: e.type == "job_progress" and e.request_id == rid and test(e), timeout)


class DataApp:
    """The backend in-process — an AppContainer over a temporary data dir with secrets in memory — so a test runs jobs
    on sessions with their real hooks and adapters."""

    def __init__(self, directory: Path) -> None:
        settings = Settings(token="data", data_dir=directory / "data", log_to_file=False)
        self.container = AppContainer(settings, secret_store=InMemorySecretStore())

    def add_profile(self, body: dict) -> UUID:
        return self.container.profiles.create(ProfileUpsertRequest.model_validate(body)).profile.id

    def inspect(self, profile_id: UUID, path: Path) -> FileInspection:
        return self.container.data.inspect(profile_id, str(path))

    def job_session(self, profile_id: UUID, database: str | None = None) -> JobTab:
        info = self.container.sessions.create(SessionCreateRequest(profile_id=profile_id, database=database))
        recorder = Recorder()
        return JobTab(self.container.sessions.attach(info.session_id, recorder), recorder, info.session_id)

    def history(self) -> list[HistoryEntry]:
        return self.container.history.search().items

    def close_sessions(self) -> None:
        self.container.sessions.close_all()

    def close(self) -> None:
        self.container.close()
