"""The MCP server's lifecycle, its HTTP gate and the Settings API (spec ⑧ D-08-8, R-08-3, R-08-4, D-08-12; A2, A3)."""

import json
import socket
import subprocess
import sys
import textwrap
import threading
import time

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from dbaguy_backend.agent.host import START_DELAY_S, claude_commands
from dbaguy_backend.agent.token_store import InMemoryTokenStore
from dbaguy_backend.app import create_app
from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.domain.settings import McpSettings
from dbaguy_backend.storage.secret_store import InMemorySecretStore

from .agent_support import (
    AUTH, MCP_TOKEN, TOKEN, call_tool, container, free_port, list_tools, make_container, serving, wait_for_state,
)

__all__ = ["container"]  # the fixture


def _add_profile(client: TestClient, name: str, access: str, path: str = "/tmp/zz_08_none.db") -> str:
    body = {"profile": {"engine": "sqlite", "name": name, "path": path, "mcp_access": access}}
    return client.post("/profiles", json=body, headers=AUTH).json()["profile"]["id"]


def test_the_sdk_is_never_imported_while_the_switch_is_off(tmp_path):
    script = textwrap.dedent(f"""
        import sys
        from fastapi.testclient import TestClient
        from dbaguy_backend.app import create_app
        from dbaguy_backend.config import Settings
        from dbaguy_backend.container import AppContainer
        from dbaguy_backend.storage.secret_store import InMemorySecretStore
        settings = Settings(token="t", data_dir={str(tmp_path / "data")!r}, log_to_file=False)
        container = AppContainer(settings, secret_store=InMemorySecretStore())
        with TestClient(create_app(settings, container), base_url="http://127.0.0.1") as client:
            status = client.get("/mcp/status", headers={{"Authorization": "Bearer t"}}).json()
        assert status["state"] == "off" and status["port"] == 17532, status
        loaded = sorted(name for name in sys.modules if name == "mcp" or name.startswith(("mcp.", "mcp_types")))
        assert not loaded, loaded
        print("ok")
    """)
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == "ok", done.stderr[-2000:]


def test_a_switch_left_on_starts_the_server_once_the_app_is_up(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    port = free_port()
    (data / "settings.json").write_text(json.dumps({"mcp": {"enabled": True, "port": port}}))
    built = make_container(data)
    with TestClient(create_app(built.settings, built), base_url="http://127.0.0.1"):
        assert built.agent_host.status().state == "off"  # not before the API answers (D-08-8)
        wait_for_state(built, "listening")
        assert call_tool(port, "list_connections")[0] is False
    assert built.agent_host.status().state == "off"  # the app's end stops it


def test_the_token_is_kept_and_regenerate_replaces_it(container):
    first = container.agent_host.token()
    assert first.token == MCP_TOKEN and first.url == "http://127.0.0.1:17532/mcp"
    assert first.command == (
        'claude mcp add --transport http --scope user dbaguy http://127.0.0.1:17532/mcp '
        f'--header "Authorization: Bearer {MCP_TOKEN}"')
    assert first.replace_command == f"claude mcp remove --scope user dbaguy && {first.command}"
    second = container.agent_host.regenerate()
    assert second.token != MCP_TOKEN and len(second.token) == 43
    assert container.agent_host.token().token == second.token


def test_claude_code_gets_the_header_after_the_url():
    add, replace = claude_commands("http://127.0.0.1:17531/mcp", "abc")
    assert add.index("http://127.0.0.1:17531/mcp") < add.index("--header")
    assert replace.startswith("claude mcp remove --scope user dbaguy && claude mcp add")


def test_starting_and_stopping_again_and_again_leaves_no_thread_and_frees_the_port(container):
    port = free_port()
    threads = threading.active_count()
    for _ in range(5):
        container.agent_host.apply(McpSettings(enabled=True, port=port))
        wait_for_state(container, "listening")
        container.agent_host.apply(McpSettings(enabled=False, port=port))
        assert container.agent_host.status().state == "off"
    time.sleep(0.2)
    assert threading.active_count() <= threads + 1  # anyio's worker threads may linger idle for a moment
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))  # nothing holds it any more


