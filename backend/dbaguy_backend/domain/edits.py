"""Grid editing (spec ⑤ §3): how a table's rows are identified and written (EditTarget), the changes a tab stages
(EditChanges), what a preview shows (EditPreview) and the one-time plan a session applies (EditPlan)."""

from collections.abc import Sequence
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from pydantic_core import PydanticCustomError

from .column_category import ColumnCategory
from .metadata import PathSegment

EditKind = Literal["number", "text", "bool", "datetime", "binary", "json", "other"]
Compare = Literal["equals", "as_text", "none"]

MAX_CHANGES = 10_000  # D-05-11: changes (rows) in one commit
MAX_VALUE_CHARS = 1_048_576  # D-05-11: one value; beyond it the preview reports `value_too_large`
VALUE_ACCEPTED_CHARS = 4 * MAX_VALUE_CHARS  # beyond this the request itself is invalid (422), ruling E8
MAX_COMMIT_CHARS = 64 * 1_048_576  # L15: every value, key and original cell of one commit together (422 beyond it)
MAX_PLAN_CHARS = 128 * 1_048_576  # L15: the statements and scripts of all stored plans together (EditPlanStore)
COMMIT_TOO_LARGE = f"The changes are too large: at most {MAX_COMMIT_CHARS // 1_048_576} MiB of values per commit"
MAX_STAGED_CHARS = 8 * 1_048_576  # D-05-11: the staged text one tab keeps in workspace.json (spec ⑤ §3.7)
PLAN_TTL_S = 15 * 60  # D-05-7
MAX_PLANS = 32  # D-05-7
PLAN_EXPIRED = "plan_expired"
PLAN_MISMATCH = "plan_mismatch"
PLAN_TOO_LARGE = (
    f"The statements are too large to keep for the commit: at most {MAX_PLAN_CHARS // 1_048_576} MiB of text. "
    "Commit fewer changes at once"
)
PLAN_EXPIRED_MESSAGE = "The preview has expired: preview the changes again"
PLAN_MISMATCH_MESSAGE = PLAN_EXPIRED_MESSAGE  # spec §4: plan_expired and plan_mismatch share one text (pre-flight L2)

_KIND_OF: dict[ColumnCategory, EditKind] = {
    ColumnCategory.NUMBER: "number", ColumnCategory.TEXT: "text", ColumnCategory.BOOL: "bool",
    ColumnCategory.DATETIME: "datetime", ColumnCategory.BINARY: "binary", ColumnCategory.JSON: "json",
}
_CATEGORY_OF: dict[EditKind, ColumnCategory] = {kind: category for category, kind in _KIND_OF.items()}


def edit_kind(category: ColumnCategory) -> EditKind:
    """How a column's new values are read, from the grid category each engine already gives its types (DRY)."""
    return _KIND_OF.get(category, "other")


def kind_category(kind: EditKind) -> ColumnCategory:
    return _CATEGORY_OF.get(kind, ColumnCategory.OTHER)


class EditColumn(BaseModel):
    name: str
    type_name: str  # as declared: numeric(10,2), varchar(20), datetime2(7)
    kind: EditKind
    nullable: bool
    has_default: bool  # a default, identity, auto_increment or generated value: an INSERT may leave it out
    writable: bool
    read_only_reason: str | None = None  # "generated column", "IDENTITY column", "rowversion column", …
    compare: Compare  # how an original value is checked; none: not checked (listed per statement)
    unicode: bool = True  # SQL Server: False for char / varchar / text (ASCII text then goes without N)


class EditTarget(BaseModel):
    path: list[PathSegment]
    editable: bool
    reason: str | None = None  # why not, for the toolbar
    key: list[str] = Field(default_factory=list)  # in index order
    key_source: Literal["primary_key", "unique"] | None = None
    columns: list[EditColumn] = Field(default_factory=list)
    transactional: bool = True  # MySQL: the table's engine supports transactions
    backslash_escapes: bool = True  # MySQL: the session's sql_mode has no NO_BACKSLASH_ESCAPES


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


