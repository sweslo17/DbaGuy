from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import WrapValidator

from ..container import AppContainer
from ..domain.workspace import WorkspaceState
from ..services.workspace_service import validated_workspace
from .deps import get_container, require_token

router = APIRouter(prefix="/workspace", tags=["workspace"], dependencies=[Depends(require_token)])


@router.get("", response_model=WorkspaceState)
def get_workspace(container: AppContainer = Depends(get_container)) -> WorkspaceState:
    return container.workspace.get()


@router.put("", status_code=204)
def put_workspace(
    body: Annotated[WorkspaceState, WrapValidator(validated_workspace)],
    container: AppContainer = Depends(get_container),
) -> Response:
    container.workspace.put(body)
    return Response(status_code=204)
