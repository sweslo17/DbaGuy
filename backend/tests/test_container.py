import pytest

from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.storage.secret_store import InMemorySecretStore


@pytest.fixture
def container(tmp_path):
    settings = Settings(token="t", data_dir=tmp_path / "data", log_to_file=False)
    return AppContainer(settings, secret_store=InMemorySecretStore())


def test_close_stops_every_service_in_order_even_when_one_fails(container):
    calls: list[str] = []

    def failing_sessions() -> None:
        calls.append("sessions")
        raise RuntimeError("boom")

    container.sessions.close_all = failing_sessions
    container.metadata.close_all = lambda: calls.append("metadata")
    container.tunnels.close_all = lambda: calls.append("tunnels")
    container.close()
    assert calls == ["sessions", "metadata", "tunnels"]


def test_start_and_close_housekeeping_thread(container):
    container.start()
    assert container._housekeeper is not None and container._housekeeper.is_alive()
    container.close()
    container._housekeeper.join(timeout=2)
    assert not container._housekeeper.is_alive()
