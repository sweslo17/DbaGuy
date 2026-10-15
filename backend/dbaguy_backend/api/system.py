from typing import Literal

from fastapi import APIRouter, Depends, Request
from loguru import logger
from pydantic import BaseModel

from ..container import AppContainer
from ..domain.profiles import EngineInfo
from ..version import app_version
from .deps import get_container, require_token

router = APIRouter(prefix="/system", tags=["system"], dependencies=[Depends(require_token)])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    logger.info("Health check (origin={})", request.headers.get("origin"))
    return HealthResponse(status="ok", version=app_version())


@router.get("/engines", response_model=list[EngineInfo])
def engines(container: AppContainer = Depends(get_container)) -> list[EngineInfo]:
    return container.registry.engines()
