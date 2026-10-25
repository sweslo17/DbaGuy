"""The login's privileges beyond reading (spec ⑧ CR-08-1, §3.3; A15): read once by an agent's connection, shown by
list_connections and the connection dialog's route. The MySQL role and user are zz_08_*, dropped by exact name."""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import mysql.connector
import psycopg
import pytest
from fastapi.testclient import TestClient

from dbaguy_backend.adapters.base import StatementError
from dbaguy_backend.adapters.mysql.catalog import file_granted
from dbaguy_backend.agent.login import LoginRegistry
from dbaguy_backend.app import create_app
from dbaguy_backend.container import default_registry
from dbaguy_backend.domain.agent import LoginPrivileges
from dbaguy_backend.domain.errors import ConnectionFailed, QueryFailed
from dbaguy_backend.domain.profiles import ProfileUpsertRequest, SqliteProfile, SslConfig

from .agent_support import AUTH, container, logged, share
from .support import (
    MSSQL_SA_PASSWORD, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, make_couchbase_profile, make_mssql_profile,
    make_mysql_profile, make_pg_profile, mssql_connect, mysql_driver_kwargs, requires_mssql, requires_mysql,
    requires_pg,
)

__all__ = ["container"]


def _login(container, pid: str) -> LoginPrivileges | None:
    profile = container.profiles.get(UUID(pid))
    return container.agent_login.read(profile)


@pytest.mark.parametrize(("lines", "granted"), [
    (["GRANT FILE ON *.* TO `u`@`%`"], True),
    (["GRANT SELECT, INSERT, FILE, REFERENCES ON *.* TO `root`@`%` WITH GRANT OPTION"], True),
    (["GRANT ALL PRIVILEGES ON *.* TO `u`@`%`"], True),
    (["GRANT USAGE ON *.* TO `u`@`%`", "GRANT ALL PRIVILEGES ON `dbaguy`.* TO `u`@`%`"], False),
    (["GRANT SELECT, PROCESS ON *.* TO `u`@`%`", "GRANT `r`@`%` TO `u`@`%`"], False),
    (["GRANT SELECT ON `fileserver`.* TO `u`@`%`"], False),
])
def test_mysql_file_comes_from_a_global_grant_by_name_or_by_all(lines, granted):
    assert file_granted(lines) is granted


def test_the_summary_says_what_the_login_can_do():
    assert LoginPrivileges().summary() == "nothing beyond reading"
    assert LoginPrivileges(grants=["superuser", "server_programs"]).summary() == (
        "is a superuser, run programs on the server")


def test_replication_says_it_manages_slots():  # final review I1
    assert LoginPrivileges(grants=["superuser", "replication"]).summary() == (
        "is a superuser, manage replication slots (REPLICATION)")


@pytest.mark.parametrize(("directory", "text"), [
    ("/var/lib/mysql-files/", "read and write files on the server in '/var/lib/mysql-files/' (FILE)"),
    # T3 N3: the server's own text, quoted and bounded (it reaches the INFO line)
    ("/srv/a\nMCP run_query on x: ok",
     "read and write files on the server in '/srv/a\\nMCP run_query on x: ok' (FILE)"),
    ("/" + "d" * 400, f"read and write files on the server in '/{'d' * 199}…' (FILE)"),
    ("", "read and write files on the server in any directory (FILE)"),
    (None, "has FILE, but the server allows no file access (secure_file_priv is NULL)"),  # NULL: no file at all
])
def test_the_summary_of_file_names_the_directory_the_server_allows(directory, text):
    assert LoginPrivileges(grants=["file"], secure_file_priv=directory).summary() == text


@requires_pg
def test_postgres_superuser_holds_every_role_and_list_connections_shows_it_once_read(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"), access="schema")
    assert container.agent_tools.list_connections()["connections"][0]["login"] is None  # nothing connected yet
    container.agent_tools.list_objects(pid)  # the first catalog call reads it
    login = container.agent_tools.list_connections()["connections"][0]["login"]
    assert login["grants"] == ["superuser", "server_files_read", "server_files_write", "server_programs",
                               "signal_backends", "replication"]
    assert login["summary"].startswith("is a superuser")


# exact names, dropped in `finally`
_PG_ROLES = ("zz_08_t3_pa", "zz_08_t3_pb", "zz_08_t3_pc", "zz_08_t3_pf", "zz_08_ff_repl")


@contextmanager
def _pg_scratch_roles(*statements: str) -> Iterator[None]:
    """The scratch roles zz_08_t3_p* that `statements` create (and grant), dropped by exact name afterwards."""
    admin = psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy",
                            autocommit=True)

    def drop() -> None:
        for role in _PG_ROLES:
            if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                admin.execute(f"DROP OWNED BY {role}")
                admin.execute(f"DROP ROLE {role}")

    drop()
    try:
        for statement in statements:
            admin.execute(statement)
        yield
    finally:
        drop()
        admin.close()