def test_a_port_another_program_holds_is_reported_and_taken_once_free(container):
    port = free_port()
    holder = socket.socket()
    holder.bind(("127.0.0.1", port))
    holder.listen(1)
    try:
        container.agent_host.apply(McpSettings(enabled=True, port=port))
        wait_for_state(container, "port_in_use")
        assert f"Port {port} is in use" in container.agent_host.status().message
    finally:
        holder.close()
    container.agent_host.apply(McpSettings(enabled=True, port=port))  # applying again retries
    wait_for_state(container, "listening")
    container.agent_host.stop()


def test_the_gate_answers_401_421_and_403_before_the_sdk(container):
    with serving(container) as port:
        url = f"http://127.0.0.1:{port}/mcp"
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        good = {"Authorization": f"Bearer {MCP_TOKEN}", "Accept": "application/json, text/event-stream"}
        no_token = httpx.post(url, json=body, headers={"Accept": good["Accept"]})
        assert no_token.status_code == 401 and no_token.headers["www-authenticate"] == "Bearer"
        assert httpx.post(url, json=body, headers={**good, "Authorization": "Bearer nope"}).status_code == 401
        assert httpx.post(url, json=body, headers={**good, "Host": f"evil.example:{port}"}).status_code == 421
        assert httpx.post(url, json=body, headers={**good, "Origin": f"http://localhost:{port}"}).status_code == 403
        oversized = httpx.post(url, content=b"x" * (1024 * 1024 + 1),
                               headers={**good, "Content-Type": "application/json"})
        assert oversized.status_code == 413


def test_both_protocol_versions_list_and_call_tools_and_see_only_shared_connections(container, tmp_path):
    with TestClient(create_app(container.settings, container), base_url="http://127.0.0.1") as client:
        shared = _add_profile(client, "zz_08 shared", "query")
        _add_profile(client, "zz_08 schema", "schema")
        _add_profile(client, "zz_08 hidden", "off")
        with serving(container) as port:
            for mode in ("legacy", "2026-07-28"):
                tools = list_tools(port, mode=mode)
                assert "list_connections" in {tool.name for tool in tools}
                assert all(tool.annotations.read_only_hint for tool in tools)
                assert all(tool.description.startswith("Read-only.") for tool in tools)
                error, data = call_tool(port, "list_connections", mode=mode)
                assert not error
                names = {c["name"]: c for c in data["connections"]}
                assert set(names) == {"zz_08 shared", "zz_08 schema"}
                assert names["zz_08 shared"]["id"] == shared and names["zz_08 shared"]["access"] == "query"
                assert names["zz_08 shared"]["read_only_by"] == "server"
                assert names["zz_08 shared"]["needs_unlock"] is False


