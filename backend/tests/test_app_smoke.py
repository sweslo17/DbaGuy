import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from dbaguy_backend import __main__ as backend_main
from dbaguy_backend.api.deps import websocket_token_ok
from dbaguy_backend.app import create_app, create_app_from_env
from dbaguy_backend.config import Settings

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE_URL = "http://127.0.0.1"  # the backend only answers requests addressed to 127.0.0.1 / localhost


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(token=TOKEN, data_dir=tmp_path / "data", log_to_file=False)
    return TestClient(create_app(settings), base_url=BASE_URL)


def test_health_requires_token(tmp_path):
    with make_client(tmp_path) as client:
        response = client.get("/system/health")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


def test_health_ok_with_token(tmp_path):
    with make_client(tmp_path) as client:
        response = client.get("/system/health", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


def test_non_ascii_token_is_rejected_as_unauthorized(tmp_path):
    with make_client(tmp_path) as client:
        response = client.get("/system/health", headers={"Authorization": "Bearer café".encode("utf-8")})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


def test_websocket_token_check_rejects_non_ascii_token():
    settings = SimpleNamespace(token=TOKEN)
    websocket = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(settings=settings))),
        query_params={"token": "café"},
    )
    assert websocket_token_ok(websocket) is False


def test_cors_preflight_allows_tauri_origin(tmp_path):
    with make_client(tmp_path) as client:
        response = client.options(
            "/system/health",
            headers={
                "Origin": "tauri://localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "tauri://localhost"


def test_main_prints_ready_line_and_exits_on_stdin_close(tmp_path):
    env = {
        **os.environ,
        "DBAGUY_TOKEN": TOKEN,
        "DBAGUY_PORT": "0",
        "DBAGUY_WATCH_STDIN": "true",
        "DBAGUY_LOG_TO_FILE": "false",
        "DBAGUY_DATA_DIR": str(tmp_path / "data"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "dbaguy_backend"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, text=True,
    )
    try:
        line = proc.stdout.readline()
        ready = json.loads(line)
        assert ready["event"] == "ready"
        port = ready["port"]
        response = httpx.get(f"http://127.0.0.1:{port}/system/health", headers=AUTH, timeout=5)
        assert response.status_code == 200
        proc.stdin.close()
        deadline = time.monotonic() + 8
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert proc.poll() is not None, "backend did not exit after stdin closed"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_forced_exit_closes_tunnels_first_even_if_that_fails(monkeypatch):
    calls: list = []
    monkeypatch.setattr(backend_main.os, "_exit", lambda code: calls.append(("exit", code)))
    backend_main._force_exit(lambda: calls.append("tunnels"))
    assert calls == ["tunnels", ("exit", 0)]

    calls.clear()

    def failing_close() -> None:
        calls.append("tunnels")
        raise OSError("boom")

    backend_main._force_exit(failing_close)
    assert calls == ["tunnels", ("exit", 0)]


def _env_without_token(tmp_path) -> dict[str, str]:
    env = {**os.environ, "DBAGUY_LOG_TO_FILE": "false", "DBAGUY_DATA_DIR": str(tmp_path / "data")}
    env.pop("DBAGUY_TOKEN", None)
    return env


def test_requests_for_a_foreign_host_are_rejected(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get("/system/health", headers={**AUTH, "host": "evil.example"}).status_code == 400
        assert client.get("/system/health", headers={**AUTH, "host": "localhost:18765"}).status_code == 200
        assert client.get("/system/health", headers={**AUTH, "host": "127.0.0.1:18765"}).status_code == 200
        with pytest.raises(WebSocketDenialResponse):
            with client.websocket_connect(f"ws://evil.example/sessions/x/ws?token={TOKEN}"):
                pass


def test_starting_without_a_token_exits_non_zero(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "dbaguy_backend"], env=_env_without_token(tmp_path),
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "DBAGUY_TOKEN" in result.stderr
    assert result.stdout == ""  # no ready line: nothing is listening


def test_app_factory_refuses_to_start_without_a_token(monkeypatch):
    monkeypatch.delenv("DBAGUY_TOKEN", raising=False)
    monkeypatch.setenv("DBAGUY_LOG_TO_FILE", "false")
    with pytest.raises(SystemExit) as info:
        create_app_from_env()
    assert info.value.code not in (0, None)


def test_openapi_dump_needs_no_token(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "dbaguy_backend.openapi_dump"], env=_env_without_token(tmp_path),
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "/system/health" in json.loads(result.stdout)["paths"]


def test_importing_the_app_leaves_sqlglot_for_later_and_the_domain_needs_no_sql_package(tmp_path):
    # sqlglot costs ~190 ms of start-up: the classifier imports it on first use (warmed in the background).
    code = textwrap.dedent("""
        import importlib, pkgutil, sys
        import dbaguy_backend.domain as domain
        for info in pkgutil.iter_modules(domain.__path__):
            importlib.import_module(f"dbaguy_backend.domain.{info.name}")
        leaks = sorted(m for m in sys.modules if m.startswith(("dbaguy_backend.sql", "sqlglot")))
        assert not leaks, f"the domain imports {leaks}"
        import dbaguy_backend.app
        assert "sqlglot" not in sys.modules, "importing the app loads sqlglot"
    """)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                            cwd=Path(__file__).parents[1])
    assert result.returncode == 0, result.stderr
