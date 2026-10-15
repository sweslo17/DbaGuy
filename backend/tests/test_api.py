import socket
import sqlite3
import threading
import time
from uuid import UUID

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from dbaguy_backend.app import create_app
from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.storage.secret_store import InMemorySecretStore

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE_URL = "http://127.0.0.1"  # the backend only answers requests addressed to 127.0.0.1 / localhost
WS_BASE = "ws://127.0.0.1"  # TestClient.websocket_connect ignores base_url


@pytest.fixture
def client(tmp_path):
    settings = Settings(token=TOKEN, data_dir=tmp_path / "data", log_to_file=False)
    container = AppContainer(settings, secret_store=InMemorySecretStore())
    with TestClient(create_app(settings, container), base_url=BASE_URL) as test_client:
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


def test_session_for_a_locked_profile_says_which_passwords_are_needed(client):
    body = {"profile": {"engine": "postgres", "name": "locked", "host": "127.0.0.1", "save_password": False,
                        "ssh": {"enabled": True, "host": "bastion", "auth": "password"}}}
    pid = client.post("/profiles", json=body, headers=AUTH).json()["profile"]["id"]
    response = client.post("/sessions", json={"profile_id": pid}, headers=AUTH)
    assert response.status_code == 428
    error = response.json()["error"]
    assert error["code"] == "password_required" and error["detail"] == {"password": True, "ssh_password": True}


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
    with client.websocket_connect(f"{WS_BASE}/sessions/{sid}/ws?token={TOKEN}") as ws:
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
        assert ws.receive_json() == {
            "type": "finished", "request_id": "x", "elapsed_ms": 0, "in_transaction": False,
            "transaction_failed": False, "cancelled": False,
        }
    assert client.delete(f"/sessions/{sid}", headers=AUTH).status_code == 204


def test_metadata_search_and_refresh(client, sqlite_profile_id):
    hits = client.get("/metadata/search", params={"profile_id": sqlite_profile_id, "q": "t"}, headers=AUTH).json()
    assert hits[0]["name"] == "t" and hits[0]["path"][-1] == {"kind": "table", "name": "t"}
    response = client.post("/metadata/schema/refresh", json={"profile_id": sqlite_profile_id}, headers=AUTH)
    assert response.status_code == 204


def test_profile_changes_drop_the_cached_schema(client, sqlite_profile_id, tmp_path):
    cache = client.app.state.container.schema_cache
    pid = UUID(sqlite_profile_id)

    def warm_up():
        client.get("/metadata/search", params={"profile_id": sqlite_profile_id, "q": "t"}, headers=AUTH)
        assert cache.peek(pid) is not None

    warm_up()
    body = {"profile": {"engine": "sqlite", "name": "renamed", "path": str(tmp_path / "api.db")}}
    assert client.put(f"/profiles/{sqlite_profile_id}", json=body, headers=AUTH).status_code == 200
    assert cache.peek(pid) is None
    warm_up()
    assert client.post(f"/profiles/{sqlite_profile_id}/unlock", json={"password": "x"}, headers=AUTH).status_code == 204
    assert cache.peek(pid) is None
    warm_up()
    assert client.delete(f"/profiles/{sqlite_profile_id}", headers=AUTH).status_code == 204
    assert cache.peek(pid) is None


def test_encoded_cells_reach_the_websocket_client_unchanged(client, sqlite_profile_id):
    sid = client.post("/sessions", json={"profile_id": sqlite_profile_id}, headers=AUTH).json()["session_id"]
    with client.websocket_connect(f"{WS_BASE}/sessions/{sid}/ws?token={TOKEN}") as ws:
        ws.send_json({"type": "execute", "request_id": "enc", "sql": "SELECT x'deadbeef', 9007199254740993"})
        rows = None
        while (event := ws.receive_json())["type"] != "finished":
            rows = event["rows"] if event["type"] == "rows" else rows
    assert rows == [[{"$type": "bytes", "size": 4, "preview": "deadbeef"}, "9007199254740993"]]


