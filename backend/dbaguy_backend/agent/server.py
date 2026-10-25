"""The MCP endpoint (spec ⑧ §3.2): a new MCPServer with DbaGuy's tools, its Streamable HTTP app (D-08-9), and the bearer
gate in front of it. The only module that imports the MCP SDK, and only `AgentHost` imports it, when the switch is on
(D-08-8): with the switch off the SDK is never loaded (Q1)."""

import threading
import time
from collections.abc import Callable
from functools import partial
from typing import Annotated, Any

import anyio
from loguru import logger
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from mcp.types.version import KNOWN_PROTOCOL_VERSIONS
from pydantic import Field
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..domain.metadata import PathSegment
from ..domain.well_formed import well_formed
from ..tokens import same_token
from ..version import app_version
from .queries import SAMPLE_MAX, SQL_MAX, TIMEOUT_DEFAULT_S, TIMEOUT_MAX_S, QueryTools
from .reader import CallControl
from .sessions import MAX_AGENT_SESSIONS
from .shape import ERROR_CHARS, MAX_CELL, MAX_CELL_DEFAULT, MAX_ROWS, MAX_ROWS_DEFAULT, compact, note
from .tools import CONTROL_CHARS, LIMIT_MAX, LIST_LIMIT, SEARCH_LIMIT, AgentTools, ToolFailure

MCP_PATH = "/mcp"
MAX_BODY_BYTES = 1024 * 1024  # R-08-4
MAX_SESSIONS = 32  # 2025-11-25 sessions (D-08-9)
SESSION_IDLE_S = 1800
CLIENT_NAME_CHARS = 80  # D-08-7
# Tool calls whose worker thread has not ended, at most (review I2): a call the client gives up on leaves its thread
# behind (anyio stops counting it), so a client cancelling calls in a loop could otherwise grow the threads without end
THREADS_MAX = 2 * MAX_AGENT_SESSIONS
_TOO_MANY = f"DbaGuy is still running {THREADS_MAX} calls from AI tools: try again when one of them ends"
INSTRUCTIONS = (
    "DbaGuy shares some of the user's database connections, read-only. Start with list_connections (it also shows "
    "what each login is allowed to do). Explore with list_objects (pass back the `path` it returns), search_objects "
    "and describe. run_query and explain need the connection's 'Read-only queries' access; each call runs exactly one "
    "SELECT/WITH/VALUES/TABLE/SHOW statement, inside a read-only transaction that is rolled back. Results are capped "
    "(max_rows, max_cell_chars, about 40,000 bytes of JSON, timeout up to 60 s). SQL Server connections are read-only "
    "by DbaGuy's checks only. Couchbase takes a query_context (bucket.scope)."
)
_STOPPING = "The MCP server is stopping"
_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
Connection = Annotated[str, Field(min_length=1, max_length=200, description="A connection's id or name")]
Name = Annotated[str, Field(min_length=1, max_length=1000)]
Sql = Annotated[str, Field(min_length=1, max_length=SQL_MAX, description="One statement")]
QueryContextText = Annotated[str, Field(min_length=1, max_length=400, description="Couchbase: bucket or bucket.scope")]
Timeout = Annotated[float, Field(gt=0, le=TIMEOUT_MAX_S, description=f"Seconds, default {TIMEOUT_DEFAULT_S}; "
                                                                        "at most 30 on production (red) connections")]


class ObjectPathSegment(PathSegment):
    """A path segment as an agent may send it: a name at most as long as `Name`, so no error echoes a huge one back."""

    kind: str = Field(min_length=1, max_length=40)
    name: str = Field(max_length=1000)


ObjectPath = Annotated[list[ObjectPathSegment], Field(max_length=12, description="A `path` list_objects or "
                                                                                 "search_objects returned, passed back "
                                                                                 "as it is")]


