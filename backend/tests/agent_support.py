"""Shared helpers for the MCP server's tests (spec ⑧): a free test port, an MCP client against a real port, a container
whose MCP token never touches the Keychain."""

import json
import random
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from loguru import logger

from dbaguy_backend.agent.token_store import InMemoryTokenStore
from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.profiles import AnyProfile, ProfileUpsertRequest
from dbaguy_backend.domain.settings import McpSettings
from dbaguy_backend.listen import held_elsewhere
from dbaguy_backend.storage.secret_store import InMemorySecretStore

from .support import COUCHBASE_PASSWORD, MSSQL_PASSWORD, MYSQL_PASSWORD, PG_PASSWORD

TOKEN = "test-token"
MCP_TOKEN = "mcp-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


PASSWORDS = {"postgres": PG_PASSWORD, "mysql": MYSQL_PASSWORD, "mssql": MSSQL_PASSWORD,
             "couchbase": COUCHBASE_PASSWORD}


def share(container: AppContainer, profile: AnyProfile, access: str = "query", **changes: Any) -> str:
    """Save `profile` (a support.make_*_profile, or a SqliteProfile) in the container, shared at `access`, with the dev
    password; its id."""
    draft = profile.model_dump(exclude={"id"}) | {"mcp_access": access, **changes}
    request = ProfileUpsertRequest.model_validate({"profile": draft,
                                                   "secrets": {"password": PASSWORDS.get(profile.engine)}})
    return str(container.profiles.create(request).profile.id)


@contextmanager
def logged(level: str = "INFO") -> Iterator[list[tuple[str, str]]]:
    """(level, message) of every log record at `level` and above while the block runs."""
    records: list[tuple[str, str]] = []
    sink = logger.add(lambda m: records.append((m.record["level"].name, m.record["message"])), level=level)
    try:
        yield records
    finally:
        logger.remove(sink)


def free_port() -> int:
    """A port in the settings' range (1024–49151) that nothing listens on now, on any address (ruling M1). Never 17531
    or 17532."""
    for _ in range(200):
        port = random.randint(20_000, 40_000)
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        if held_elsewhere(port):
            continue
        return port
    raise RuntimeError("no free port")


def make_container(data_dir: Path) -> AppContainer:
    settings = Settings(token=TOKEN, data_dir=data_dir, log_to_file=False)
    return AppContainer(settings, secret_store=InMemorySecretStore(), token_store=InMemoryTokenStore(MCP_TOKEN))


def wait_for_state(container: AppContainer, state: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while container.agent_host.status().state != state:
        if time.monotonic() > deadline:
            raise AssertionError(f"MCP state is {container.agent_host.status().state}, not {state}")
        time.sleep(0.02)


@contextmanager
def serving(container: AppContainer) -> Iterator[int]:
    """The container's MCP server listening on a free port, stopped afterwards."""
    port = free_port()
    container.agent_host.apply(McpSettings(enabled=True, port=port))
    try:
        wait_for_state(container, "listening")
        yield port
    finally:
        container.agent_host.stop()


async def _call(port: int, token: str, mode: str, tool: str | None, arguments: dict[str, Any],
                client: str) -> Any:
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    from mcp.types import Implementation

    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        transport = streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http)
        async with Client(transport, mode=mode, client_info=Implementation(name=client, version="1")) as session:
            if tool is None:
                return await session.list_tools()
            return await session.call_tool(tool, arguments)


def call_tool(port: int, tool: str, arguments: dict[str, Any] | None = None, *, mode: str = "legacy",
              token: str = MCP_TOKEN, client: str = "pytest") -> tuple[bool, Any]:
    """(is_error, the JSON of its one text block): one tool call through a new MCP client session."""
    result = anyio.run(_call, port, token, mode, tool, arguments or {}, client)
    assert len(result.content) == 1 and result.content[0].type == "text"
    return result.is_error, json.loads(result.content[0].text)


def list_tools(port: int, *, mode: str = "legacy") -> list[Any]:
    return anyio.run(_call, port, MCP_TOKEN, mode, None, {}, "pytest").tools


@pytest.fixture
def container(tmp_path):
    built = make_container(tmp_path / "data")
    try:
        yield built
    finally:
        built.close()
