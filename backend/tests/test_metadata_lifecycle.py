"""MetadataService cleanup paths, with fake workers/connections (no database needed)."""

import threading
from uuid import uuid4

import pytest

from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.domain.errors import ConnectionFailed
from dbaguy_backend.domain.metadata import MetadataRequest
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.services.metadata_service import MetadataService, _MetaEntry
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.sessions.worker import SessionWorker
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.test_session_worker import GatedConnections


@pytest.fixture
def service_env(tmp_path):
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    entry = profiles.create(ProfileUpsertRequest.model_validate(
        {"profile": {"engine": "sqlite", "name": "meta", "path": str(tmp_path / "x.db")}}
    ))
    connections = GatedConnections()
    return MetadataService(AdapterRegistry([SqliteAdapter()]), connections, profiles), connections, entry.profile.id


def test_open_that_times_out_does_not_leak_the_connection(monkeypatch, service_env):
    service, connections, profile_id = service_env
    original = SessionWorker.start
    monkeypatch.setattr(SessionWorker, "start", lambda self, timeout=0.05: original(self, timeout))
    with pytest.raises(ConnectionFailed):
        service.children(MetadataRequest(profile_id=profile_id))
    connections.release.set()
    assert connections.lease.closed.wait(5), "the connection opened after the timeout was never closed"
    assert service._workers == {}


def test_worker_is_closed_when_start_fails_for_any_reason(monkeypatch, service_env):
    service, connections, profile_id = service_env
    connections.release.set()
    original = SessionWorker.start

    def start_then_blow_up(self, timeout=30.0):
        original(self, timeout)
        raise RuntimeError("boom")

    monkeypatch.setattr(SessionWorker, "start", start_then_blow_up)
    with pytest.raises(RuntimeError, match="boom"):
        service.children(MetadataRequest(profile_id=profile_id))
    assert connections.lease.closed.wait(5)
    assert service._workers == {}


class ExplodingWorker:
    def __init__(self) -> None:
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()
        raise OSError("boom")


def test_close_all_reaches_every_connection_even_if_one_close_fails(service_env):
    service, *_ = service_env
    workers = [ExplodingWorker() for _ in range(3)]
    service._workers = {(uuid4(), None): _MetaEntry(worker=w, last_used=0.0) for w in workers}
    service.close_all()  # must not raise
    assert all(w.closed.is_set() for w in workers)
    assert service._workers == {}
