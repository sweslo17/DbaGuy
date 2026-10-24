"""Copy data between connections (spec ⑥ G3, §3.5): what the dialog asks for (`TransferRequest`), what the preview
says (`TransferPlan`, POST /data/transfer/plan), and the one-time plan a copy job takes (`StoredTransfer`): the copy
runs exactly what was previewed (R-05-7, D-06-8), and the command carries only the plan's id
(`services/transfer_plans.py` keeps it)."""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .execution import ColumnMeta
from .metadata import PathSegment

MAX_TRANSFER_TABLES = 500  # spec §3.5.1: one copy holds at most this many tables
MAX_QUERY_CHARS = 1_048_576  # a query result's statement (the editor's, run again)
MAX_QUERY_COLUMNS = 4_096
TRANSFER_FILTER_LENGTH = 65_536  # as a dump's (DUMP_FILTER_LENGTH)
TYPE_TEXT_LENGTH = 1_024  # a typed type as the request takes it; the plan says `invalid_type` past TYPE_CHARS (200)
NAME_LENGTH = 1_024
TransferMode = Literal["create", "append", "replace_rows"]
IdentityChoice = Literal["keep", "generate"]
COPY_DATA = "copy data"  # what a connection without `copy_data` cannot (spec §4)
ONE_TABLE_FILTER = "a filter applies to a copy of one table"
COPY_PLAN_EXPIRED = "The preview has expired: preview the copy again"  # spec §4: plan_expired and plan_mismatch
# A query result whose statement does not only read, or returns no rows: refused by the preview and again by the copy's
# read (the same code and words in both)
QUERY_NOT_SELECT = "query_not_select"
QUERY_NOT_SELECT_MESSAGE = ("Only a statement that returns rows (SELECT, WITH, VALUES…) can be copied, and one that "
                            "only reads")


class QuerySource(BaseModel):
    """A query tab's result (spec §3.5.1): its statement, run again on the tab's own session as an export is (the same
    transaction, its temporary tables), and the columns the tab already has, which the preview maps without running it."""

    model_config = ConfigDict(extra="forbid")

    sql: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    columns: list[ColumnMeta] = Field(min_length=1, max_length=MAX_QUERY_COLUMNS)
    title: str = Field(default="the query", max_length=NAME_LENGTH)  # what messages call it: the tab's name


class SourceSpec(BaseModel):
    """Tables, views and materialized views of one connection, or a query tab's result."""

    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    database: str | None = Field(default=None, max_length=NAME_LENGTH)  # PostgreSQL, SQL Server: the database node's
    tables: list[list[PathSegment]] | None = Field(default=None, min_length=1, max_length=MAX_TRANSFER_TABLES)
    query: QuerySource | None = None
    filter: str | None = Field(default=None, max_length=TRANSFER_FILTER_LENGTH)  # one table: the table-data tab's

    @model_validator(mode="after")
    def _tables_or_a_query(self) -> Self:
        if (self.tables is None) == (self.query is None):
            raise ValueError("a copy's source is tables or a query, one of them")
        if self.filter is not None and (self.tables is None or len(self.tables) != 1):
            raise ValueError(ONE_TABLE_FILTER)
        return self


class TargetSpec(BaseModel):
    """Where the rows go (spec §3.5.1). `overrides`, `identity` and `left_out` are by the source table's name (a
    query's: its `title`) and then the source column's."""

    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    database: str | None = Field(default=None, max_length=NAME_LENGTH)
    schema_name: str | None = Field(default=None, max_length=NAME_LENGTH)  # SQLite: main; None: the session's own
    table: str | None = Field(default=None, min_length=1, max_length=NAME_LENGTH)  # one source only: its new name
    mode: TransferMode
    overrides: dict[str, dict[str, str]] = Field(default_factory=dict)  # a new table's column types (create)
    identity: dict[str, dict[str, IdentityChoice]] = Field(default_factory=dict)  # append: keep or let it generate
    left_out: dict[str, list[str]] = Field(default_factory=dict)  # source columns not copied

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        for table in (self.overrides, self.identity, self.left_out):
            if len(table) > MAX_TRANSFER_TABLES:
                raise ValueError(f"at most {MAX_TRANSFER_TABLES} tables")
        if any(len(text) > TYPE_TEXT_LENGTH for columns in self.overrides.values() for text in columns.values()):
            raise ValueError(f"a type is at most {TYPE_TEXT_LENGTH} characters")
        return self


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: SourceSpec
    target: TargetSpec

    @model_validator(mode="after")
    def _a_name_for_one_table(self) -> Self:
        if self.target.table is not None and (self.source.tables is not None and len(self.source.tables) != 1):
            raise ValueError("a new name is for a copy of one table")
        return self


ProblemCode = Literal[
    "target_exists", "target_missing", "target_not_writable", "unmappable_type", "invalid_type", "same_table",
    "same_sqlite_file", "source_missing", "missing_column", "unconvertible", "query_not_select", "no_columns",
    "duplicate_column", "invalid_filter", "duplicate_target", "cascade_loop",
]


class TransferProblem(BaseModel):
    """Why the copy cannot run as asked (spec §4): the preview has no plan id while there is one."""

    code: ProblemCode
    message: str
    column: str | None = None  # the source column it is about


class ColumnMapping(BaseModel):
    """One source column and where it goes (spec §3.5.1)."""

    source_name: str
    source_type: str
    family: str  # TypeFamily
    target_name: str | None = None  # None: not copied (`skipped` says why)
    target_type: str | None = None
    type_editable: bool = False  # create: the new column's type may be changed
    overridden: bool = False  # the type is the one asked for
    conversion: str | None = None  # how its values change on the way (⚠), or what the preview should say of it
    skipped: str | None = None  # why it is not copied
    identity: IdentityChoice | None = None  # append into an identity column: keep the values, or let it generate them
    problem: str | None = None


class TableTransfer(BaseModel):
    """One table of the copy, in the order the copy runs (spec §3.5.1)."""

    source: str  # as messages name it: schema.name, or the query's title
    target: str  # schema.name on the target
    source_key: str  # what `overrides`, `identity` and `left_out` key it by
    source_path: list[PathSegment] | None = None  # the request's tree path of it (None: a query)
    mode: TransferMode
    columns: list[ColumnMapping]
    before: list[str] = []  # in its transaction (MySQL's CREATE TABLE before it): CREATE TABLE, DELETE, IDENTITY_INSERT
    after: list[str] = []  # before its COMMIT: PostgreSQL's setval, SQL Server's IDENTITY_INSERT OFF
    warnings: list[str] = []
    problems: list[TransferProblem] = []


class TransferPlan(BaseModel):
    """The preview (POST /data/transfer/plan). `plan_id` is null while any table or the copy has a problem (⑤ E8)."""

    plan_id: str | None = None
    source_profile_id: UUID
    tables: list[TableTransfer]
    warnings: list[str] = []
    problems: list[TransferProblem] = []