def test_invalid_command_without_a_request_id_gets_an_error_but_no_finished(client, sqlite_profile_id):
    sid = client.post("/sessions", json={"profile_id": sqlite_profile_id}, headers=AUTH).json()["session_id"]
    with client.websocket_connect(f"{WS_BASE}/sessions/{sid}/ws?token={TOKEN}") as ws:
        ws.send_json({"type": "bogus"})
        assert ws.receive_json()["code"] == "invalid_command"
        ws.send_json({"type": "execute", "request_id": "q", "sql": "SELECT 1"})
        assert ws.receive_json()["type"] == "started"  # nothing was queued between the error and this request


def test_command_rejected_by_a_closed_session_gets_an_error_and_finished(client, sqlite_profile_id):
    sid = client.post("/sessions", json={"profile_id": sqlite_profile_id}, headers=AUTH).json()["session_id"]
    with client.websocket_connect(f"{WS_BASE}/sessions/{sid}/ws?token={TOKEN}") as ws:
        client.app.state.container.sessions.worker(sid).close()  # the socket stays attached to a dead worker
        ws.send_json({"type": "execute", "request_id": "late", "sql": "SELECT 1"})
        error = ws.receive_json()
        finished = ws.receive_json()
    assert error["type"] == "error" and error["request_id"] == "late" and error["code"] == "session_busy"
    assert finished["type"] == "finished" and finished["request_id"] == "late" and finished["cancelled"] is False


def test_websocket_rejects_bad_token(client, sqlite_profile_id):
    sid = client.post("/sessions", json={"profile_id": sqlite_profile_id}, headers=AUTH).json()["session_id"]
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"{WS_BASE}/sessions/{sid}/ws?token=bad") as ws:
            ws.receive_json()


def test_metadata_unknown_profile(client):
    response = client.post("/metadata/children", json={"profile_id": "00000000-0000-0000-0000-000000000000", "path": []}, headers=AUTH)
    assert response.status_code == 404


def test_websocket_close_codes(client, sqlite_profile_id):
    with pytest.raises(WebSocketDisconnect) as unknown:
        with client.websocket_connect(f"{WS_BASE}/sessions/nope/ws?token={TOKEN}") as ws:
            ws.receive_json()
    assert unknown.value.code == 4404
    sid = client.post("/sessions", json={"profile_id": sqlite_profile_id}, headers=AUTH).json()["session_id"]
    with pytest.raises(WebSocketDisconnect) as bad_token:
        with client.websocket_connect(f"{WS_BASE}/sessions/{sid}/ws?token=bad") as ws:
            ws.receive_json()
    assert bad_token.value.code == 4401


def test_websocket_cancel_stops_a_running_statement(client, sqlite_profile_id):
    sid = client.post("/sessions", json={"profile_id": sqlite_profile_id}, headers=AUTH).json()["session_id"]
    endless = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
    with client.websocket_connect(f"{WS_BASE}/sessions/{sid}/ws?token={TOKEN}") as ws:
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
    with TestClient(create_app(settings, container), base_url=BASE_URL, raise_server_exceptions=False) as test_client:
        response = test_client.get("/profiles", headers=AUTH)
    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal_error", "message": "Internal error"}}


@pytest.fixture
def live_port(tmp_path):
    """The app under a real uvicorn server: TestClient hides how a close-before-accept looks on the wire."""
    settings = Settings(token=TOKEN, data_dir=tmp_path / "data", log_to_file=False)
    app = create_app(settings, AppContainer(settings, secret_store=InMemorySecretStore()))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="test-uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "uvicorn did not start"
    try:
        yield sock.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive(), "uvicorn did not shut down"


@pytest.mark.parametrize(
    ("session_id", "token", "expected_code"),
    [("anything", "bad", 4401), ("no-such-session", TOKEN, 4404)],
    ids=["bad_token", "unknown_session"],
)
def test_websocket_close_codes_are_visible_to_a_real_client(live_port, session_id, token, expected_code):
    # Closing before accept() would be an HTTP 403 handshake rejection: a browser then only sees 1006.
    with connect(f"ws://127.0.0.1:{live_port}/sessions/{session_id}/ws?token={token}", open_timeout=2) as ws:
        with pytest.raises(ConnectionClosed) as closed:
            ws.recv(timeout=2)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == expected_code