ChangeId = Annotated[str, Field(min_length=1, max_length=100)]  # made by the client ("u:…", "i:…", "d:…"), echoed
# A column a change names: valid text, so a lone surrogate in a name is a 422 (its `loc` shows U+FFFD) and never
# reaches a statement or an error body that cannot be encoded
ColumnName = Annotated[str, Field(min_length=1, max_length=1024)]


class CellInput(_Input):
    kind: Literal["value", "null", "default"]
    text: str | None = Field(default=None, max_length=VALUE_ACCEPTED_CHARS)  # with kind "value" only

    @model_validator(mode="after")
    def _text_with_a_value_only(self) -> Self:
        if (self.kind == "value") != (self.text is not None):
            raise ValueError("a value needs its text; null and default take none")
        return self


class RowUpdate(_Input):
    id: ChangeId
    key: dict[ColumnName, JsonValue]  # the key cells as the grid loaded them (encode_value's output)
    set: dict[ColumnName, CellInput] = Field(min_length=1)
    original: dict[ColumnName, JsonValue]  # each set column's cell as loaded

    @model_validator(mode="after")
    def _an_original_per_column(self) -> Self:
        if self.set.keys() - self.original.keys():
            raise ValueError("every column set needs its original value")
        return self


class RowInsert(_Input):
    id: ChangeId
    values: dict[ColumnName, CellInput] = Field(default_factory=dict)  # a column left out takes its default


class RowDelete(_Input):
    id: ChangeId
    key: dict[ColumnName, JsonValue]


def _json_chars(cell: JsonValue) -> int:
    """The text a key or original cell holds: the length of each string (an object's keys too), 1 for any other scalar.
    Iterative, so however deep a cell nests it is measured, never a RecursionError."""
    total = 0
    pending = [cell]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            total += len(item)
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            total += sum(len(name) for name in item)
            pending.extend(item.values())
        else:
            total += 1
    return total


def _text_chars(updates: Sequence[RowUpdate], inserts: Sequence[RowInsert], deletes: Sequence[RowDelete]) -> int:
    """The text a set of changes carries: every value's, key's and original's (`_json_chars`)."""
    total = sum(len(cell.text or "") for change in updates for cell in change.set.values())
    total += sum(len(cell.text or "") for change in inserts for cell in change.values.values())
    total += sum(_json_chars(change.key) for change in (*updates, *deletes))
    total += sum(_json_chars(change.original) for change in updates)
    return total


def _counted_ids(changes: Sequence[RowUpdate | RowInsert | RowDelete]) -> list[str]:
    """The changes' ids, refused beyond MAX_CHANGES or when one repeats."""
    ids = [change.id for change in changes]
    if len(ids) > MAX_CHANGES:
        raise ValueError(f"at most {MAX_CHANGES:,} changes are committed at once")
    if len(set(ids)) != len(ids):
        raise ValueError("change ids must be unique")
    return ids


class EditChanges(_Input):
    updates: list[RowUpdate] = Field(default_factory=list)
    inserts: list[RowInsert] = Field(default_factory=list)
    deletes: list[RowDelete] = Field(default_factory=list)

    @model_validator(mode="after")
    def _counted_and_unique(self) -> Self:
        if not _counted_ids((*self.deletes, *self.updates, *self.inserts)):
            raise ValueError("nothing to commit")
        return self

    @model_validator(mode="after")
    def _bounded_in_size(self) -> Self:
        """L15: one value may be 4 MiB of text before it is invalid and 10,000 changes may carry one each, so the commit
        as a whole is bounded too: what is previewed is held in memory twice (statements and script) until applied."""
        if _text_chars(self.updates, self.inserts, self.deletes) > MAX_COMMIT_CHARS:
            # a custom error, so `detail[0].msg` is the text itself (a ValueError's gets "Value error, " in front)
            raise PydanticCustomError("commit_too_large", COMMIT_TOO_LARGE)
        return self


