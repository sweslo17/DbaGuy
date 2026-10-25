"""The MCP server's Settings API (spec ⑧ D-08-12): its state, the token and its Claude Code commands, a new token, and
a connection's login privileges for the connection dialog (CR-08-1). The switch and the port are AppSettings.mcp
(`PUT /settings`)."""

from uuid import UUID

from fastapi import APIRouter, Depends

from ..container import AppContainer
from ..domain.agent import LoginPrivileges, McpStatus, McpToken
from .deps import get_container, require_token

router = APIRouter(prefix="/mcp", tags=["mcp"], dependencies=[Depends(require_token)])


@router.get("/status", response_model=McpStatus)
def mcp_status(container: AppContainer = Depends(get_container)) -> McpStatus:
    return container.agent_host.status()


@router.get("/token", response_model=McpToken)
def mcp_token(container: AppContainer = Depends(get_container)) -> McpToken:
    return container.agent_host.token()


@router.post("/token/regenerate", response_model=McpToken)
def regenerate_mcp_token(container: AppContainer = Depends(get_container)) -> McpToken:
    return container.agent_host.regenerate()


@router.get("/connections/{profile_id}/login", response_model=LoginPrivileges | None)
def connection_login(profile_id: UUID, container: AppContainer = Depends(get_container)) -> LoginPrivileges | None:
    """Read now on the agents' read-only connection (a locked profile: 428 password_required, as every route); null for
    an engine with nothing to tell (SQLite, Couchbase)."""
    return container.agent_login.read(container.profiles.get(profile_id))