class BearerGate:
    """Outermost (R-08-4, §3.2): the bearer token (401 with `WWW-Authenticate: Bearer`), then the Host (421: only
    127.0.0.1:<port> and localhost:<port>), then any Origin (403: CLI clients send none, a web page always does). A
    refused request never reaches the SDK. `token` is read per request, so a regenerated token applies at once."""

    def __init__(self, app: ASGIApp, *, token: Callable[[], str], port: int,
                 sessions: StreamableHTTPSessionManager | None = None) -> None:
        self._app = app
        self._token = token
        self._hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
        self._sessions = sessions  # the SDK's 2025-11-25 sessions
        self._serving: set[anyio.CancelScope] = set()  # the requests the SDK is answering
        self._stopping = False

    async def stop_serving(self) -> None:
        """Before uvicorn's graceful shutdown, on the server's loop (Task 1 review m2): end every request the SDK is
        still answering, and refuse new ones (503). An idle 2025-11-25 client holds its GET stream open (as Claude
        Code does), and a 2026-07-28 call answers on its own request's stream; either held each stop for GRACEFUL_S,
        then uvicorn cut it with an ERROR. Ended here, each response is completed (an empty last chunk, or a 503 when
        none started), so uvicorn finds nothing to wait for. A call's statement is cancelled the way a client's cancel
        does it (`run_tool`). A request that comes after (a client reconnecting its stream) gets the 503.

        Then the 2025-11-25 sessions end as a client's DELETE ends one: a call of theirs still running answers into a
        closed stream at once, where the SDK would otherwise wait a second for the one it cut. The SDK has no public
        call that ends them all, so its manager's table is read; without it the stop is only that second slower."""
        self._stopping = True
        serving = list(self._serving)
        for cancel in serving:
            cancel.cancel()
        transports = list(getattr(self._sessions, "_server_instances", {}).values())
        for transport in transports:
            await transport.terminate()
        if serving or transports:
            logger.info("MCP: ended {} open request(s) and {} client session(s) before stopping", len(serving),
                        len(transports))

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
            await self._serve(scope, receive, send)
            return
        logger.info("MCP request refused: {}", refusal.status_code)  # never a header's value: one may be a token
        await refusal(scope, receive, send)

    async def _serve(self, scope: Scope, receive: Receive, send: Send) -> None:
        """The SDK answers the request, unless `stop_serving` ends it first: then its response is completed here."""
        if self._stopping:
            await Response(_STOPPING, status_code=503)(scope, receive, send)
            return
        started = done = False

        async def sending(message: Message) -> None:
            nonlocal started, done
            if message["type"] == "http.response.start":
                started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                done = True
            await send(message)

        with anyio.CancelScope() as cancel:
            self._serving.add(cancel)
            try:
                await self._app(scope, receive, sending)
            finally:
                self._serving.discard(cancel)
        if not self._stopping or done:
            return
        if started:  # a stream (SSE) the stop cut: its last chunk, so the client sees it end
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        else:
            await Response(_STOPPING, status_code=503)(scope, receive, send)


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
    return well_formed(CONTROL_CHARS.sub("", name))[:CLIENT_NAME_CHARS] or None


def protocol_named(version: object) -> str:
    """A protocol version as the log may say it: one the SDK knows, else "other" (never the client's own text)."""
    return version if isinstance(version, str) and version in KNOWN_PROTOCOL_VERSIONS else "other"


_STARTS = frozenset({"initialize", "server/discover"})


async def log_client_start(ctx: Any, call_next: Callable[[Any], Any]) -> Any:
    """Server middleware (the SDK's `ServerMiddleware`; final review, Task 9's A1): one INFO line when a client starts,
    with its protocol version: 2025-11-25's `initialize` (the version the server answered with) and 2026-07-28's
    `server/discover` probe. A 2026-07-28 client that adopts the version without the probe sends neither, so it has no
    line. Never the client's own text (its name is at DEBUG in each tool's line)."""
    result = await call_next(ctx)
    if ctx.method in _STARTS and ctx.request_id is not None:
        answered = result.get("protocolVersion") if isinstance(result, dict) else None
        logger.info("MCP: a client started (protocol {})",
                    protocol_named(answered if ctx.method == "initialize" else ctx.protocol_version))
    return result


def _text(data: dict[str, Any], *, error: bool = False) -> CallToolResult:
    """One compact JSON text, no structured content (D-08-6: the data would be sent twice)."""
    return CallToolResult(content=[TextContent(type="text", text=compact(data))], is_error=error)


def failure_result(failure: ToolFailure) -> CallToolResult:
    """Every tool's error, its message cut (review I4: a server's text can quote a value of any size). The query tools
    cut theirs at ERROR_CHARS before adding the code; this bounds the rest (answered's query_failed and others)."""
    message = note(failure.message, ERROR_CHARS + 100)
    return _text({"error": {"code": failure.code, "message": message}}, error=True)


