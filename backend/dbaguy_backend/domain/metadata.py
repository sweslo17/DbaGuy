from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .profiles import Engine


class NodeKind(StrEnum):
    DATABASE = "database"
    SCHEMA = "schema"
    FOLDER = "folder"
    TABLE = "table"
    PARTITION = "partition"
    VIEW = "view"
    MATERIALIZED_VIEW = "materialized_view"
    FUNCTION = "function"
    PROCEDURE = "procedure"
    SEQUENCE = "sequence"
    TYPE = "type"
    INDEX = "index"
    TRIGGER = "trigger"
    COLUMN = "column"


class Folder(StrEnum):
    TABLES = "tables"
    VIEWS = "views"
    MATERIALIZED_VIEWS = "materialized_views"
    FUNCTIONS = "functions"
    PROCEDURES = "procedures"
    SEQUENCES = "sequences"
    TYPES = "types"
    COLUMNS = "columns"
    INDEXES = "indexes"
    TRIGGERS = "triggers"
    PARTITIONS = "partitions"


class PathSegment(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    name: str


class ObjectNode(BaseModel):
    kind: str
    name: str
    path: list[PathSegment]
    has_children: bool
    system: bool = False
    detail: str | None = None


class ColumnInfo(BaseModel):
    name: str
    type_name: str
    nullable: bool
    default: str | None = None
    is_primary_key: bool = False
    comment: str | None = None


class ObjectDetail(BaseModel):
    path: list[PathSegment]
    kind: str
    name: str
    ddl: str | None = None
    ddl_is_generated: bool = False
    columns: list[ColumnInfo] | None = None
    comment: str | None = None


class ServerInfo(BaseModel):
    engine: Engine
    version: str
    current_database: str | None = None
    current_user: str | None = None


class MetadataRequest(BaseModel):
    profile_id: UUID
    path: list[PathSegment] = Field(default_factory=list)


def child_path(path: list[PathSegment], kind: str, name: str) -> list[PathSegment]:
    return [*path, PathSegment(kind=kind, name=name)]


def make_node(
    path: list[PathSegment],
    kind: str,
    name: str,
    *,
    has_children: bool,
    system: bool = False,
    detail: str | None = None,
) -> ObjectNode:
    return ObjectNode(
        kind=kind, name=name, path=child_path(path, kind, name),
        has_children=has_children, system=system, detail=detail,
    )
