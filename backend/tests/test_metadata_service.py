import psycopg
import pytest

from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.domain.errors import QueryFailed
from dbaguy_backend.domain.metadata import MetadataRequest, PathSegment
from dbaguy_backend.domain.profiles import ProfileSecretsInput, ProfileUpsertRequest
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.metadata_service import MetadataService
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.support import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, requires_pg

pytestmark = requires_pg


@pytest.fixture
def service_and_profile(tmp_path):
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    entry = profiles.create(ProfileUpsertRequest.model_validate({
        "profile": {"engine": "postgres", "name": "pg", "host": PG_HOST, "port": PG_PORT, "database": "dbaguy",
                    "username": PG_USER, "ssl": {"mode": "disable"}},
        "secrets": ProfileSecretsInput(password=PG_PASSWORD).model_dump(),
    }))
    registry = AdapterRegistry([PostgresAdapter()])
    connections = ConnectionService(registry, profiles, SshTunnelManager(write_askpass_script(tmp_path)))
    service = MetadataService(registry, connections, profiles)
    yield service, entry.profile.id
    service.close_all()


def seg(kind, name):
    return PathSegment(kind=kind, name=name)


def test_routes_each_database_to_its_own_connection(service_and_profile):
    service, pid = service_and_profile
    databases = [n.name for n in service.children(MetadataRequest(profile_id=pid))]
    assert "analytics" in databases
    path = [seg("database", "analytics"), seg("schema", "public"), seg("folder", "tables")]
    assert [n.name for n in service.children(MetadataRequest(profile_id=pid, path=path))] == ["page_views"]


def test_reconnects_after_server_drops_connection(service_and_profile):
    service, pid = service_and_profile
    request = MetadataRequest(profile_id=pid, path=[seg("database", "dbaguy")])
    assert service.children(request)
    with psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy",
                         autocommit=True, application_name="pytest-admin") as admin:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'DbaGuy'")
    assert service.children(request)  # transparently reconnected


def test_without_retry_a_failure_is_raised_once_and_never_sent_again(service_and_profile):  # D-04-9, ruling C26
    service, pid = service_and_profile
    calls = []

    def failing(connection):
        calls.append(connection)
        raise RuntimeError("the answer was lost")

    with pytest.raises(QueryFailed):
        service.with_connection(pid, "dbaguy", failing, retry=False)
    assert len(calls) == 1
    with pytest.raises(QueryFailed):
        service.with_connection(pid, "dbaguy", failing)
    assert len(calls) == 3  # the default reconnects once and runs it again
