import os
import threading

import pytest

from dbaguy_backend.adapters.mssql import freetds
from dbaguy_backend.config import Settings, default_data_dir
from dbaguy_backend.container import AppContainer, keychain_service
from dbaguy_backend.domain.settings import SafetySettings
from dbaguy_backend.storage.secret_store import InMemorySecretStore


@pytest.fixture
def container(tmp_path):
    settings = Settings(token="t", data_dir=tmp_path / "data", log_to_file=False)
    return AppContainer(settings, secret_store=InMemorySecretStore())


def test_only_the_app_data_dir_uses_the_app_keychain_service(tmp_path):
    assert keychain_service(default_data_dir()) == "app.dbaguy"
    assert keychain_service(default_data_dir() / "scripts" / "..") == "app.dbaguy"  # same directory, other spelling
    # scripts/dev.sh's dev/data, tests and ad-hoc sidecars (DBAGUY_DATA_DIR elsewhere) never share the app's entries
    assert keychain_service(tmp_path / "data") == "app.dbaguy.dev"
    assert keychain_service(default_data_dir() / "nested") == "app.dbaguy.dev"
    assert keychain_service(default_data_dir().with_name("DbaGuy-dev")) == "app.dbaguy.dev"


def test_a_symlink_to_the_app_data_dir_is_the_app_data_dir(tmp_path, monkeypatch):  # R-03a-K1 edge
    # HOME points at a temp dir: the default data dir is created there, never under the real ~/Library.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    app_dir = default_data_dir()
    assert app_dir.is_relative_to(tmp_path)
    app_dir.mkdir(parents=True)
    link = tmp_path / "link-to-app-data"
    link.symlink_to(app_dir, target_is_directory=True)
    # The same directory, so the same saved passwords: the app's service, whichever spelling reached it.
    assert keychain_service(link) == "app.dbaguy"
    assert keychain_service(app_dir) == "app.dbaguy"
    other = tmp_path / "dev-data"
    other.mkdir()
    (tmp_path / "link-to-dev-data").symlink_to(other, target_is_directory=True)
    assert keychain_service(tmp_path / "link-to-dev-data") == "app.dbaguy.dev"


def test_the_container_opens_the_keychain_service_of_its_data_dir(tmp_path, monkeypatch):
    services: list[str] = []

    def keyring_store(service: str) -> InMemorySecretStore:  # never touches the real Keychain
        services.append(service)
        return InMemorySecretStore()

    monkeypatch.setattr("dbaguy_backend.container.KeyringSecretStore", keyring_store)
    AppContainer(Settings(token="t", data_dir=tmp_path / "data", log_to_file=False))
    assert services == ["app.dbaguy.dev"]


def test_building_the_app_creates_no_history_file(container, tmp_path):
    # Building the app, e.g. only to dump its OpenAPI schema, must not create the history database
    assert not (tmp_path / "data" / "history.db").exists()


def test_building_the_app_writes_no_freetds_conf(container, tmp_path):  # R-03b-P9
    assert not (tmp_path / "data" / "freetds").exists()


def test_the_app_only_records_where_freetds_conf_goes(tmp_path, monkeypatch):  # R-03b-P9
    monkeypatch.setenv("FREETDSCONF", "before")
    monkeypatch.setattr(freetds, "_active", None)
    monkeypatch.setattr(freetds, "_location", None)
    AppContainer(Settings(token="t", data_dir=tmp_path / "data", log_to_file=False), secret_store=InMemorySecretStore())
    assert freetds._location == tmp_path / "data" / "freetds" / "freetds.conf"
    assert not (tmp_path / "data" / "freetds").exists() and os.environ["FREETDSCONF"] == "before"


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
    container.sessions.check_paused_results = lambda: calls.append("paused results")
    container._housekeep_once()
    assert calls == ["sessions", "metadata", "idle transactions", "paused results"]


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
