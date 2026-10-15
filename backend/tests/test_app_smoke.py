import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from dbaguy_backend.app import create_app
from dbaguy_backend.config import Settings

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(token=TOKEN, data_dir=tmp_path / "data", log_to_file=False)
    return TestClient(create_app(settings))


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
