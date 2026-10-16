import threading

import pytest

from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.settings import SafetySettings
from dbaguy_backend.storage.secret_store import InMemorySecretStore


@pytest.fixture
def container(tmp_path):
    settings = Settings(token="t", data_dir=tmp_path / "data", log_to_file=False)
    return AppContainer(settings, secret_store=InMemorySecretStore())


def test_building_the_app_creates_no_history_file(container, tmp_path):
    # `npm run gen:api` builds the app with the real data dir just to dump its OpenAPI schema
    assert not (tmp_path / "data" / "history.db").exists()


def test_close_stops_every_service_in_order_even_when_one_fails(container):
    calls: list[str] = []

    def failing_sessions() -> None:
        calls.append("sessions")
        raise RuntimeError("boom")

    container.sessions.close_all = failing_sessions
    container.metadata.close_all = lambda: calls.append("metadata")
    container.tunnels.close_all = lambda: calls.append("tunnels")
    container.history.close = lambda: calls.append("history")
    container.close()
    assert calls == ["sessions", "metadata", "tunnels", "history"]


def test_housekeeping_checks_idle_transactions_with_the_current_settings(container):
    checks: list[tuple[float, float]] = []
    container.sessions.check_idle_transactions = lambda warn, rollback: checks.append((warn, rollback))
    container._housekeep_once()
    settings = container.app_settings.get()
    container.app_settings.update(settings.model_copy(update={
        "safety": SafetySettings(idle_transaction_warn_minutes=2, idle_transaction_rollback_minutes=10),
    }))
    container._housekeep_once()
    assert checks == [(3 * 60, 0), (2 * 60, 10 * 60)]  # the defaults, then the changed setting, without a restart


def test_one_failing_housekeeping_task_does_not_stop_the_others(container):
    calls: list[str] = []

    def failing_reaper() -> int:
        calls.append("sessions")
        raise RuntimeError("boom")

    container.sessions.reap_idle = failing_reaper
    container.metadata.reap_idle = lambda: calls.append("metadata")
    container.sessions.check_idle_transactions = lambda warn, rollback: calls.append("idle transactions")
    container._housekeep_once()
    assert calls == ["sessions", "metadata", "idle transactions"]


def test_start_and_close_housekeeping_thread(container):
    container.start()
    assert container._housekeeper is not None and container._housekeeper.is_alive()
    container.close()
    container._housekeeper.join(timeout=2)
    assert not container._housekeeper.is_alive()


def test_start_warms_the_sql_parser_up_in_a_background_thread(container, monkeypatch):
    ran, seen = threading.Event(), []

    def warm_up() -> None:
        seen.append(threading.current_thread())
        ran.set()

    monkeypatch.setattr("dbaguy_backend.container.warm_up", warm_up, raising=False)
    container.start()
    try:
        assert ran.wait(5)
        assert seen[0] is not threading.main_thread() and seen[0].daemon
    finally:
        container.close()
