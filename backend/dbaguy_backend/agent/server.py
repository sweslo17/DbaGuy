"""The MCP endpoint (spec ⑧ §3.2): a new MCPServer with DbaGuy's tools, its Streamable HTTP app (D-08-9), and the bearer
gate in front of it. The only module that imports the MCP SDK, and only `AgentHost` imports it, when the switch is on
(D-08-8): with the switch off the SDK is never loaded (Q1)."""

import json
import re
import time
from collections.abc import Callable
from typing import Annotated, Any

import anyio
from loguru import logger
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ..domain.metadata import PathSegment
from ..domain.well_formed import well_formed
from ..tokens import same_token
from ..version import app_version
from .tools import LIMIT_MAX, LIST_LIMIT, SEARCH_LIMIT, AgentTools, ToolFailure

MCP_PATH = "/mcp"
MAX_BODY_BYTES = 1024 * 1024  # R-08-4
MAX_SESSIONS = 32  # 2025-11-25 sessions (D-08-9)
SESSION_IDLE_S = 1800
CLIENT_NAME_CHARS = 80  # D-08-7
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
INSTRUCTIONS = (
    "DbaGuy shares some of the user's database connections, read-only. Start with list_connections (it also shows "
    "what each login is allowed to do). Explore with list_objects (pass back the `path` it returns), search_objects "
    "and describe. run_query and explain need the connection's 'Read-only queries' access; each call runs exactly one "
    "SELECT/WITH/VALUES/TABLE/SHOW statement, inside a read-only transaction that is rolled back. Results are capped "
    "(max_rows, max_cell_chars, about 40,000 characters, timeout up to 60 s). SQL Server connections are read-only by "
    "DbaGuy's checks only. Couchbase takes a query_context (bucket.scope)."
)
_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
Connection = Annotated[str, Field(min_length=1, max_length=200, description="A connection's id or name")]
ObjectPath = Annotated[list[PathSegment], Field(max_length=12, description="A `path` list_objects or search_objects "
                                                                           "returned, passed back as it is")]
Name = Annotated[str, Field(min_length=1, max_length=1000)]


class BearerGate:
    """Outermost (R-08-4, §3.2): the bearer token (401 with `WWW-Authenticate: Bearer`), then the Host (421: only
    127.0.0.1:<port> and localhost:<port>), then any Origin (403: CLI clients send none, a web page always does). A
    refused request never reaches the SDK. `token` is read per request, so a regenerated token applies at once."""

    def __init__(self, app: ASGIApp, *, token: Callable[[], str], port: int) -> None:
        self._app = app
        self._token = token
        self._hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":  # a WebSocket: MCP never uses one, so it never reaches the SDK (403)
            logger.info("MCP request refused: websocket")
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = Headers(scope=scope)
        refusal: Response | None = None
        if not same_token(headers.get("authorization", ""), f"Bearer {self._token()}"):
            refusal = Response("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"})
        elif headers.get("host") not in self._hosts:
            refusal = Response("Invalid Host header", status_code=421)
        elif "origin" in headers:
            refusal = Response("Origin not allowed", status_code=403)
        if refusal is None:
            await self._app(scope, receive, send)
            return
        logger.info("MCP request refused: {}", refusal.status_code)  # never a header's value: one may be a token
        await refusal(scope, receive, send)


def client_name(ctx: Context) -> str | None:
    """The name the client gave for itself (D-08-7): `initialize`'s clientInfo (2025-11-25) or the request's `_meta`
    (2026-07-28); self-reported, never an identity. At most 80 characters, without control characters."""
    try:
        params = ctx.session.client_params
    except Exception:  # outside a request
        return None
    name = params.client_info.name if params is not None else None
    if not name:
        return None
    return well_formed(_CONTROL.sub("", name))[:CLIENT_NAME_CHARS] or None


def _text(data: dict[str, Any], *, error: bool = False) -> CallToolResult:
    """One compact JSON text, no structured content (D-08-6: the data would be sent twice)."""
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return CallToolResult(content=[TextContent(type="text", text=body)], is_error=error)


def failure_result(failure: ToolFailure) -> CallToolResult:
    return _text({"error": {"code": failure.code, "message": failure.message}}, error=True)


async def run_tool(tool: str, client: str | None, call: Callable[[], dict[str, Any]]) -> CallToolResult:
    """`call` on a worker thread; its data or its failure as a result, never an exception the SDK would log with its
    message (D-08-6). An unexpected error is logged by its type only above DEBUG."""
    started = time.monotonic()
    try:
        return _text(await anyio.to_thread.run_sync(call))
    except ToolFailure as failure:
        if not failure.logged:
            elapsed = int((time.monotonic() - started) * 1000)
            logger.info("MCP {} by {}: {} ({} ms)", tool, client, failure.code, elapsed)
        return failure_result(failure)
    except Exception as exc:
        logger.error("MCP {} by {} failed unexpectedly ({})", tool, client, type(exc).__name__)
        logger.opt(exception=exc).debug("MCP {} failed", tool)
        return failure_result(ToolFailure("internal_error", "Internal error"))


