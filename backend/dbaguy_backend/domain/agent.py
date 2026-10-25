"""DbaGuy's MCP server (spec ⑧): what the Settings API answers and the codes a tool's error carries (§4)."""

from typing import Literal

from pydantic import BaseModel

from .profiles import McpAccess

__all__ = ["McpAccess", "McpState", "McpStatus", "McpToken", "ToolErrorCode", "ToolFailure", "APP_MCP_PORT",
           "DEV_MCP_PORT", "LoginGrant", "LoginPrivileges", "PAGE_RAW_CHARS", "READ_RAW_CHARS"]

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

# What one agent's read holds (§3.6, ruling T6-I1, P10), here so that `sessions/` (the worker's page) and `adapters/`
# (SQLite's length limit, final review I3) need not import `agent/` (final review M7):
PAGE_RAW_CHARS = 8 * 1024 * 1024  # an agent's page ends once its rows reach this many characters, raw (T6-I1)
# Every value one call reads, raw: past it, no more rows are asked for. A few huge values end the call (one 100 MB
# value: that row alone), instead of dozens of them being read for their 1,000-character previews (P10)
READ_RAW_CHARS = 64 * 1024 * 1024


class ToolFailure(Exception):
    """A tool's error result (§4): its code and a message for the agent. `logged`: whoever raised it wrote its log line
    already (a refusal names its reason), so the server does not add a second. `connection`: the id of the profile it
    concerns, once known (the log line names it, never what the agent typed). Here, not in `agent/tools.py`, so the
    reader and the agent sessions raise it without importing the tools (final review M7)."""

    def __init__(self, code: ToolErrorCode, message: str, *, logged: bool = False,
                 connection: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.logged = logged
        self.connection = connection


# What a login may do beyond reading that a read-only transaction does not stop (CR-08-1, §3.3), in this order:
# PostgreSQL superuser, the four pg_* roles and REPLICATION (final review I1: replication slots outlive a ROLLBACK);
# MySQL FILE; SQL Server writes in the database, sysadmin, bulkadmin, VIEW SERVER STATE (its registry, volumes, services)
LoginGrant = Literal[
    "superuser", "server_files_read", "server_files_write", "server_programs", "signal_backends", "replication",
    "file", "can_write", "sysadmin", "bulkadmin", "view_server_state",
]
_GRANT_TEXT: dict[str, str] = {
    "superuser": "is a superuser", "server_files_read": "read the server's files",
    "server_files_write": "write the server's files", "server_programs": "run programs on the server",
    "signal_backends": "end other sessions", "replication": "manage replication slots (REPLICATION)",
    "can_write": "write in this database", "sysadmin": "is a sysadmin", "bulkadmin": "bulk-load files (bulkadmin)",
    "view_server_state": "read the server's registry, volumes and services (VIEW SERVER STATE)",
}
PATH_CHARS = 200  # the server's secure_file_priv, at most this much of it in the summary (T3 N3)


def _file_text(directory: str | None) -> str:
    """MySQL's FILE, with where the server lets it work (`secure_file_priv`): "" any directory, a path only that one,
    NULL no file at all. The path is the server's text: quoted (`repr`, so no newline reaches the INFO line) and cut at
    PATH_CHARS (T3 N3)."""
    if directory is None:
        return "has FILE, but the server allows no file access (secure_file_priv is NULL)"
    if directory == "":
        return "read and write files on the server in any directory (FILE)"
    shown = directory if len(directory) <= PATH_CHARS else directory[:PATH_CHARS] + "…"
    return f"read and write files on the server in {shown!r} (FILE)"


class LoginPrivileges(BaseModel):
    """The connection's login, as an agent's connection found it (CR-08-1): read once, kept until the profile changes.
    Empty `grants`: it can do nothing a read-only transaction does not stop."""

    grants: list[LoginGrant] = []
    # MySQL with `file`: secure_file_priv: "" any directory, a path only that one, None (NULL) no file at all
    secure_file_priv: str | None = None

    def summary(self) -> str:
        """`superuser, run programs on the server`, for the log and the agent: never a secret."""
        texts = [_file_text(self.secure_file_priv) if grant == "file" else _GRANT_TEXT[grant] for grant in self.grants]
        return ", ".join(texts) or "nothing beyond reading"


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
