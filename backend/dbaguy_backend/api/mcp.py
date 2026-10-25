"""The MCP server's Settings API (spec ⑧ D-08-12): its state, the token and its Claude Code commands, a new token. The
switch and the port are AppSettings.mcp (`PUT /settings`)."""

from fastapi import APIRouter, Depends

from ..container import AppContainer
from ..domain.agent import McpStatus, McpToken
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
