from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .metadata import PathSegment


class TextSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor: int = Field(ge=0)  # UTF-16 code units, like every text offset in the API
    head: int = Field(ge=0)


class WorkspaceTab(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    kind: Literal["query", "table", "object"]
    title: str = ""
    profile_id: UUID | None = None
    database: str | None = None
    script_path: str | None = None  # relative to the scripts folder
    object_path: list[PathSegment] | None = None
    buffer: str | None = None  # the editor text, unsaved changes included
    cursor: int | None = Field(default=None, ge=0)  # UTF-16 code units
    selection: TextSelection | None = None
    autocommit: bool | None = None


class WorkspaceState(BaseModel):
    """The open tabs (`workspace.json`), written whole every few seconds so that a crash loses nothing."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    tabs: list[WorkspaceTab] = []
    active_tab_id: str | None = None
    sidebar_tab: str | None = None