def _pg_granted(container, role: str) -> list[str]:
    draft = make_pg_profile(name=f"zz_08 {role}", username=role).model_dump(exclude={"id"})
    request = ProfileUpsertRequest.model_validate({"profile": draft | {"mcp_access": "schema"},
                                                   "secrets": {"password": "zz08t3pw"}})
    try:
        return _login(container, str(container.profiles.create(request).profile.id)).grants
    finally:
        container.metadata.close_all()  # the role cannot be dropped while a session of it is open


@requires_pg
def test_a_postgres_replication_login_says_so(container):
    """Final review I1: REPLICATION is all pg_copy_*_replication_slot and pg_logical_slot_get_* need."""
    with _pg_scratch_roles("CREATE ROLE zz_08_ff_repl LOGIN REPLICATION PASSWORD 'zz08t3pw'"):
        assert _pg_granted(container, "zz_08_ff_repl") == ["replication"]


@requires_pg
def test_postgres_roles_count_through_a_chain_of_roles_and_a_plain_login_has_none(container):
    with _pg_scratch_roles("CREATE ROLE zz_08_t3_pb NOLOGIN IN ROLE pg_read_server_files, pg_signal_backend",
                           "CREATE ROLE zz_08_t3_pa LOGIN PASSWORD 'zz08t3pw' IN ROLE zz_08_t3_pb",  # a -> b -> pg_*
                           "CREATE ROLE zz_08_t3_pc LOGIN PASSWORD 'zz08t3pw'"):
        assert _pg_granted(container, "zz_08_t3_pa") == ["server_files_read", "signal_backends"]
        assert _pg_granted(container, "zz_08_t3_pc") == []


@requires_pg
@pytest.mark.parametrize(("function", "expected"), [
    ("lo_import(text)", ["server_files_read"]),  # a direct EXECUTE, without the pg_read_server_files role
    ("pg_read_file(text)", ["server_files_read"]),  # (inside the data directory: probed on 17)
    ("pg_read_binary_file(text)", ["server_files_read"]),
    ("pg_ls_dir(text)", ["server_files_read"]),
    ("pg_stat_file(text)", ["server_files_read"]),
    ("lo_export(oid, text)", ["server_files_write"]),
])
def test_postgres_a_direct_execute_grant_on_a_file_function_counts_as_file_rights(container, function, expected):
    with _pg_scratch_roles("CREATE ROLE zz_08_t3_pf LOGIN PASSWORD 'zz08t3pw'",
                           f"GRANT EXECUTE ON FUNCTION {function} TO zz_08_t3_pf"):
        assert _pg_granted(container, "zz_08_t3_pf") == expected


