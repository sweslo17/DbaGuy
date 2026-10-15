import sqlite3

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dbaguy_backend.app import create_app
from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.storage.secret_store import InMemorySecretStore

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    settings = Settings(token=TOKEN, data_dir=tmp_path / "data", log_to_file=False)
    container = AppContainer(settings, secret_store=InMemorySecretStore())
    with TestClient(create_app(settings, container)) as test_client:
        yield test_client


@pytest.fixture
def sqlite_profile_id(client, tmp_path):
    db = tmp_path / "api.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [("a",), ("b",), ("c",)])
    conn.commit()
    conn.close()
    body = {"profile": {"engine": "sqlite", "name": "local", "path": str(db)}}
    return client.post("/profiles", json=body, headers=AUTH).json()["profile"]["id"]


def test_profile_crud_and_secret_not_on_disk(client, tmp_path):
    body = {"profile": {"engine": "sqlite", "name": "local", "path": "/tmp/x.db"}, "secrets": {"password": "s3cret-xyz"}}
    created = client.post("/profiles", json=body, headers=AUTH)
    assert created.status_code == 201 and created.json()["has_password"] is True
    pid = created.json()["profile"]["id"]
    assert "s3cret-xyz" not in (tmp_path / "data" / "profiles.json").read_text()
    assert [e["profile"]["name"] for e in client.get("/profiles", headers=AUTH).json()] == ["local"]
    body["profile"]["name"] = "renamed"
    assert client.put(f"/profiles/{pid}", json=body, headers=AUTH).json()["profile"]["name"] == "renamed"
    assert client.delete(f"/profiles/{pid}", headers=AUTH).status_code == 204
    missing = client.delete(f"/profiles/{pid}", headers=AUTH)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "profile_not_found"


def test_import_url_and_validation(client):
    response = client.post("/profiles/import-url", json={"url": "postgres://me:p%40ss@db:5433/app?sslmode=require"}, headers=AUTH)
    assert response.json()["profile"]["port"] == 5433 and response.json()["password"] == "p@ss"
    assert client.post("/profiles/import-url", json={"url": "redis://x"}, headers=AUTH).status_code == 422
    invalid = client.post("/profiles", json={"profile": {"engine": "sqlite"}}, headers=AUTH)
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "validation_failed"


def test_engines(client):
    assert {e["engine"] for e in client.get("/system/engines", headers=AUTH).json()} == {"postgres", "sqlite"}


def test_metadata_and_session_over_websocket(client, sqlite_profile_id):
    pid = sqlite_profile_id
    nodes = client.post("/metadata/children", json={"profile_id": pid, "path": []}, headers=AUTH).json()
    assert nodes[0]["name"] == "main"
    tables_path = [{"kind": "schema", "name": "main"}, {"kind": "folder", "name": "tables"}]
    tables = client.post("/metadata/children", json={"profile_id": pid, "path": tables_path}, headers=AUTH).json()
    detail = client.post("/metadata/detail", json={"profile_id": pid, "path": tables[0]["path"]}, headers=AUTH).json()
    assert detail["ddl"].startswith("CREATE TABLE t")

    session = client.post("/sessions", json={"profile_id": pid}, headers=AUTH).json()
    assert session["server"]["engine"] == "sqlite"
    sid = session["session_id"]
    with client.websocket_connect(f"/sessions/{sid}/ws?token={TOKEN}") as ws:
        ws.send_json({"type": "execute", "request_id": "q1", "sql": "SELECT id, v FROM t ORDER BY id"})
        events = []
        while True:
            events.append(ws.receive_json())
            if events[-1]["type"] == "finished":
                break
        assert [e["type"] for e in events] == ["started", "result_set", "rows", "result_set_end", "finished"]
        assert events[2]["rows"] == [[1, "a"], [2, "b"], [3, "c"]]
        ws.send_json({"type": "bogus", "request_id": "x"})
        assert ws.receive_json()["code"] == "invalid_command"
    assert client.delete(f"/sessions/{sid}", headers=AUTH).status_code == 204


def test_websocket_rejects_bad_token(client, sqlite_profile_id):
    sid = client.post("/sessions", json={"profile_id": sqlite_profile_id}, headers=AUTH).json()["session_id"]
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/sessions/{sid}/ws?token=bad") as ws:
            ws.receive_json()


def test_metadata_unknown_profile(client):
    response = client.post("/metadata/children", json={"profile_id": "00000000-0000-0000-0000-000000000000", "path": []}, headers=AUTH)
    assert response.status_code == 404


def test_websocket_close_codes(client, sqlite_profile_id):
    with pytest.raises(WebSocketDisconnect) as unknown:
        with client.websocket_connect(f"/sessions/nope/ws?token={TOKEN}") as ws:
            ws.receive_json()
    assert unknown.value.code == 4404
    sid = client.post("/sessions", json={"profile_id": sqlite_profile_id}, headers=AUTH).json()["session_id"]
    with pytest.raises(WebSocketDisconnect) as bad_token:
        with client.websocket_connect(f"/sessions/{sid}/ws?token=bad") as ws:
            ws.receive_json()
    assert bad_token.value.code == 4401


def test_websocket_cancel_stops_a_running_statement(client, sqlite_profile_id):
    sid = client.post("/sessions", json={"profile_id": sqlite_profile_id}, headers=AUTH).json()["session_id"]
    endless = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
    with client.websocket_connect(f"/sessions/{sid}/ws?token={TOKEN}") as ws:
        ws.send_json({"type": "cancel", "request_id": "nothing-running"})  # unknown request: ignored, not fatal
        ws.send_json({"type": "execute", "request_id": "slow", "sql": endless})
        assert ws.receive_json()["type"] == "started"
        ws.send_json({"type": "cancel", "request_id": "slow"})
        finished = None
        while finished is None:
            event = ws.receive_json()
            finished = event if event["type"] == "finished" else None
        assert finished["cancelled"] is True


def test_session_for_unknown_profile_and_close_unknown_session(client):
    missing = client.post("/sessions", json={"profile_id": "00000000-0000-0000-0000-000000000000"}, headers=AUTH)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "profile_not_found"
    gone = client.delete("/sessions/nope", headers=AUTH)
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "session_not_found"


def test_endpoints_require_token(client):
    for method, path in [("GET", "/profiles"), ("POST", "/metadata/children"), ("POST", "/sessions"), ("GET", "/system/engines")]:
        assert client.request(method, path).status_code == 401


def test_unexpected_errors_do_not_leak_details(tmp_path):
    settings = Settings(token=TOKEN, data_dir=tmp_path / "data", log_to_file=False)
    container = AppContainer(settings, secret_store=InMemorySecretStore())

    def boom():
        raise RuntimeError("could not connect to host=10.0.0.5 user=admin password=hunter2")

    container.profiles.list_entries = boom
    with TestClient(create_app(settings, container), raise_server_exceptions=False) as test_client:
        response = test_client.get("/profiles", headers=AUTH)
    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal_error", "message": "Internal error"}}
