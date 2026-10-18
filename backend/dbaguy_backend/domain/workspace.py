from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .execution import PARAM_NAME, QueryContext
from .metadata import PathSegment

# A parameter panel's draft is keyed by the name as the statement writes it (`$name`, `$1`), the text as typed: valid
# JSON or not yet (B14). Bounded like the panel itself (StatementParams: 200 named + 200 positional, 1 MiB a literal).
ParamName = Annotated[str, Field(pattern=rf"^\$(?:{PARAM_NAME}|[1-9][0-9]{{0,2}})$")]
ParamDraft = Annotated[str, Field(max_length=1_048_576)]
MAX_PARAM_DRAFTS = 400
# None follows the profile's default context, "none" is the user's explicit No context (B14)
TabQueryContext = QueryContext | Literal["none"]


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
    query_context: TabQueryContext | None = None  # Couchbase
    params: dict[ParamName, ParamDraft] | None = Field(default=None, max_length=MAX_PARAM_DRAFTS)


class WorkspaceState(BaseModel):
    """The open tabs (`workspace.json`), written whole every few seconds so that a crash loses nothing."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    tabs: list[WorkspaceTab] = []
    active_tab_id: str | None = None
    sidebar_tab: str | None = None