@requires_mysql
def test_mysql_file_through_an_active_role_is_seen(container):
    user, role = "zz_08_t3_mu", "zz_08_t3_mr"  # exact names, dropped in `finally`
    root = mysql.connector.connect(**mysql_driver_kwargs(user="root", database=None, autocommit=True))
    cursor = root.cursor()
    try:
        for sql in (f"DROP USER IF EXISTS '{user}'@'%'", f"DROP ROLE IF EXISTS '{role}'", f"CREATE ROLE '{role}'",
                    f"GRANT FILE ON *.* TO '{role}'", f"CREATE USER '{user}'@'%' IDENTIFIED BY 'zz08pw'",
                    f"GRANT '{role}' TO '{user}'@'%'", f"SET DEFAULT ROLE ALL TO '{user}'@'%'",
                    f"GRANT SELECT ON dbaguy.* TO '{user}'@'%'"):
            cursor.execute(sql)
        plain = share(container, make_mysql_profile(name="zz_08 mysql"), access="schema")
        assert _login(container, plain) == LoginPrivileges()
        as_root = share(container, make_mysql_profile(name="zz_08 root", username="root"), access="schema")
        assert _login(container, as_root) == LoginPrivileges(grants=["file"], secure_file_priv="/var/lib/mysql-files/")
        # TLS: a new caching_sha2_password account's first login needs it (R-03a-P5)
        draft = make_mysql_profile(name="zz_08 role", username=user,
                                   ssl=SslConfig(mode="require")).model_dump(exclude={"id"})
        request = ProfileUpsertRequest.model_validate({"profile": draft | {"mcp_access": "schema"},
                                                       "secrets": {"password": "zz08pw"}})
        through_the_role = str(container.profiles.create(request).profile.id)
        assert _login(container, through_the_role).grants == ["file"]
    finally:
        cursor.execute(f"DROP USER IF EXISTS '{user}'@'%'")
        cursor.execute(f"DROP ROLE IF EXISTS '{role}'")
        root.close()


@requires_mssql
def test_sql_server_says_the_login_can_write_and_its_server_roles(container):
    owner = share(container, make_mssql_profile(name="zz_08 mssql"), access="schema")
    assert _login(container, owner).grants == ["can_write"]  # dbaguy is db_owner (T0)
    draft = make_mssql_profile(name="zz_08 sa", username="sa").model_dump(exclude={"id"})
    request = ProfileUpsertRequest.model_validate({"profile": draft | {"mcp_access": "schema"},
                                                   "secrets": {"password": MSSQL_SA_PASSWORD}})
    sa = str(container.profiles.create(request).profile.id)
    assert _login(container, sa).grants == ["can_write", "sysadmin", "bulkadmin", "view_server_state"]


def test_the_dialogs_route_reads_it_and_a_saved_profile_forgets_it(container, tmp_path):
    path = tmp_path / "zz_08.db"
    sqlite3.connect(path).close()
    pid = share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000009", engine="sqlite", name="lite",
                                         path=str(path)), access="off")
    with TestClient(create_app(container.settings, container), base_url="http://127.0.0.1") as client:
        answer = client.get(f"/mcp/connections/{pid}/login", headers=AUTH)
        assert answer.status_code == 200 and answer.json() is None  # SQLite: nothing to tell
        missing = client.get("/mcp/connections/00000000-0000-0000-0000-00000000000a/login", headers=AUTH)
        assert missing.status_code == 404
    # The route kept SQLite's None: forget it first, so the remembered grant is really held before the save
    container.agent_login.forget(UUID(pid))
    container.agent_login.remember(container.profiles.get(UUID(pid)), lambda: LoginPrivileges(grants=["file"]))
    assert container.agent_login.peek(UUID(pid)) == LoginPrivileges(grants=["file"])
    with TestClient(create_app(container.settings, container), base_url="http://127.0.0.1") as client:
        body = {"profile": {"engine": "sqlite", "name": "lite", "path": str(path), "mcp_access": "query"}}
        assert client.put(f"/profiles/{pid}", json=body, headers=AUTH).status_code == 200
    assert container.agent_login.peek(UUID(pid)) is None


def test_the_dialogs_route_answers_null_for_sqlite_and_couchbase_without_connecting(container, tmp_path):
    path = tmp_path / "zz_08.db"
    sqlite3.connect(path).close()
    lite = share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000009", engine="sqlite", name="lite",
                                          path=str(path)), access="off")
    couchbase = share(container, make_couchbase_profile(name="zz_08 couchbase", host="127.0.0.1", port=1),
                      access="query")  # nothing listens on port 1: a connection would answer 502
    with TestClient(create_app(container.settings, container), base_url="http://127.0.0.1") as client:
        for pid in (lite, couchbase):
            answer = client.get(f"/mcp/connections/{pid}/login", headers=AUTH)
            assert answer.status_code == 200 and answer.json() is None
    assert not container.metadata._workers


# ---- the registry itself: what is read when, what a failure does (no database needed) ----

_LITE = SqliteProfile(id="00000000-0000-0000-0000-000000000009", engine="sqlite", name="lite", path="/tmp/zz_08.db")