def build_server(tools: AgentTools) -> MCPServer:
    """A new server each start: the SDK's session manager runs once per instance (D-08-8)."""
    server = MCPServer("dbaguy", title="DbaGuy", instructions=INSTRUCTIONS, version=app_version())

    async def list_connections(ctx: Context) -> CallToolResult:
        client = client_name(ctx)
        return await run_tool("list_connections", client, lambda: tools.list_connections(client=client))

    server.add_tool(
        list_connections, name="list_connections", annotations=_READ_ONLY, structured_output=False,
        description=(
            "Read-only. The database connections the user shares with AI tools in DbaGuy: id, name, engine, access "
            "('schema': names and definitions only; 'query': also run_query and explain), the default database, "
            "whether the server or only DbaGuy enforces read-only, whether it needs unlocking in DbaGuy first, and "
            "what the login may do beyond reading (null until a call has used the connection)."
        ),
    )

    async def list_objects(
        connection: Connection, ctx: Context, path: ObjectPath | None = None,
        limit: Annotated[int, Field(ge=1, le=LIMIT_MAX)] = LIST_LIMIT,
    ) -> CallToolResult:
        client = client_name(ctx)
        return await run_tool("list_objects", client,
                              lambda: tools.list_objects(connection, path, limit, client=client))

    server.add_tool(
        list_objects, name="list_objects", annotations=_READ_ONLY, structured_output=False,
        description=(
            "Read-only. Needs 'schema' access or more. The objects one level under `path` (none: the top level: "
            "databases, schemas or buckets): kind, name, `path` (pass it back to go deeper or to describe), whether it "
            "has children, a short detail. Reads no rows."
        ),
    )

    async def search_objects(
        connection: Connection, pattern: Name, ctx: Context, database: Name | None = None,
        include_columns: bool = False, limit: Annotated[int, Field(ge=1, le=LIMIT_MAX)] = SEARCH_LIMIT,
    ) -> CallToolResult:
        client = client_name(ctx)
        return await run_tool("search_objects", client, lambda: tools.search_objects(
            connection, pattern, database, include_columns, limit, client=client))

    server.add_tool(
        search_objects, name="search_objects", annotations=_READ_ONLY, structured_output=False,
        description=(
            "Read-only. Needs 'schema' access or more. Tables, views and Couchbase collections whose name matches "
            "`pattern` (exact, prefix, substring, then fuzzy; `schema.name` narrows by schema, `database` by database "
            "or bucket); with include_columns, also those with a column whose name contains `pattern`. Reads no rows."
        ),
    )

    async def describe(
        connection: Connection, ctx: Context, path: ObjectPath | None = None, database: Name | None = None,
        schema: Name | None = None, name: Name | None = None, include_ddl: bool = False,
    ) -> CallToolResult:
        client = client_name(ctx)
        return await run_tool("describe", client, lambda: tools.describe(
            connection, path, database, schema, name, include_ddl, client=client))

    server.add_tool(
        describe, name="describe", annotations=_READ_ONLY, structured_output=False,
        description=(
            "Read-only. Needs 'schema' access or more. One table, view or collection, by `path` or by name (`schema` "
            "and `database` narrow it): columns with types, nullability and defaults, primary key, foreign keys, "
            "indexes, comment; its DDL with include_ddl. Reads no rows."
        ),
    )
    return server


def build_app(tools: AgentTools, *, port: int, token: Callable[[], str]) -> ASGIApp:
    """The ASGI app the MCP thread serves on 127.0.0.1:`port`."""
    security = TransportSecuritySettings(  # the SDK checks again; BearerGate has refused anything else already
        allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"], allowed_origins=[],
    )
    # json_response=False (P18): only the SDK's SSE path notices a 2026-07-28 client closing its request's stream,
    # which is how that client cancels a call (A4). A call that ends within the SDK's 15 s still answers JSON there;
    # 2025-11-25 answers as an SSE stream and cancels with notifications/cancelled.
    app = build_server(tools).streamable_http_app(
        streamable_http_path=MCP_PATH, json_response=False, stateless_http=False, max_sessions=MAX_SESSIONS,
        session_idle_timeout=SESSION_IDLE_S, max_request_body_size=MAX_BODY_BYTES, transport_security=security,
    )
    return BearerGate(app, token=token, port=port)
