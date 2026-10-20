"""Grid editing (spec ⑤ §3.4): a table's edit target and the preview of a commit. The commit itself is the session's
`apply_edits` WebSocket command."""

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..container import AppContainer
from ..domain.edits import EditChanges, EditPreview, EditTarget
from ..domain.metadata import PathSegment
from .deps import get_container, require_token

router = APIRouter(prefix="/edits", tags=["edits"], dependencies=[Depends(require_token)])


class EditTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    path: list[PathSegment] = Field(min_length=1)  # the table's explorer path, as the table-data tab opened it


class EditPreviewRequest(EditTargetRequest):
    changes: EditChanges


@router.post("/target", response_model=EditTarget)
def edit_target(body: EditTargetRequest, container: AppContainer = Depends(get_container)) -> EditTarget:
    return container.edits.target(body.profile_id, body.path)


@router.post("/preview", response_model=EditPreview)
def edit_preview(body: EditPreviewRequest, container: AppContainer = Depends(get_container)) -> EditPreview:
    return container.edits.preview(body.profile_id, body.path, body.changes)