def _reads(*answers):
    """A `read` that answers (or raises) the next of `answers` each time it is called, and counts the calls."""
    calls: list[int] = []

    def read():
        calls.append(1)
        answer = answers[min(len(calls), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    read.calls = calls
    return read


def test_the_privileges_are_read_once_and_nothing_to_tell_is_remembered_too():
    registry = LoginRegistry(None, None)
    read = _reads(LoginPrivileges(grants=["file"]))
    registry.remember(_LITE, read)
    registry.remember(_LITE, read)
    assert len(read.calls) == 1 and registry.peek(_LITE.id) == LoginPrivileges(grants=["file"])
    registry.forget(_LITE.id)
    assert registry.peek(_LITE.id) is None
    nothing = _reads(None)  # SQLite, Couchbase: asked once, not on every call
    registry.remember(_LITE, nothing)
    registry.remember(_LITE, nothing)
    assert len(nothing.calls) == 1 and registry.peek(_LITE.id) is None


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize("failure", [StatementError("permission denied for table x", "42501"),
                                     QueryFailed("the server said no")])
def test_a_query_that_fails_is_logged_by_its_code_and_tried_again_after_a_minute(failure):
    clock = _Clock()
    registry = LoginRegistry(None, None, clock=clock)
    read = _reads(failure, LoginPrivileges())
    with logged() as records:
        registry.remember(_LITE, read)  # does not raise: the call it belongs to goes on
        assert registry.peek(_LITE.id) is None
        clock.now += 59
        registry.remember(_LITE, read)  # a schema tool's next calls: no second query, no second warning
        registry.remember(_LITE, read)
        assert len(read.calls) == 1
        clock.now += 2
        registry.remember(_LITE, read)
    warnings = [m for level, m in records if level == "WARNING"]
    assert len(warnings) == 1 and "could not be read" in warnings[0] and str(_LITE.id) in warnings[0]  # M8: its id
    assert "permission denied" not in warnings[0] and "the server said no" not in warnings[0]  # a code, never text
    assert len(read.calls) == 2 and registry.peek(_LITE.id) == LoginPrivileges()


def test_a_saved_profile_is_read_again_at_once_after_a_failed_read():
    registry = LoginRegistry(None, None, clock=_Clock())
    read = _reads(StatementError("x", "42883"), LoginPrivileges())
    registry.remember(_LITE, read)
    registry.forget(_LITE.id)  # the login may be another now
    registry.remember(_LITE, read)
    assert len(read.calls) == 2 and registry.peek(_LITE.id) == LoginPrivileges()


def test_a_failure_of_a_read_that_began_before_the_profile_changed_is_not_held_against_the_new_login():
    registry = LoginRegistry(None, None, clock=_Clock())

    def saved_while_failing():
        registry.forget(_LITE.id)
        raise StatementError("x", "42883")

    registry.remember(_LITE, saved_while_failing)
    read = _reads(LoginPrivileges())
    registry.remember(_LITE, read)
    assert len(read.calls) == 1 and registry.peek(_LITE.id) == LoginPrivileges()


class _Connection:
    def login_privileges(self) -> LoginPrivileges:
        return LoginPrivileges(grants=["superuser"])


class _Metadata:
    """What `with_connection` is asked, answered by a connection that says superuser: no database is needed."""

    def __init__(self) -> None:
        self.asked: list[tuple[UUID, str | None, bool]] = []

    def with_connection(self, profile_id, database, fn, *, agent=False):
        self.asked.append((profile_id, database, agent))
        return fn(_Connection())


def test_an_engine_with_nothing_to_tell_is_never_connected_to_and_the_others_read_on_their_own_database():
    metadata = _Metadata()
    registry = LoginRegistry(metadata, default_registry())
    couchbase = make_couchbase_profile(host="127.0.0.1", port=1)  # nothing listens there
    for profile in (_LITE, couchbase):
        assert registry.read(profile) is None
        registry.ensure(profile)
        assert registry.peek(profile.id) is None
    assert metadata.asked == []
    postgres = make_pg_profile()
    assert registry.read(postgres) == LoginPrivileges(grants=["superuser"])
    assert metadata.asked == [(postgres.id, "dbaguy", True)]  # the agents' connection of the profile's own database


def test_privileges_read_while_the_profile_changed_are_not_kept_for_the_new_login():
    registry = LoginRegistry(None, None)

    def saved_while_reading():
        registry.forget(_LITE.id)  # PUT /profiles/{id} arrives between the query and its answer
        return LoginPrivileges(grants=["superuser"])

    registry.remember(_LITE, saved_while_reading)
    assert registry.peek(_LITE.id) is None  # the next call reads the new login
    registry.remember(_LITE, _reads(LoginPrivileges()))
    assert registry.peek(_LITE.id) == LoginPrivileges()


def test_a_connection_failure_is_the_callers():
    registry = LoginRegistry(None, None)
    with pytest.raises(ConnectionFailed):
        registry.remember(_LITE, _reads(ConnectionFailed("no route to host")))
    assert registry.peek(_LITE.id) is None


def test_the_summary_goes_to_the_log_at_info():
    with logged() as records:
        LoginRegistry(None, None).remember(_LITE, _reads(LoginPrivileges(grants=["superuser", "server_programs"])))
    assert ("INFO", f"MCP: the login of {_LITE.id} can: is a superuser, run programs on the server") in records
    assert not [text for _, text in records if "'lite'" in text]  # M8: the profile's id, never its name


def test_a_failed_read_is_logged_by_the_profiles_id():
    with logged() as records:
        LoginRegistry(None, None).remember(_LITE, _reads(QueryFailed("permission denied for 'lite'")))
    assert ("WARNING", f"MCP: the login of {_LITE.id} could not be read (query_failed)") in records


def test_list_connections_says_nothing_beyond_reading_as_something_and_not_as_null(container, tmp_path):
    path = tmp_path / "zz_08.db"
    sqlite3.connect(path).close()
    pid = share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000009", engine="sqlite", name="lite",
                                         path=str(path)), access="schema")
    profile = container.profiles.get(UUID(pid))
    entry = lambda: container.agent_tools.list_connections()["connections"][0]["login"]  # noqa: E731
    assert entry() is None
    container.agent_login.remember(profile, lambda: LoginPrivileges())
    assert entry() == {"grants": [], "summary": "nothing beyond reading"}
    container.agent_login.forget(UUID(pid))
    container.agent_login.remember(profile, lambda: LoginPrivileges(grants=["file"], secure_file_priv=""))
    assert entry() == {"grants": ["file"], "secure_file_priv": "",  # "" is any directory: not the same as unset
                       "summary": "read and write files on the server in any directory (FILE)"}
    container.agent_login.forget(UUID(pid))
    container.agent_login.remember(profile, lambda: LoginPrivileges(grants=["file"], secure_file_priv="/srv/files/"))
    assert entry()["secure_file_priv"] == "/srv/files/" and "in '/srv/files/'" in entry()["summary"]
    container.agent_login.forget(UUID(pid))
    container.agent_login.remember(profile, lambda: LoginPrivileges(grants=["file"]))  # secure_file_priv NULL
    assert entry() == {"grants": ["file"], "secure_file_priv": None,  # NULL is no file access: kept, not left out
                       "summary": "has FILE, but the server allows no file access (secure_file_priv is NULL)"}


