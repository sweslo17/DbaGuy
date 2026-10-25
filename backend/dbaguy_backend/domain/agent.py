"""DbaGuy's MCP server (spec ⑧): what the Settings API answers and the codes a tool's error carries (§4)."""

from typing import Literal

from pydantic import BaseModel

from .profiles import McpAccess

__all__ = ["McpAccess", "McpState", "McpStatus", "McpToken", "ToolErrorCode", "APP_MCP_PORT", "DEV_MCP_PORT"]

APP_MCP_PORT = 17531  # the app's own data dir (R-08-2)
DEV_MCP_PORT = 17532  # any other data dir: dev/data, tests, a scratch sidecar

# off: the switch is off; starting: importing the SDK and binding; listening: serving; port_in_use: another program
# holds the port; error: anything else (the message says what)
McpState = Literal["off", "starting", "listening", "port_in_use", "error"]

# §4: every tool error is a result with one of these codes, never an exception the SDK logs (D-08-6)
ToolErrorCode = Literal[
    "unknown_connection", "ambiguous", "not_allowed", "refused", "session_changed", "password_required",
    "connection_failed", "query_failed", "timeout", "cancelled", "busy", "not_found", "invalid_argument",
    "internal_error",
]


class McpStatus(BaseModel):
    state: McpState
    port: int  # the port it uses or would use
    url: str  # what an MCP client connects to: http://127.0.0.1:<port>/mcp
    message: str | None = None  # why it is not listening (port_in_use, error)
    shared_schema: int = 0  # connections whose access is `schema`
    shared_query: int = 0  # connections whose access is `query`


class McpToken(BaseModel):
    """The bearer token MCP clients send, and the Claude Code commands that register it (§3.8). Never logged."""

    token: str
    url: str
    command: str  # claude mcp add … (user scope)
    replace_command: str  # after Regenerate: remove the old registration first
