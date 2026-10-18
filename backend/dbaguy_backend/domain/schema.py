from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from .metadata import PathSegment

# synonym: SQL Server, no columns; collection: Couchbase, found by ⌘P (not a relation of the snapshot's schemas)
RelationKind = Literal["table", "view", "materialized_view", "synonym", "collection"]


class SnapshotColumn(BaseModel):
    name: str
    type_name: str


class SnapshotForeignKey(BaseModel):
    name: str
    columns: list[str]
    ref_schema: str
    ref_table: str
    ref_columns: list[str]


class SnapshotRelation(BaseModel):
    name: str
    kind: RelationKind
    columns: list[SnapshotColumn]
    primary_key: list[str] = []
    foreign_keys: list[SnapshotForeignKey] = []


class SnapshotSchema(BaseModel):
    name: str
    system: bool = False
    relations: list[SnapshotRelation] = []
    functions: list[str] = []


class SnapshotKeyspace(BaseModel):
    """A Couchbase collection; the bucket-level (default) keyspace is `_default._default` of its bucket."""

    bucket: str
    scope: str
    collection: str
    system: bool = False


class SchemaSnapshot(BaseModel):
    database: str | None
    default_schema: str | None
    search_path: list[str]
    schemas: list[SnapshotSchema]
    built_at: datetime
    keyspaces: list[SnapshotKeyspace] = []  # Couchbase keyspaces; relational engines leave it empty


class ObjectSearchHit(BaseModel):
    database: str | None
    schema_name: str
    name: str
    kind: RelationKind
    path: list[PathSegment]