@requires_pg
def test_the_login_is_read_on_the_profiles_own_database_connection_and_no_other(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"), access="schema")
    profile = container.profiles.get(UUID(pid))
    container.agent_login.ensure(profile)
    assert set(container.metadata._workers) == {(UUID(pid), "dbaguy", True)}  # the key a schema tool of it uses too
    container.agent_tools.describe(pid, schema="sales", name="orders")
    assert set(container.metadata._workers) == {(UUID(pid), "dbaguy", True)}


@requires_pg
def test_a_locked_profile_answers_the_dialogs_route_with_428_and_connects_to_nothing(container):
    draft = make_pg_profile(name="zz_08 locked", save_password=False).model_dump(exclude={"id"})
    request = ProfileUpsertRequest.model_validate({"profile": draft | {"mcp_access": "schema"}, "secrets": {}})
    pid = str(container.profiles.create(request).profile.id)
    with TestClient(create_app(container.settings, container), base_url="http://127.0.0.1") as client:
        answer = client.get(f"/mcp/connections/{pid}/login", headers=AUTH)
    assert answer.status_code == 428 and answer.json()["error"]["code"] == "password_required"
    assert container.agent_login.peek(UUID(pid)) is None and not container.metadata._workers


# ---- SQL Server: what the login is granted beyond db_owner (ruling T3-1) ----

_MS_LOGIN, _MS_USER, _MS_PASSWORD = "zz_08_t3_l", "zz_08_t3_u", "Zz08_t3_Pw1!x"  # exact names, dropped in `finally`


def _ms_run(conn, sql: str) -> None:
    conn.execute_non_query(sql)


@contextmanager
def _sql_server_login(grants: list[str], *, server_grant: str | None = None) -> Iterator[None]:
    """The login `zz_08_t3_l` with a user in dbaguy, a table `dbo.zz_08_t3_t` with a procedure `dbo.zz_08_t3_p` that
    writes to it, and a schema `zz_08_t3_s` with a table: what the `grants` (run in dbaguy) are granted on. Everything
    is dropped by its exact name, whatever happened."""
    master = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database="master")
    db = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database="dbaguy")

    def drop() -> None:  # the schema before the user that may own it, the user before the roles it is in
        for sql in ("DROP PROCEDURE IF EXISTS dbo.zz_08_t3_p", "DROP TABLE IF EXISTS dbo.zz_08_t3_t",
                    "DROP TABLE IF EXISTS zz_08_t3_s.t2", "DROP SCHEMA IF EXISTS zz_08_t3_s",
                    f"IF DATABASE_PRINCIPAL_ID('{_MS_USER}') IS NOT NULL DROP USER {_MS_USER}",
                    "IF DATABASE_PRINCIPAL_ID('zz_08_t3_r1') IS NOT NULL DROP ROLE zz_08_t3_r1",
                    "IF DATABASE_PRINCIPAL_ID('zz_08_t3_r2') IS NOT NULL DROP ROLE zz_08_t3_r2"):
            _ms_run(db, sql)
        _ms_run(master, f"IF SUSER_ID('{_MS_LOGIN}') IS NOT NULL DROP LOGIN {_MS_LOGIN}")

    drop()
    try:
        _ms_run(master, f"CREATE LOGIN {_MS_LOGIN} WITH PASSWORD = '{_MS_PASSWORD}', CHECK_POLICY = OFF, "
                        "DEFAULT_DATABASE = dbaguy")
        if server_grant:
            _ms_run(master, server_grant)
        for sql in ("CREATE SCHEMA zz_08_t3_s", "CREATE TABLE dbo.zz_08_t3_t (id int PRIMARY KEY, c1 int)",
                    "CREATE TABLE zz_08_t3_s.t2 (id int PRIMARY KEY)",
                    "CREATE PROCEDURE dbo.zz_08_t3_p AS INSERT INTO dbo.zz_08_t3_t (id) VALUES (777)",
                    f"CREATE USER {_MS_USER} FOR LOGIN {_MS_LOGIN}", *grants):
            _ms_run(db, sql)
        yield
    finally:
        drop()
        master.close()
        db.close()


