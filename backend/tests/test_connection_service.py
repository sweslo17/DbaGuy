import sqlite3

from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.domain.profiles import ConnectionTestRequest, ProfileSecretsInput
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.support import BASTION_PORT, PG_PASSWORD, PG_PORT, requires_bastion, requires_pg
from tests.test_ssh_tunnel import TEST_SSH_OPTIONS


def make_service(tmp_path) -> ConnectionService:
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    tunnels = SshTunnelManager(write_askpass_script(tmp_path), extra_options=TEST_SSH_OPTIONS)
    return ConnectionService(AdapterRegistry([PostgresAdapter(), SqliteAdapter()]), profiles, tunnels)


def test_sqlite_ok_and_missing(tmp_path):
    db = tmp_path / "a.db"
    sqlite3.connect(db).close()
    service = make_service(tmp_path)
    ok = service.test(ConnectionTestRequest.model_validate({"profile": {"engine": "sqlite", "name": "a", "path": str(db)}}))
    assert ok.ok and ok.server_version
    missing = service.test(
        ConnectionTestRequest.model_validate({"profile": {"engine": "sqlite", "name": "b", "path": str(tmp_path / "nope.db")}})
    )
    assert not missing.ok and "not found" in missing.message


@requires_pg
def test_postgres_wrong_password_reports_failure(tmp_path):
    request = ConnectionTestRequest.model_validate({
        "profile": {"engine": "postgres", "name": "pg", "host": "127.0.0.1", "port": PG_PORT,
                    "database": "dbaguy", "username": "dbaguy", "ssl": {"mode": "disable"}},
        "secrets": {"password": "wrong"},
    })
    result = make_service(tmp_path).test(request)
    assert not result.ok and "password authentication failed" in result.message


@requires_bastion
def test_postgres_through_ssh_bastion(tmp_path):
    request = ConnectionTestRequest(
        profile={
            "engine": "postgres", "name": "pg via ssh", "host": "postgres", "port": 5432, "database": "dbaguy",
            "username": "dbaguy", "ssl": {"mode": "disable"},
            "ssh": {"enabled": True, "host": "127.0.0.1", "port": BASTION_PORT, "user": "dev", "auth": "password"},
        },
        secrets=ProfileSecretsInput(password=PG_PASSWORD, ssh_password="dev"),
    )
    service = make_service(tmp_path)
    result = service.test(request)
    assert result.ok, result.message
    assert service._tunnels.active_count() == 0  # released after the test
