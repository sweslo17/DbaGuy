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
from loguru import logger

from dbaguy_backend.adapters.base import StatementError
from dbaguy_backend.adapters.mysql.catalog import file_granted
from dbaguy_backend.agent.login import LoginRegistry
from dbaguy_backend.app import create_app
from dbaguy_backend.domain.agent import LoginPrivileges
from dbaguy_backend.domain.errors import ConnectionFailed, QueryFailed
from dbaguy_backend.domain.profiles import ProfileUpsertRequest, SqliteProfile, SslConfig

from .agent_support import AUTH, container, share
from .support import (
    MSSQL_SA_PASSWORD, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, make_mssql_profile, make_mysql_profile, make_pg_profile,
    mssql_connect, mysql_driver_kwargs, requires_mssql, requires_mysql, requires_pg,
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


@requires_pg
def test_postgres_superuser_holds_every_role_and_list_connections_shows_it_once_read(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"), access="schema")
    assert container.agent_tools.list_connections()["connections"][0]["login"] is None  # nothing connected yet
    container.agent_tools.list_objects(pid)  # the first catalog call reads it
    login = container.agent_tools.list_connections()["connections"][0]["login"]
    assert login["grants"] == ["superuser", "server_files_read", "server_files_write", "server_programs",
                               "signal_backends"]
    assert login["summary"].startswith("is a superuser")


@requires_pg
def test_postgres_roles_count_through_a_chain_of_roles_and_a_plain_login_has_none(container):
    admin = psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy",
                            autocommit=True)
    roles = ("zz_08_t3_pa", "zz_08_t3_pb", "zz_08_t3_pc")  # exact names, dropped in `finally`

    def drop() -> None:
        for role in roles:
            if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                admin.execute(f"DROP OWNED BY {role}")
                admin.execute(f"DROP ROLE {role}")

    def granted(role: str) -> list[str]:
        draft = make_pg_profile(name=f"zz_08 {role}", username=role).model_dump(exclude={"id"})
        request = ProfileUpsertRequest.model_validate({"profile": draft | {"mcp_access": "schema"},
                                                       "secrets": {"password": "zz08t3pw"}})
        try:
            return _login(container, str(container.profiles.create(request).profile.id)).grants
        finally:
            container.metadata.close_all()

    drop()
    try:
        admin.execute("CREATE ROLE zz_08_t3_pb NOLOGIN IN ROLE pg_read_server_files, pg_signal_backend")
        admin.execute("CREATE ROLE zz_08_t3_pa LOGIN PASSWORD 'zz08t3pw' IN ROLE zz_08_t3_pb")  # a -> b -> pg_*
        admin.execute("CREATE ROLE zz_08_t3_pc LOGIN PASSWORD 'zz08t3pw'")
        assert granted("zz_08_t3_pa") == ["server_files_read", "signal_backends"]
        assert granted("zz_08_t3_pc") == []
    finally:
        drop()
        admin.close()


@requires_mysql
def test_mysql_file_through_an_active_role_is_seen(container):
    root = mysql.connector.connect(**mysql_driver_kwargs(user="root", database=None, autocommit=True))
    cursor = root.cursor()
    try:
        for sql in ("DROP USER IF EXISTS 'zz_08_u'@'%'", "DROP ROLE IF EXISTS 'zz_08_r'", "CREATE ROLE 'zz_08_r'",
                    "GRANT FILE ON *.* TO 'zz_08_r'", "CREATE USER 'zz_08_u'@'%' IDENTIFIED BY 'zz08pw'",
                    "GRANT 'zz_08_r' TO 'zz_08_u'@'%'", "SET DEFAULT ROLE ALL TO 'zz_08_u'@'%'",
                    "GRANT SELECT ON dbaguy.* TO 'zz_08_u'@'%'"):
            cursor.execute(sql)
        plain = share(container, make_mysql_profile(name="zz_08 mysql"), access="schema")
        assert _login(container, plain) == LoginPrivileges()
        as_root = share(container, make_mysql_profile(name="zz_08 root", username="root"), access="schema")
        assert _login(container, as_root) == LoginPrivileges(grants=["file"], secure_file_priv="/var/lib/mysql-files/")
        # TLS: a new caching_sha2_password account's first login needs it (R-03a-P5)
        draft = make_mysql_profile(name="zz_08 role", username="zz_08_u",
                                   ssl=SslConfig(mode="require")).model_dump(exclude={"id"})
        request = ProfileUpsertRequest.model_validate({"profile": draft | {"mcp_access": "schema"},
                                                       "secrets": {"password": "zz08pw"}})
        role = str(container.profiles.create(request).profile.id)
        assert _login(container, role).grants == ["file"]
    finally:
        cursor.execute("DROP USER IF EXISTS 'zz_08_u'@'%'")
        cursor.execute("DROP ROLE IF EXISTS 'zz_08_r'")
        root.close()


@requires_mssql
def test_sql_server_says_the_login_can_write_and_its_server_roles(container):
    owner = share(container, make_mssql_profile(name="zz_08 mssql"), access="schema")
    assert _login(container, owner).grants == ["can_write"]  # dbaguy is db_owner (T0)
    draft = make_mssql_profile(name="zz_08 sa", username="sa").model_dump(exclude={"id"})
    request = ProfileUpsertRequest.model_validate({"profile": draft | {"mcp_access": "schema"},
                                                   "secrets": {"password": MSSQL_SA_PASSWORD}})
    sa = str(container.profiles.create(request).profile.id)
    assert _login(container, sa).grants == ["can_write", "sysadmin", "bulkadmin"]


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
    container.agent_login._known[UUID(pid)] = LoginPrivileges(grants=["file"])
    with TestClient(create_app(container.settings, container), base_url="http://127.0.0.1") as client:
        body = {"profile": {"engine": "sqlite", "name": "lite", "path": str(path), "mcp_access": "query"}}
        assert client.put(f"/profiles/{pid}", json=body, headers=AUTH).status_code == 200
    assert container.agent_login.peek(UUID(pid)) is None


# ---- the registry itself: what is read when, what a failure does (no database needed) ----

@pytest.fixture
def caplog_loguru():
    """[(level, message)] of what is logged at INFO and above while the test runs."""
    records: list[tuple[str, str]] = []
    sink = logger.add(lambda m: records.append((m.record["level"].name, m.record["message"])), level="INFO")
    try:
        yield records
    finally:
        logger.remove(sink)


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
    registry = LoginRegistry(metadata=None)
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


@pytest.mark.parametrize("failure", [StatementError("permission denied for table x", "42501"),
                                     QueryFailed("the server said no")])
def test_a_query_that_fails_is_logged_by_its_code_and_tried_again_later(failure, caplog_loguru):
    registry = LoginRegistry(metadata=None)
    read = _reads(failure, LoginPrivileges())
    registry.remember(_LITE, read)  # does not raise: the call it belongs to goes on
    assert registry.peek(_LITE.id) is None
    warnings = [m for level, m in caplog_loguru if level == "WARNING"]
    assert len(warnings) == 1 and "could not be read" in warnings[0] and "lite" in warnings[0]
    assert "permission denied" not in warnings[0] and "the server said no" not in warnings[0]  # a code, never text
    registry.remember(_LITE, read)
    assert len(read.calls) == 2 and registry.peek(_LITE.id) == LoginPrivileges()


def test_privileges_read_while_the_profile_changed_are_not_kept_for_the_new_login():
    registry = LoginRegistry(metadata=None)

    def saved_while_reading():
        registry.forget(_LITE.id)  # PUT /profiles/{id} arrives between the query and its answer
        return LoginPrivileges(grants=["superuser"])

    registry.remember(_LITE, saved_while_reading)
    assert registry.peek(_LITE.id) is None  # the next call reads the new login
    registry.remember(_LITE, _reads(LoginPrivileges()))
    assert registry.peek(_LITE.id) == LoginPrivileges()


def test_a_connection_failure_is_the_callers():
    registry = LoginRegistry(metadata=None)
    with pytest.raises(ConnectionFailed):
        registry.remember(_LITE, _reads(ConnectionFailed("no route to host")))
    assert registry.peek(_LITE.id) is None


def test_the_summary_goes_to_the_log_at_info(caplog_loguru):
    LoginRegistry(metadata=None).remember(_LITE, _reads(LoginPrivileges(grants=["superuser", "server_programs"])))
    assert ("INFO", "MCP: the login of 'lite' can: is a superuser, run programs on the server") in caplog_loguru


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
                       "summary": "read and write files on the server (FILE)"}


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
    ("GRANT CONTROL SERVER TO zz_08_t3_l", ["can_write", "sysadmin", "bulkadmin"]),  # sysadmin in all but the name
    ("GRANT ADMINISTER BULK OPERATIONS TO zz_08_t3_l", ["bulkadmin"]),  # without the server role
    ("ALTER SERVER ROLE bulkadmin ADD MEMBER zz_08_t3_l", ["bulkadmin"]),
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
