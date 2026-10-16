from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ScriptNode(BaseModel):
    name: str
    path: str  # relative to the scripts folder, "/"-separated
    kind: Literal["file", "folder"]
    children: list["ScriptNode"] = []


class ScriptTree(BaseModel):
    root: str  # the scripts folder, as an absolute path
    items: list[ScriptNode]


class ScriptFile(BaseModel):
    path: str
    content: str
    modified_at: datetime


class ScriptWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content: str


class ScriptRename(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True)

    source: str = Field(alias="from")
    target: str = Field(alias="to")


class ScriptFolder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