def _granted_by_the_login(container, database: str = "dbaguy") -> list[str]:
    draft = make_mssql_profile(name="zz_08 t3 login", username=_MS_LOGIN, database=database).model_dump(exclude={"id"})
    request = ProfileUpsertRequest.model_validate({"profile": draft | {"mcp_access": "schema"},
                                                   "secrets": {"password": _MS_PASSWORD}})
    try:
        return _login(container, str(container.profiles.create(request).profile.id)).grants
    finally:
        container.metadata.close_all()  # the login cannot be dropped while a session of it is open


@requires_mssql
@pytest.mark.parametrize(("grants", "expected"), [
    ([], []),  # the recommended read-only login, and a login that can only connect
    (["ALTER ROLE db_datareader ADD MEMBER zz_08_t3_u"], []),
    (["GRANT INSERT ON dbo.zz_08_t3_t TO zz_08_t3_u"], ["can_write"]),  # one object, to the user
    (["GRANT UPDATE (c1) ON dbo.zz_08_t3_t TO zz_08_t3_u"], ["can_write"]),  # one column
    (["GRANT EXECUTE ON dbo.zz_08_t3_p TO zz_08_t3_u"], ["can_write"]),  # a procedure that writes
    (["GRANT INSERT ON SCHEMA::zz_08_t3_s TO zz_08_t3_u"], ["can_write"]),
    (["GRANT ALTER ON dbo.zz_08_t3_t TO zz_08_t3_u"], ["can_write"]),
    (["GRANT CONTROL ON dbo.zz_08_t3_t TO zz_08_t3_u"], ["can_write"]),
    (["GRANT TAKE OWNERSHIP ON dbo.zz_08_t3_t TO zz_08_t3_u"], ["can_write"]),
    (["GRANT DELETE ON dbo.zz_08_t3_t TO public"], ["can_write"]),  # public is every user's
    (["CREATE ROLE zz_08_t3_r1", "GRANT UPDATE ON dbo.zz_08_t3_t TO zz_08_t3_r1",
      "ALTER ROLE zz_08_t3_r1 ADD MEMBER zz_08_t3_u"], ["can_write"]),  # through a role
    (["CREATE ROLE zz_08_t3_r1", "CREATE ROLE zz_08_t3_r2", "GRANT INSERT ON dbo.zz_08_t3_t TO zz_08_t3_r2",
      "ALTER ROLE zz_08_t3_r2 ADD MEMBER zz_08_t3_r1", "ALTER ROLE zz_08_t3_r1 ADD MEMBER zz_08_t3_u"],
     ["can_write"]),  # a role in a role
    (["ALTER ROLE db_ddladmin ADD MEMBER zz_08_t3_u"], ["can_write"]),  # DROP and ALTER of every table
    (["ALTER AUTHORIZATION ON SCHEMA::zz_08_t3_s TO zz_08_t3_u"], ["can_write"]),  # owns a schema
    (["GRANT INSERT ON dbo.zz_08_t3_t TO zz_08_t3_u", "DENY INSERT ON dbo.zz_08_t3_t TO zz_08_t3_u"], []),
])
def test_sql_server_can_write_counts_what_is_granted_on_objects_schemas_and_roles(container, grants, expected):
    with _sql_server_login(grants):
        assert _granted_by_the_login(container) == expected


