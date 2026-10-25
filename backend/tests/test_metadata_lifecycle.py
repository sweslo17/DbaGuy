"""MetadataService cleanup paths, with fake workers/connections (no database needed)."""

import threading
import time
from uuid import uuid4

import pytest

from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.domain.errors import ConnectionFailed, SessionBusy
from dbaguy_backend.domain.metadata import MetadataRequest
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.services import metadata_service
from dbaguy_backend.services.metadata_service import MetadataService, _MetaEntry
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.sessions.worker import SessionWorker
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.fake_db import FakeLease
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


# ---- the agents' catalog connections (spec ⑧ D-08-11; task 2 fix round 1) ----

class CountingConnections:
    """Stands in for ConnectionService: a new FakeLease per open, by (database, agent); `gate` holds an open back."""

    def __init__(self) -> None:
        self.leases: dict[tuple, FakeLease] = {}
        self.gate = threading.Event()
        self.gate.set()
        self.opening = threading.Event()

    def open(self, profile, database, secrets, *, agent=False):
        self.opening.set()
        assert self.gate.wait(10)
        lease = self.leases[(database, agent)] = FakeLease()
        return lease


@pytest.fixture
def agents_env(tmp_path):
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    entry = profiles.create(ProfileUpsertRequest.model_validate({
        "profile": {"engine": "postgres", "name": "pg", "host": "127.0.0.1", "database": "a"},
        "secrets": {"password": "x"},
    }))
    connections = CountingConnections()
    service = MetadataService(AdapterRegistry([PostgresAdapter()]), connections, profiles)
    yield service, connections, entry.profile.id
    service.close_all()


def _noop(connection):
    return None


def test_the_agents_hold_at_most_the_cap_closing_the_least_recently_used(monkeypatch, agents_env):
    service, connections, pid = agents_env
    monkeypatch.setattr(metadata_service, "MAX_AGENT_CONNECTIONS", 3)
    for database in ("a", "b", "c"):
        service.with_connection(pid, database, _noop, agent=True)
    service.with_connection(pid, "a", _noop, agent=True)  # used again: b is now the oldest
    service.with_connection(pid, "x", _noop)  # the app's own: never counted, never closed
    service.with_connection(pid, "d", _noop, agent=True)
    assert set(service._workers) == {(pid, "a", True), (pid, "c", True), (pid, "d", True), (pid, "x", False)}
    assert connections.leases[("b", True)].closed.is_set()
    assert not connections.leases[("x", False)].closed.is_set()


def test_a_call_that_ends_after_the_server_stopped_closes_its_connection(agents_env):
    """Minor 3: the MCP server stopped (close_all(agents_only=True)) while a call was opening its connection."""
    service, connections, pid = agents_env
    connections.gate.clear()
    done: list = []
    caller = threading.Thread(target=lambda: done.append(service.with_connection(pid, "a", _noop, agent=True)))
    caller.start()
    assert connections.opening.wait(5)
    service.close_all(agents_only=True)  # nothing registered yet: the call is still opening
    connections.gate.set()
    caller.join(10)
    assert done == [None]  # it finished
    assert connections.leases[("a", True)].closed.wait(5)
    assert not any(key[2] for key in service._workers)


def test_a_call_queued_when_the_server_stopped_does_not_reopen(agents_env):
    """Minor 3: its connection closed under it, the call does not open a new one after the stop."""
    service, connections, pid = agents_env
    service.with_connection(pid, "a", _noop, agent=True)
    holding, release = threading.Event(), threading.Event()

    def hold(connection):
        holding.set()
        assert release.wait(10)

    outcomes: list = []

    def queued():
        try:
            outcomes.append(service.with_connection(pid, "a", _noop, agent=True))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(exc)

    first = threading.Thread(target=lambda: service.with_connection(pid, "a", hold, agent=True))
    first.start()
    assert holding.wait(5)
    second = threading.Thread(target=queued)
    second.start()
    stopper = threading.Thread(target=lambda: service.close_all(agents_only=True))
    time.sleep(0.2)  # the second call is queued behind the first
    stopper.start()
    time.sleep(0.2)
    release.set()
    for thread in (first, second, stopper):
        thread.join(10)
    assert len(outcomes) == 1 and isinstance(outcomes[0], SessionBusy)
    assert not any(key[2] for key in service._workers)
