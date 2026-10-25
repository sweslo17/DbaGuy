"""DbaGuy's MCP server (spec ⑧): what the Settings API answers and the codes a tool's error carries (§4)."""

from typing import Literal

from pydantic import BaseModel

from .profiles import McpAccess

__all__ = ["McpAccess", "McpState", "McpStatus", "McpToken", "ToolErrorCode", "APP_MCP_PORT", "DEV_MCP_PORT",
           "LoginGrant", "LoginPrivileges"]

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


# What a login may do beyond reading that a read-only transaction does not stop (CR-08-1, §3.3), in this order:
# PostgreSQL superuser and the four pg_* roles; MySQL FILE; SQL Server writes in the database, sysadmin, bulkadmin
LoginGrant = Literal[
    "superuser", "server_files_read", "server_files_write", "server_programs", "signal_backends", "file", "can_write",
    "sysadmin", "bulkadmin",
]
_GRANT_TEXT: dict[str, str] = {
    "superuser": "is a superuser", "server_files_read": "read the server's files",
    "server_files_write": "write the server's files", "server_programs": "run programs on the server",
    "signal_backends": "end other sessions", "file": "read and write files on the server (FILE)",
    "can_write": "write in this database", "sysadmin": "is a sysadmin", "bulkadmin": "bulk-load files (bulkadmin)",
}


class LoginPrivileges(BaseModel):
    """The connection's login, as an agent's connection found it (CR-08-1): read once, kept until the profile changes.
    Empty `grants`: it can do nothing a read-only transaction does not stop."""

    grants: list[LoginGrant] = []
    # MySQL with `file`: secure_file_priv: "" any directory, a path only that one, None (NULL) no file at all
    secure_file_priv: str | None = None

    def summary(self) -> str:
        """`superuser, run programs on the server`, for the log and the agent: never a secret."""
        return ", ".join(_GRANT_TEXT[grant] for grant in self.grants) or "nothing beyond reading"


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