class StagedCell(CellInput):
    """A staged cell as `workspace.json` keeps it: one value of at most MAX_VALUE_CHARS (D-05-11), not a request's 4x."""

    text: str | None = Field(default=None, max_length=MAX_VALUE_CHARS)


class StagedRowUpdate(RowUpdate):
    set: dict[ColumnName, StagedCell] = Field(min_length=1)


class StagedRowInsert(RowInsert):
    values: dict[ColumnName, StagedCell] = Field(default_factory=dict)


class StagedEditsFile(_Input):
    """A table-data tab's staged grid edits in `workspace.json` (spec ⑤ §3.7, ruling H6): the changes as staged (an
    UPDATE under its row's DELETE included), the key they were staged on, and whether the last commit's outcome is
    unknown. Bounded like a commit (D-05-11) and by MAX_STAGED_CHARS in all: the frontend writes none that is larger
    (`savedEdits`), and the workspace repair drops one this model refuses rather than the tab (B40)."""

    version: Literal[1] = 1
    key_columns: list[ColumnName] = Field(min_length=1)
    updates: list[StagedRowUpdate] = Field(default_factory=list)
    inserts: list[StagedRowInsert] = Field(default_factory=list)
    deletes: list[RowDelete] = Field(default_factory=list)
    outcome_unknown: bool = False

    @model_validator(mode="after")
    def _counted_and_bounded(self) -> Self:
        _counted_ids((*self.deletes, *self.updates, *self.inserts))
        if _text_chars(self.updates, self.inserts, self.deletes) > MAX_STAGED_CHARS:
            raise ValueError(f"at most {MAX_STAGED_CHARS // 1_048_576} MiB of staged text is kept")
        return self


EditProblemCode = Literal[
    "invalid_number", "invalid_json", "invalid_hex", "invalid_bool", "invalid_text", "value_too_large",
    "read_only_column", "unknown_column", "not_nullable", "key_incomplete", "default_not_allowed",
    "key_collision",  # an UPDATE moves a row onto another changed row's key (ruling G7)
]


class EditProblem(BaseModel):
    change_id: str
    column: str | None = None
    code: EditProblemCode
    message: str  # fixed text: never the value


class UncheckedColumn(BaseModel):
    column: str
    reason: str


class PlannedStatement(BaseModel):
    change_id: str
    kind: Literal["update", "insert", "delete"]
    sql: str  # exactly what the worker sends
    unchecked: list[UncheckedColumn] = Field(default_factory=list)  # set columns whose original is not checked
    row: str | None = None  # the key as the user's messages show it (`id = 42`); never logged


EditWarningCode = Literal["unchecked_columns", "non_transactional", "missing_required"]


class EditWarning(BaseModel):
    code: EditWarningCode
    message: str
    change_id: str | None = None
    columns: list[str] = Field(default_factory=list)


class EditCounts(BaseModel):
    updates: int = 0
    inserts: int = 0
    deletes: int = 0


class EditPreview(BaseModel):
    plan_id: str | None  # None: problems; nothing can be applied
    begin: str
    statements: list[PlannedStatement]  # empty when there are problems (ruling E8)
    commit: str
    problems: list[EditProblem]
    warnings: list[EditWarning]
    counts: EditCounts
    script: str  # begin, statements and commit as one text (Copy, History); "" with problems


class EditPlan(BaseModel):
    """What a session applies: the previewed texts, nothing else (R-05-7). Taken once, by a session of the same profile
    and, when the path names one, the same database (CR-2, ruling E3)."""

    model_config = ConfigDict(frozen=True)

    plan_id: str
    profile_id: UUID
    database: str | None  # the path's database segment; None: any session of the profile
    table: str  # as the statements write it (logs)
    begin: str
    statements: tuple[PlannedStatement, ...]
    commit: str
    transactional: bool
    script: str
    # How the literals read a backslash (the target's, from the metadata connection, D-05-13): the session that
    # applies must read it alike (05a review M2)
    backslash_escapes: bool = True


class PlanUnavailable(Exception):
    """A plan the session cannot take: expired (or never made, or taken already) or made for another connection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
