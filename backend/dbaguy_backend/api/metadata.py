from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from ..container import AppContainer
from ..domain.metadata import MetadataRequest, ObjectDetail, ObjectNode
from ..domain.schema import ObjectSearchHit
from .deps import get_container, require_token

router = APIRouter(prefix="/metadata", tags=["metadata"], dependencies=[Depends(require_token)])


class SchemaRefreshRequest(BaseModel):
    profile_id: UUID
    database: str | None = None


@router.post("/children", response_model=list[ObjectNode])
def children(body: MetadataRequest, container: AppContainer = Depends(get_container)) -> list[ObjectNode]:
    return container.metadata.children(body)


@router.post("/detail", response_model=ObjectDetail)
def detail(body: MetadataRequest, container: AppContainer = Depends(get_container)) -> ObjectDetail:
    return container.metadata.detail(body)


@router.get("/search", response_model=list[ObjectSearchHit])
def search(
    profile_id: UUID, q: str = "", database: str | None = None, limit: int = Query(50, ge=1, le=500),
    container: AppContainer = Depends(get_container),
) -> list[ObjectSearchHit]:
    return container.schema_cache.search(profile_id, q, database, limit)


@router.post("/schema/refresh", status_code=204)
def refresh_schema(body: SchemaRefreshRequest, container: AppContainer = Depends(get_container)) -> Response:
    """Forget the cached schema; the next completion or search rebuilds it."""
    container.schema_cache.invalidate(body.profile_id, body.database)
    return Response(status_code=204)
