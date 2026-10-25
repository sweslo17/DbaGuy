"""The login's privileges beyond reading (spec ⑧ CR-08-1, §3.3; A15): read once by an agent's connection, shown by
list_connections and the connection dialog's route. The MySQL role and user are zz_08_*, dropped by exact name."""

import sqlite3
from uuid import UUID

import mysql.connector
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
    MSSQL_SA_PASSWORD, make_mssql_profile, make_mysql_profile, make_pg_profile, mysql_driver_kwargs,
    requires_mssql, requires_mysql, requires_pg,
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