@requires_mssql
@pytest.mark.parametrize(("server_grant", "expected"), [
    ("GRANT CONTROL SERVER TO zz_08_t3_l", ["can_write", "sysadmin", "bulkadmin", "view_server_state"]),  # sysadmin
    ("GRANT ADMINISTER BULK OPERATIONS TO zz_08_t3_l", ["bulkadmin"]),  # without the server role
    ("ALTER SERVER ROLE bulkadmin ADD MEMBER zz_08_t3_l", ["bulkadmin"]),
    # the registry, volume and service readers the gate leaves to the server (final fix wave, I2), and 2022's halves
    ("GRANT VIEW SERVER STATE TO zz_08_t3_l", ["view_server_state"]),
    ("GRANT VIEW SERVER SECURITY STATE TO zz_08_t3_l", ["view_server_state"]),
    ("GRANT VIEW SERVER PERFORMANCE STATE TO zz_08_t3_l", ["view_server_state"]),
])
def test_sql_server_server_level_rights_are_the_logins_effective_ones(container, server_grant, expected):
    with _sql_server_login([], server_grant=server_grant):
        assert _granted_by_the_login(container) == expected  # the login's drop takes its server grants with it


@requires_mssql
def test_sql_server_the_default_grants_of_master_to_public_are_not_the_logins(container):
    with _sql_server_login([]):  # no user in master: the login is guest there, and public holds 1,739 EXECUTE grants
        assert _granted_by_the_login(container, database="master") == []


@requires_mssql
def test_sql_server_reading_the_login_does_not_wait_for_another_sessions_uncommitted_grant(container):
    blocker = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database="dbaguy")
    answer: list[list[str]] = []
    with _sql_server_login(["ALTER ROLE db_datareader ADD MEMBER zz_08_t3_u"]):
        try:
            _ms_run(blocker, f"BEGIN TRAN; GRANT INSERT ON dbo.zz_08_t3_t TO {_MS_USER}")  # open for the whole read
            reader = threading.Thread(target=lambda: answer.append(_granted_by_the_login(container)), daemon=True)
            reader.start()
            reader.join(10)  # a plain read of sys.database_permissions waits for the GRANT to end
            assert not reader.is_alive(), "reading the login waits for another session's open GRANT"
        finally:
            _ms_run(blocker, "IF @@TRANCOUNT > 0 ROLLBACK")  # also lets a blocked reader finish
            blocker.close()
            reader.join(10)
