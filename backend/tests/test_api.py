import socket
import sqlite3
import threading
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from dbaguy_backend.api.errors import register_error_handlers
from dbaguy_backend.app import create_app
from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.errors import QueryFailed, ValidationFailed
from dbaguy_backend.domain.history import HistoryRecord
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


def receive(ws):
    """The next event that is not a `session_state` (sent on attach and right before every `finished`)."""
    while (event := ws.receive_json())["type"] == "session_state":
        pass
    return event


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


def test_a_validation_error_never_echoes_the_input(client):
    secret = "hunter2\ud800"  # a lone surrogate could not be encoded in the answer either
    body = '{"profile": {"engine": "postgres", "port": "' + secret.encode("unicode_escape").decode() + '"}}'
    response = client.post("/profiles", content=body, headers={**AUTH, "Content-Type": "application/json"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_failed"
    assert "hunter2" not in response.text and all("input" not in e for e in response.json()["error"]["detail"])


@pytest.mark.parametrize("url", [
    "postgres://h:abc/db", "postgres://h:99999999/db", "postgres://[bad/db", "sqlite://[bad/x.db",
    "mysql://h:abc/db", "jdbc:mysql://h:99999999/db", "mysql://[bad/db",
])
def test_import_url_with_a_malformed_authority_is_a_validation_error_not_a_500(client, url):
    response = client.post("/profiles/import-url", json={"url": url}, headers=AUTH)
    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_failed"
    assert "Unsupported URL scheme" not in response.json()["error"]["message"]  # the engine's own parser said no


def test_import_url_never_echoes_the_password_of_a_malformed_url(client):
    logged: list[str] = []
    sink = logger.add(lambda message: logged.append(str(message)), level="DEBUG")
    try:
        response = client.post("/profiles/import-url", json={"url": "postgres://root:hunter2\uff20db:5432/app"}, headers=AUTH)
    finally:
        logger.remove(sink)
    assert response.status_code == 422 and "Percent-encode" in response.json()["error"]["message"]
    assert "hunter2" not in response.text
    assert any("rejected" in line for line in logged) and "hunter2" not in "".join(logged)


def test_a_query_failed_keeps_the_servers_text_out_of_the_info_log():
    """The server's text quotes the user's statement (a syntax error shows the token, a string literal included): the
    answer carries it, INFO does not, DEBUG does. Our own errors keep their text at INFO."""
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/server")
    def server_error():
        raise QueryFailed('syntax error - at "SECRET_LIT_1"')

    @app.get("/ours")
    def own_error():
        raise ValidationFailed("This is our own text")

    levels: dict[str, list[str]] = {"INFO": [], "DEBUG": []}
    sinks = [logger.add(lambda m, name=name: levels[name].append(m.record["message"]), level=name,
                        filter=lambda r, name=name: r["level"].name == name) for name in levels]
    try:
        with TestClient(app, base_url=BASE_URL) as client:
            failed, ours = client.get("/server"), client.get("/ours")
    finally:
        for sink in sinks:
            logger.remove(sink)
    assert failed.status_code == 400 and "SECRET_LIT_1" in failed.json()["error"]["message"]
    assert any("/server" in line and "query_failed" in line for line in levels["INFO"])
    assert "SECRET_LIT_1" not in "".join(levels["INFO"])
    assert any("SECRET_LIT_1" in line for line in levels["DEBUG"])
    assert ours.status_code == 422 and any("This is our own text" in line for line in levels["INFO"])


def test_engines(client):
    engines = {e["engine"]: e for e in client.get("/system/engines", headers=AUTH).json()}
    assert set(engines) == {"postgres", "mysql", "mssql", "couchbase", "sqlite"}
    assert engines["postgres"]["per_database_connection"] and engines["postgres"]["cancel_aborts_transaction"]
    assert not engines["sqlite"]["per_database_connection"] and not engines["sqlite"]["cancel_aborts_transaction"]
    assert engines["postgres"]["server_read_only"] and engines["sqlite"]["server_read_only"]
    mysql = engines["mysql"]
    assert (mysql["per_database_connection"], mysql["cancel_aborts_transaction"], mysql["server_read_only"]) == (
        False, False, True,
    )
    mssql = engines["mssql"]
    assert (mssql["per_database_connection"], mssql["cancel_aborts_transaction"], mssql["server_read_only"]) == (
        True, False, False,
    )
    couchbase = engines["couchbase"]
    assert couchbase["dialect"] == "sqlpp" and couchbase["default_port"] == 8093 and couchbase["server_read_only"]
    assert not couchbase["transactions"] and couchbase["query_context"] and couchbase["parameters"]
    assert couchbase["documents"] and couchbase["executed_plan"] and couchbase["plan_format"] == "couchbase"
    assert couchbase["url_schemes"] == ["couchbase", "couchbases", "http", "https"]
    for name, info in engines.items():
        if name == "couchbase":
            continue
        assert info["transactions"] and info["plan_format"] is None
        assert not (info["query_context"] or info["parameters"] or info["documents"] or info["executed_plan"])
    assert {name: info["row_editing"] for name, info in engines.items()} == {
        "postgres": True, "mysql": True, "mssql": True, "sqlite": True, "couchbase": False}


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
        assert [e["type"] for e in events] == [
            "session_state", "started", "result_set", "rows", "result_set_end", "session_state", "finished",
        ]
        assert events[0] == events[5] == {
            "type": "session_state", "autocommit": True, "in_transaction": False, "transaction_failed": False,
            "transaction_started_at": None, "idle_seconds": None,
        }
        assert events[3]["rows"] == [[1, "a"], [2, "b"], [3, "c"]]
        ws.send_json({"type": "bogus", "request_id": "x"})
        assert ws.receive_json()["code"] == "invalid_command"
        assert ws.receive_json() == {
            "type": "finished", "request_id": "x", "elapsed_ms": 0, "in_transaction": False,
            "transaction_failed": False, "cancelled": False, "timed_out": False, "skipped": False,
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
        assert receive(ws)["code"] == "invalid_command"
        ws.send_json({"type": "execute", "request_id": "q", "sql": "SELECT 1"})
        assert receive(ws)["type"] == "started"  # nothing was queued between the error and this request


def test_command_rejected_by_a_closed_session_gets_an_error_and_finished(client, sqlite_profile_id):
    sid = client.post("/sessions", json={"profile_id": sqlite_profile_id}, headers=AUTH).json()["session_id"]
    with client.websocket_connect(f"{WS_BASE}/sessions/{sid}/ws?token={TOKEN}") as ws:
        client.app.state.container.sessions.worker(sid).close()  # the socket stays attached to a dead worker
        ws.send_json({"type": "execute", "request_id": "late", "sql": "SELECT 1"})
        error = receive(ws)
        finished = receive(ws)
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
        assert receive(ws)["type"] == "started"
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


def test_settings_and_workspace_api(client):
    settings = client.get("/settings", headers=AUTH).json()
    assert settings["run"]["page_size"] == 500
    settings["editor"]["vim"] = True
    assert client.put("/settings", json=settings, headers=AUTH).json()["editor"]["vim"] is True
    workspace = {"version": 1, "tabs": [{"id": "t1", "kind": "query", "buffer": "SELECT '😀'"}], "active_tab_id": "t1"}
    assert client.put("/workspace", json=workspace, headers=AUTH).status_code == 204
    assert client.get("/workspace", headers=AUTH).json()["tabs"][0]["buffer"] == "SELECT '😀'"
    object_tab = {"id": "o1", "kind": "object", "title": "orders",
                  "object_path": [{"kind": "schema", "name": "public"}, {"kind": "table", "name": "orders"}]}
    workspace["tabs"].append(object_tab)
    assert client.put("/workspace", json=workspace, headers=AUTH).status_code == 204
    assert client.get("/workspace", headers=AUTH).json()["tabs"][1]["kind"] == "object"


def test_a_save_with_a_bad_draft_name_or_context_keeps_the_rest(client):  # B40 backstop: prune, then save
    logged: list[str] = []
    sink = logger.add(lambda m: logged.append(m.record["message"]), level="DEBUG")
    tab = {"id": "t1", "kind": "query", "buffer": "SELECT $a", "query_context": "all",
           "params": {"$a": "1", "bad name": "2"}}
    try:
        saved = client.put("/workspace", json={"tabs": [tab, {"id": "t2", "kind": "query"}], "active_tab_id": "t2"},
                           headers=AUTH)
    finally:
        logger.remove(sink)
    assert saved.status_code == 204
    state = client.get("/workspace", headers=AUTH).json()
    assert [t["id"] for t in state["tabs"]] == ["t1", "t2"] and state["active_tab_id"] == "t2"
    assert state["tabs"][0]["buffer"] == "SELECT $a" and state["tabs"][0]["params"] == {"$a": "1"}
    assert state["tabs"][0]["query_context"] is None
    assert any("workspace being saved" in line for line in logged) and "bad name" not in "".join(logged)
    # a tab that is wrong in any other way still refuses the whole save
    assert client.put("/workspace", json={"tabs": [{"id": "t3", "kind": "chart"}]}, headers=AUTH).status_code == 422


def test_settings_and_workspace_reject_bad_input(client):
    settings = client.get("/settings", headers=AUTH).json()
    assert client.put("/settings", json={**settings, "editor": {"font_size": 999}}, headers=AUTH).status_code == 422
    assert client.put("/settings", json={**settings, "future": 1}, headers=AUTH).status_code == 422
    relative = client.put("/settings", json={**settings, "scripts_dir": "relative"}, headers=AUTH)
    assert relative.status_code == 422 and relative.json()["error"]["code"] == "validation_failed"
    assert client.put("/workspace", json={"tabs": [{"id": "", "kind": "query"}]}, headers=AUTH).status_code == 422
    assert client.get("/settings").status_code == 401 and client.put("/workspace", json={}).status_code == 401


def test_scripts_api(client):
    assert client.put("/scripts/file", json={"path": "a.sql", "content": "SELECT 1"}, headers=AUTH).status_code == 200
    assert client.get("/scripts", headers=AUTH).json()["items"][0]["path"] == "a.sql"
    assert client.get("/scripts/file", params={"path": "a.sql"}, headers=AUTH).json()["content"] == "SELECT 1"
    assert client.post("/scripts/rename", json={"from": "a.sql", "to": "b.sql"}, headers=AUTH).status_code == 204
    assert client.get("/scripts/file", params={"path": "../x.sql"}, headers=AUTH).status_code == 422
    assert client.delete("/scripts/file", params={"path": "b.sql"}, headers=AUTH).status_code == 204


def test_scripts_api_folders_and_errors(client, tmp_path):
    assert client.post("/scripts/folder", json={"path": "reports"}, headers=AUTH).status_code == 204
    again = client.post("/scripts/folder", json={"path": "reports"}, headers=AUTH)
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_exists"
    missing = client.get("/scripts/file", params={"path": "gone.sql"}, headers=AUTH)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    tree = client.get("/scripts", headers=AUTH).json()
    assert tree["root"] == str(tmp_path / "data" / "scripts") and tree["items"][0]["kind"] == "folder"
    assert client.post("/scripts/rename", json={"from": "a.sql"}, headers=AUTH).status_code == 422
    assert client.post("/scripts/rename", json={"from": "a.sql", "to": "b.sql", "x": 1}, headers=AUTH).status_code == 422
    assert client.get("/scripts").status_code == 401


def test_scripts_follow_the_configured_folder(client, tmp_path):
    settings = client.get("/settings", headers=AUTH).json()
    settings["scripts_dir"] = str(tmp_path / "repo")
    assert client.put("/settings", json=settings, headers=AUTH).status_code == 200
    client.put("/scripts/file", json={"path": "q.sql", "content": "SELECT 2"}, headers=AUTH)
    assert (tmp_path / "repo" / "q.sql").read_text() == "SELECT 2"


def test_scripts_api_path_problems_are_client_errors(client, tmp_path):
    (tmp_path / "data" / "scripts" / ".git").mkdir(parents=True)
    assert client.put("/scripts/file", json={"path": "file.sql", "content": "x"}, headers=AUTH).status_code == 200
    assert client.get("/scripts/file", params={"path": "file.sql/x.sql"}, headers=AUTH).status_code == 404
    in_the_way = client.put("/scripts/file", json={"path": "file.sql/x.sql", "content": "x"}, headers=AUTH)
    assert in_the_way.status_code == 409 and in_the_way.json()["error"]["code"] == "already_exists"
    too_long = client.get("/scripts/file", params={"path": "x" * 252 + ".sql"}, headers=AUTH)
    assert too_long.status_code == 422 and "too long" in too_long.json()["error"]["message"]
    assert client.post("/scripts/rename", json={"from": ".git", "to": "old"}, headers=AUTH).status_code == 422
    assert client.post("/scripts/folder", json={"path": ".git/new"}, headers=AUTH).status_code == 422
    assert client.put("/scripts/file", json={"path": ".git/x.sql", "content": "x"}, headers=AUTH).status_code == 422
    assert [p.name for p in (tmp_path / "data" / "scripts" / ".git").iterdir()] == []


def test_history_api(client):
    container = client.app.state.container
    container.history.record(HistoryRecord(profile_id=uuid4(), profile_name="p", database=None, sql="SELECT 42",
                                           started_at=datetime.now(UTC), elapsed_ms=1, rows=1, status="ok"))
    page = client.get("/history", params={"q": "42"}, headers=AUTH).json()
    assert [e["sql"] for e in page["items"]] == ["SELECT 42"]
    assert client.delete("/history", headers=AUTH).status_code == 204
    assert client.get("/history", headers=AUTH).json()["items"] == []
    assert client.get("/history").status_code == 401 and client.delete("/history").status_code == 401
    assert client.get("/history", params={"limit": 0}, headers=AUTH).status_code == 422
    assert client.get("/history", params={"limit": 1001}, headers=AUTH).status_code == 422
