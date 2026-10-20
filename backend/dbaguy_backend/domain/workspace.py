from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .couchbase import CAS_PATTERN
from .edits import StagedEditsFile
from .execution import PARAM_NAME, QueryContext
from .metadata import PathSegment

# A parameter panel's draft is keyed by the name as the statement writes it (`$name`, `$1`), the text as typed: valid
# JSON or not yet (B14). Bounded like the panel itself (StatementParams: 200 named + 200 positional, 1 MiB a literal).
ParamName = Annotated[str, Field(pattern=rf"^\$(?:{PARAM_NAME}|[1-9][0-9]{{0,2}})$")]
ParamDraft = Annotated[str, Field(max_length=1_048_576)]
MAX_PARAM_DRAFTS = 400
# The CAS a document tab's unsaved text is based on: decimal text, like the API's (a CAS is beyond 2^53)
DocumentCas = Annotated[str, Field(pattern=CAS_PATTERN)]
# None follows the profile's default context, "none" is the user's explicit No context (B14)
TabQueryContext = QueryContext | Literal["none"]


class TextSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor: int = Field(ge=0)  # UTF-16 code units, like every text offset in the API
    head: int = Field(ge=0)


class WorkspaceTab(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    kind: Literal["query", "table", "object", "document"]
    title: str = ""
    profile_id: UUID | None = None
    database: str | None = None
    script_path: str | None = None  # relative to the scripts folder
    object_path: list[PathSegment] | None = None  # a document tab's: bucket, scope, collection, then `document` (its ID)
    buffer: str | None = None  # the editor text, unsaved changes included; a document tab keeps it only when unsaved
    cursor: int | None = Field(default=None, ge=0)  # UTF-16 code units
    selection: TextSelection | None = None
    autocommit: bool | None = None
    query_context: TabQueryContext | None = None  # Couchbase
    params: dict[ParamName, ParamDraft] | None = Field(default=None, max_length=MAX_PARAM_DRAFTS)
    # Couchbase document tab: the CAS its unsaved `buffer` is based on (decimal text); None for a new document
    document_cas: DocumentCas | None = None
    # A table tab's staged grid edits (spec ⑤ §3.7); None: nothing staged, or too large to keep
    staged_edits: StagedEditsFile | None = None

    @model_validator(mode="after")
    def _staged_edits_on_a_table_tab(self) -> Self:
        if self.staged_edits is not None and self.kind != "table":
            raise ValueError("only a table tab keeps staged edits")
        return self


class WorkspaceState(BaseModel):
    """The open tabs (`workspace.json`), written whole every few seconds so that a crash loses nothing."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    tabs: list[WorkspaceTab] = []
    active_tab_id: str | None = None
    sidebar_tab: str | None = None
