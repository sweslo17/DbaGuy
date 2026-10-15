from typing import Literal

from fastapi import APIRouter, Depends, Request
from loguru import logger
from pydantic import BaseModel

from ..version import app_version
from .deps import require_token

router = APIRouter(prefix="/system", tags=["system"], dependencies=[Depends(require_token)])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    logger.info("Health check (origin={})", request.headers.get("origin"))
    return HealthResponse(status="ok", version=app_version())