class _CountingSecrets(InMemorySecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def get(self, profile_id, kind):
        self.reads += 1
        return super().get(profile_id, kind)


def test_an_agents_calls_never_read_a_saved_secret(tmp_path):
    secrets = _CountingSecrets()
    built = AppContainer(Settings(token=TOKEN, data_dir=tmp_path / "data", log_to_file=False), secret_store=secrets,
                         token_store=InMemoryTokenStore(MCP_TOKEN))
    try:
        for name in ("zz_08 a", "zz_08 b"):
            built.profiles.create(ProfileUpsertRequest.model_validate({
                "profile": {"engine": "postgres", "name": name, "host": "127.0.0.1", "mcp_access": "query"},
                "secrets": {"password": "x"}}))
        secrets.reads = 0
        listed = built.agent_tools.list_connections()["connections"]
        assert [c["needs_unlock"] for c in listed] == [False, False]
        assert built.agent_tools.shared("zz_08 a", "list_objects").profile.name == "zz_08 a"
        assert secrets.reads == 0  # P14: an agent's list never makes the Keychain ask
    finally:
        built.close()


def test_a_regenerated_token_refuses_the_old_one_at_once(container):
    with serving(container) as port:
        assert call_tool(port, "list_connections")[0] is False
        new = container.agent_host.regenerate().token
        refused = httpx.post(f"http://127.0.0.1:{port}/mcp", json={}, headers={"Authorization": f"Bearer {MCP_TOKEN}"})
        assert refused.status_code == 401
        assert call_tool(port, "list_connections", token=new)[0] is False


def test_the_settings_switch_starts_and_stops_the_server(container):
    port = free_port()
    with TestClient(create_app(container.settings, container), base_url="http://127.0.0.1") as client:
        assert client.get("/mcp/status", headers=AUTH).json()["state"] == "off"
        settings = client.get("/settings", headers=AUTH).json()
        assert settings["mcp"] == {"enabled": False, "port": None}
        settings["mcp"] = {"enabled": True, "port": port}
        assert client.put("/settings", json=settings, headers=AUTH).status_code == 200
        wait_for_state(container, "listening")
        status = client.get("/mcp/status", headers=AUTH).json()
        assert status["port"] == port and status["url"] == f"http://127.0.0.1:{port}/mcp"
        settings["mcp"] = {"enabled": False, "port": port}
        client.put("/settings", json=settings, headers=AUTH)
        assert client.get("/mcp/status", headers=AUTH).json()["state"] == "off"
        for path in ("/mcp/status", "/mcp/token"):
            assert client.get(path).status_code == 401  # the API token guards them too


class _SlowTokens(InMemoryTokenStore):
    """A Keychain read that waits, as one does while the Keychain asks the user to allow it."""

    def __init__(self) -> None:
        super().__init__(MCP_TOKEN)
        self.entered = threading.Event()
        self.release = threading.Event()

    def get(self):
        self.entered.set()
        self.release.wait(10)
        return super().get()


def test_a_slow_keychain_read_never_holds_up_the_status(tmp_path):
    tokens = _SlowTokens()
    built = AppContainer(Settings(token=TOKEN, data_dir=tmp_path / "data", log_to_file=False),
                         secret_store=InMemorySecretStore(), token_store=tokens)
    try:
        built.agent_host.apply(McpSettings(enabled=True, port=free_port()))
        assert tokens.entered.wait(10)  # the MCP thread is reading the token now
        asked = time.monotonic()
        assert built.agent_host.status().state == "starting"
        assert time.monotonic() - asked < 1  # GET /mcp/status and PUT /settings answer meanwhile
        tokens.release.set()
        wait_for_state(built, "listening")
    finally:
        tokens.release.set()
        built.close()


def test_a_change_before_the_delayed_start_wins(container):
    port = free_port()
    container.agent_host.start_later(McpSettings(enabled=True, port=port))
    container.agent_host.apply(McpSettings(enabled=False, port=port))  # the user turned it off within the first second
    time.sleep(START_DELAY_S + 0.5)
    assert container.agent_host.status().state == "off"
    container.agent_host.start_later(McpSettings(enabled=True, port=port))
    container.agent_host.stop()  # the app closed within the first second
    time.sleep(START_DELAY_S + 0.5)
    assert container.agent_host.status().state == "off"


def test_an_app_that_fails_to_start_is_reported_and_frees_the_port(container, monkeypatch):
    from dbaguy_backend.agent import server

    async def failing(scope, receive, send):
        assert scope["type"] == "lifespan"
        await receive()
        await send({"type": "lifespan.startup.failed", "message": "zz_08"})

    monkeypatch.setattr(server, "build_app", lambda *args, **kwargs: failing)
    port = free_port()
    container.agent_host.apply(McpSettings(enabled=True, port=port))
    wait_for_state(container, "error")  # uvicorn leaves through SystemExit: never stuck at `starting`
    assert container.agent_host.status().message == "The MCP server stopped while starting"
    container.agent_host.apply(McpSettings(enabled=False, port=port))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def test_a_websocket_never_reaches_the_sdk():
    from dbaguy_backend.agent.server import BearerGate

    reached, sent = [], []

    async def inner(scope, receive, send):
        reached.append(scope["type"])

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    gate = BearerGate(inner, token=lambda: MCP_TOKEN, port=17532)
    headers = [(b"host", b"127.0.0.1:17532"), (b"authorization", f"Bearer {MCP_TOKEN}".encode())]
    anyio.run(gate, {"type": "websocket", "path": "/mcp", "headers": headers}, receive, send)
    assert reached == [] and sent == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.parametrize("family, address, v6only", [
    (socket.AF_INET, "0.0.0.0", None),
    (socket.AF_INET6, "::", 0),  # dual-stack
    (socket.AF_INET6, "::", 1),
    (socket.AF_INET6, "::1", None),
], ids=["0.0.0.0", "[::] dual-stack", "[::] v6only", "[::1]"])
def test_a_port_another_program_holds_on_any_address_is_in_use(container, family, address, v6only):
    """Ruling M1: DbaGuy never shares a port with another program (a SO_REUSEADDR bind of 127.0.0.1 succeeds beside
    these on macOS, and would take 127.0.0.1's traffic or leave `localhost`'s to the other program)."""
    port = free_port()
    holder = socket.socket(family, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as most servers set it
    if v6only is not None:
        holder.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
    holder.bind((address, port))
    holder.listen(1)
    try:
        container.agent_host.apply(McpSettings(enabled=True, port=port))
        wait_for_state(container, "port_in_use")
        assert f"Port {port} is in use" in container.agent_host.status().message
    finally:
        holder.close()
        container.agent_host.stop()


def test_a_restart_beside_its_own_closed_connections_listens(container):
    """DbaGuy's own TIME_WAIT sockets on the port (the server closed first) never count as another program."""
    port = free_port()
    container.agent_host.apply(McpSettings(enabled=True, port=port))
    wait_for_state(container, "listening")
    with httpx.Client() as client:
        assert client.post(f"http://127.0.0.1:{port}/mcp", json={}).status_code == 401  # kept alive
        container.agent_host.apply(McpSettings(enabled=False, port=port))  # the server closes it first
    container.agent_host.apply(McpSettings(enabled=True, port=port))
    wait_for_state(container, "listening")
    container.agent_host.stop()


class _SlowWrites(InMemoryTokenStore):
    """A Keychain write that waits, as one does while the Keychain asks the user to allow it."""

    def __init__(self) -> None:
        super().__init__(MCP_TOKEN)
        self.entered = threading.Event()
        self.release = threading.Event()

    def set(self, value):
        self.entered.set()
        self.release.wait(10)
        super().set(value)


def test_requests_are_answered_while_regenerate_waits_on_the_keychain(tmp_path):
    tokens = _SlowWrites()
    built = AppContainer(Settings(token=TOKEN, data_dir=tmp_path / "data", log_to_file=False),
                         secret_store=InMemorySecretStore(), token_store=tokens)
    try:
        with serving(built) as port:
            regenerating = threading.Thread(target=built.agent_host.regenerate)
            regenerating.start()
            assert tokens.entered.wait(10)
            asked = time.monotonic()
            answer = httpx.post(f"http://127.0.0.1:{port}/mcp", json={}, timeout=10,
                                headers={"Authorization": f"Bearer {MCP_TOKEN}"})
            assert answer.status_code != 401 and time.monotonic() - asked < 0.5  # the old token, at once
            tokens.release.set()
            regenerating.join(10)
            assert call_tool(port, "list_connections", token=built.agent_host.current_token())[0] is False
    finally:
        tokens.release.set()
        built.close()


def test_a_run_superseded_during_a_slow_token_read_never_binds(tmp_path, monkeypatch):
    from dbaguy_backend.agent import host

    monkeypatch.setattr(host, "STOP_JOIN_S", 0.2)  # the stop gives up on the run that waits on the Keychain
    binds, real_listen = [], host.listen_socket

    def listen(*args, **kwargs):
        binds.append(threading.current_thread().name)
        return real_listen(*args, **kwargs)

    monkeypatch.setattr(host, "listen_socket", listen)
    tokens = _SlowTokens()
    built = AppContainer(Settings(token=TOKEN, data_dir=tmp_path / "data", log_to_file=False),
                         secret_store=InMemorySecretStore(), token_store=tokens)
    port = free_port()
    try:
        built.agent_host.apply(McpSettings(enabled=True, port=port))
        assert tokens.entered.wait(10)
        built.agent_host.apply(McpSettings(enabled=False, port=port))  # off and on again while the Keychain asks
        built.agent_host.apply(McpSettings(enabled=True, port=port))
        tokens.release.set()
        wait_for_state(built, "listening")
        for thread in threading.enumerate():
            if thread.name == "mcp-server-1":
                thread.join(5)
        assert binds == ["mcp-server-2"]  # the superseded run never bound: it cannot take the port from the new one
        assert built.agent_host.status().state == "listening"
    finally:
        tokens.release.set()
        built.close()