class _Threads:
    """How many tool calls' worker threads are still running, counted from the thread itself: a thread a cancel
    abandoned counts until it ends (review I2)."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._running = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self._running >= self._limit:
                return False
            self._running += 1
            return True

    def give(self) -> None:
        with self._lock:
            self._running -= 1


_THREADS = _Threads(THREADS_MAX)


class _Ticket:
    """One call's place in `_THREADS`: its thread gives it back when it ends; a thread that never started (the call
    was cancelled before) never runs the call, and the place is given back at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = False
        self._dropped = False

    def run(self, call: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
        with self._lock:
            if self._dropped:
                return None
            self._started = True
        try:
            return call()
        finally:
            _THREADS.give()

    def drop_unstarted(self) -> None:
        with self._lock:
            if self._started or self._dropped:
                return
            self._dropped = True
        _THREADS.give()


async def run_tool(tool: str, client: str | None, call: Callable[[], dict[str, Any]],
                   control: CallControl | None = None) -> CallToolResult:
    """`call` on a worker thread; its data or its failure as a result, never an exception the SDK would log with its
    message (D-08-6). An unexpected error is logged by its type only above DEBUG. Above DEBUG a line names the tool,
    the connection's id, the outcome and the time, never what the agent sent or calls itself (fix round 1, I-2).

    The client cancelling the request (A4: 2025-11-25's notifications/cancelled, or a 2026-07-28 client closing its
    request's stream) cancels this task, as a stop does: the thread is abandoned at once, and `control` cancels the
    call's statement from a thread of its own (a PostgreSQL cancel can wait on the server for seconds). The call then
    ends on its own thread, `cancelled`, and writes its History record."""
    started = time.monotonic()
    if not _THREADS.take():  # review I2: without a thread, at once
        logger.info("MCP {}: busy, {} calls still running", tool, THREADS_MAX)
        return failure_result(ToolFailure("busy", _TOO_MANY))
    ticket = _Ticket()
    try:
        return _text(await anyio.to_thread.run_sync(partial(ticket.run, call), abandon_on_cancel=True))
    except anyio.get_cancelled_exc_class():
        if control is not None:
            threading.Thread(target=control.cancel, name=f"mcp-cancel-{tool}", daemon=True).start()
        # the client gave up, or the MCP server is stopping (BearerGate.stop_serving)
        logger.info("MCP {}: cancelled after {} ms", tool, int((time.monotonic() - started) * 1000))
        logger.debug("MCP {} by {!r}: cancelled", tool, client)
        raise
    except ToolFailure as failure:
        if not failure.logged:
            elapsed = int((time.monotonic() - started) * 1000)
            on = f" on {failure.connection}" if failure.connection else ""
            logger.info("MCP {}{}: {}, {} ms", tool, on, failure.code, elapsed)
            logger.debug("MCP {} by {!r}: {}", tool, client, failure.code)
        return failure_result(failure)
    except Exception as exc:
        logger.error("MCP {} failed unexpectedly ({})", tool, type(exc).__name__)
        logger.opt(exception=exc).debug("MCP {} by {!r} failed", tool, client)
        return failure_result(ToolFailure("internal_error", "Internal error"))
    finally:
        ticket.drop_unstarted()


def build_server(tools: AgentTools, queries: QueryTools) -> MCPServer:
    """A new server each start: the SDK's session manager runs once per instance (D-08-8)."""
    server = MCPServer("dbaguy", title="DbaGuy", instructions=INSTRUCTIONS, version=app_version(),
                       middleware=[log_client_start])

    async def list_connections(ctx: Context) -> CallToolResult:
        client = client_name(ctx)
        return await run_tool("list_connections", client, lambda: tools.list_connections(client=client))

    server.add_tool(
        list_connections, name="list_connections", annotations=_READ_ONLY, structured_output=False,
        description=(
            "Read-only. The database connections the user shares with AI tools in DbaGuy: id, name, engine, access "
            "('schema': names and definitions only; 'query': also run_query and explain), the default database, "
            "whether the server or only DbaGuy enforces read-only, whether it needs unlocking in DbaGuy first, and "
            "what the login may do beyond reading (null until a call has used the connection). A login that may do "
            "more than read is only fully contained by a duplicate of the connection whose login can only read, which "
            "the user can share instead."
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

    async def run_query(
        connection: Connection, sql: Sql, ctx: Context, database: Name | None = None,
        query_context: QueryContextText | None = None,
        max_rows: Annotated[int, Field(ge=1, le=MAX_ROWS)] = MAX_ROWS_DEFAULT,
        max_cell_chars: Annotated[int, Field(ge=1, le=MAX_CELL)] = MAX_CELL_DEFAULT, timeout_s: Timeout | None = None,
    ) -> CallToolResult:
        client, control = client_name(ctx), CallControl()
        return await run_tool("run_query", client, lambda: queries.run_query(
            connection, sql, database, query_context, max_rows, max_cell_chars, timeout_s, client=client,
            control=control), control)

    server.add_tool(
        run_query, name="run_query", annotations=_READ_ONLY, structured_output=False,
        description=(
            "Read-only. Needs 'Read-only queries' access. Runs exactly one SELECT, WITH, VALUES, TABLE or SHOW "
            "statement (SQLite: SELECT, WITH or VALUES; SQL Server and Couchbase: SELECT or WITH) in a read-only "
            "transaction that is rolled back. "
            "Returns columns, rows (big integers as strings; long text cut at max_cell_chars, with its size), "
            "row_count, more_rows, and notes. At most max_rows rows (100 by default, 1000 at most) and about 40,000 "
            "bytes; timeout_s 30 s by default, 60 at most (30 on production connections). DbaGuy checks every "
            "statement and rolls it back; for full protection the user shares a duplicate of the connection whose "
            "login can only read."
        ),
    )

    async def explain(
        connection: Connection, sql: Sql, ctx: Context, database: Name | None = None,
        query_context: QueryContextText | None = None, timeout_s: Timeout | None = None,
    ) -> CallToolResult:
        client, control = client_name(ctx), CallControl()
        return await run_tool("explain", client, lambda: queries.explain(
            connection, sql, database, query_context, timeout_s, client=client, control=control), control)

    server.add_tool(
        explain, name="explain", annotations=_READ_ONLY, structured_output=False,
        description=(
            "Read-only. Needs 'Read-only queries' access. The plan of one read statement (the statement run_query "
            "would take), without running it: no ANALYZE. Pass the statement without EXPLAIN; DbaGuy adds it "
            "(SQL Server: SHOWPLAN_TEXT). Couchbase also lists findings such as primary or uncovered scans."
        ),
    )

    async def sample_fields(
        connection: Connection, bucket: Name, scope: Name, collection: Name, ctx: Context,
        size: Annotated[int, Field(ge=1, le=SAMPLE_MAX)] | None = None,
    ) -> CallToolResult:
        client = client_name(ctx)
        return await run_tool("sample_fields", client, lambda: queries.sample_fields(
            connection, bucket, scope, collection, size, client=client))

    server.add_tool(
        sample_fields, name="sample_fields", annotations=_READ_ONLY, structured_output=False,
        description=(
            "Read-only. Needs 'Read-only queries' access; Couchbase only. The field paths of a collection's "
            "documents, from a sample (the last one DbaGuy took, or a new one of `size` documents, at most 1000): "
            "each path with its JSON types and how many documents have it."
        ),
    )
    return server


def build_app(tools: AgentTools, queries: QueryTools, *, port: int, token: Callable[[], str]) -> BearerGate:
    """The ASGI app the MCP thread serves on 127.0.0.1:`port`; `stop_serving` before it stops."""
    security = TransportSecuritySettings(  # the SDK checks again; BearerGate has refused anything else already
        allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"], allowed_origins=[],
    )
    # json_response=False (P18): only the SDK's SSE path notices a 2026-07-28 client closing its request's stream,
    # which is how that client cancels a call (A4). A call that ends within the SDK's 15 s still answers JSON there;
    # 2025-11-25 answers as an SSE stream and cancels with notifications/cancelled.
    server = build_server(tools, queries)
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH, json_response=False, stateless_http=False, max_sessions=MAX_SESSIONS,
        session_idle_timeout=SESSION_IDLE_S, max_request_body_size=MAX_BODY_BYTES, transport_security=security,
    )
    return BearerGate(app, token=token, port=port, sessions=server.session_manager)
