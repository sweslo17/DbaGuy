from fastapi import APIRouter, Depends

from ..container import AppContainer
from ..domain.metadata import MetadataRequest, ObjectDetail, ObjectNode
from .deps import get_container, require_token

router = APIRouter(prefix="/metadata", tags=["metadata"], dependencies=[Depends(require_token)])


@router.post("/children", response_model=list[ObjectNode])
def children(body: MetadataRequest, container: AppContainer = Depends(get_container)) -> list[ObjectNode]:
    return container.metadata.children(body)


@router.post("/detail", response_model=ObjectDetail)
def detail(body: MetadataRequest, container: AppContainer = Depends(get_container)) -> ObjectDetail:
    return container.metadata.detail(body)
