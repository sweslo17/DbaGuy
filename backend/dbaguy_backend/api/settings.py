from fastapi import APIRouter, Depends

from ..container import AppContainer
from ..domain.settings import AppSettings
from .deps import get_container, require_token

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_token)])


@router.get("", response_model=AppSettings)
def get_settings(container: AppContainer = Depends(get_container)) -> AppSettings:
    return container.app_settings.get()


@router.put("", response_model=AppSettings)
def update_settings(body: AppSettings, container: AppContainer = Depends(get_container)) -> AppSettings:
    saved = container.app_settings.update(body)
    container.agent_host.apply(saved.mcp)  # the MCP switch or port changed: start, stop or restart (D-08-12)
    return saved
