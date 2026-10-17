"""Editor-facing SQL helpers. Every text offset that crosses this API is a UTF-16 code unit (the editor's unit);
the conversion to and from Python code points happens here, with `Utf16Index`, and nowhere else.

These run on every keystroke (completion) or on every selection change, so they log at DEBUG only and never put the
SQL text above it.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from ..container import AppContainer
from ..domain.errors import ValidationFailed
from ..domain.formats import FormatColumn, FormatKind, RowFormatOptions
from ..domain.metadata import PathSegment
from ..services.safety import GuardDecision
from ..sql.completion import CompletionItem, DescribeResult, SnapshotIndex, complete, describe_at
from ..sql.dialects import Dialect
from ..sql.formatting import FormatResult, format_rows
from ..sql.offsets import Utf16Index
from ..sql.select_builder import SortKey, build_browse_select
from ..sql.splitter import split
from .deps import get_container, require_token


def _log_call(request: Request) -> None:
    logger.debug("{} {}", request.method, request.url.path)


router = APIRouter(prefix="/sql", tags=["sql"], dependencies=[Depends(require_token), Depends(_log_call)])


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SplitRequest(_Input):
    text: str
    dialect: Dialect


class StatementRange(BaseModel):
    start: int  # UTF-16
    end: int  # UTF-16, without the terminator
    terminator_end: int  # UTF-16, after the `;` or the custom delimiter (or `end` when there is none)


class SplitResponse(BaseModel):
    statements: list[StatementRange]


class AnalyzeRequest(_Input):
    profile_id: UUID
    statements: list[str] = Field(max_length=10_000)


class _TextAtCursor(_Input):
    database: str | None = None
    text: str
    cursor: int = Field(ge=0)  # UTF-16; beyond the end of the text it means the end


class CompleteRequest(_TextAtCursor):
    profile_id: UUID | None = None  # without one only keywords are suggested, and `dialect` is required
    dialect: Dialect | None = None  # ignored when `profile_id` is given


class DescribeRequest(_TextAtCursor):
    profile_id: UUID


class CompletionResponse(BaseModel):
    start: int  # UTF-16: the items replace text[start:end]
    end: int  # UTF-16
    items: list[CompletionItem]


class FormatRowsRequest(_Input):
    dialect: Dialect
    kind: FormatKind
    columns: list[FormatColumn]
    rows: list[list[Any]] = Field(max_length=100_000)  # the grid's encoded cells
    options: RowFormatOptions = Field(default_factory=RowFormatOptions)


class BrowseSelectRequest(_Input):
    profile_id: UUID
    path: list[PathSegment]
    where: str | None = None
    order_by: list[SortKey] = []
    count: bool = False


class BrowseSelectResponse(BaseModel):
    sql: str


def _profile_dialect(container: AppContainer, profile_id: UUID) -> Dialect:
    return container.registry.get(container.profiles.get(profile_id).engine).dialect


def _completion_dialect(container: AppContainer, body: CompleteRequest) -> Dialect:
    if body.profile_id is not None:
        return _profile_dialect(container, body.profile_id)
    if body.dialect is None:
        raise ValidationFailed("Give a profile_id or a dialect")
    return body.dialect


def _snapshot_index(
    container: AppContainer, profile_id: UUID, database: str | None, wait: bool
) -> SnapshotIndex | None:
    """The completion index of the profile's schema, or None: keywords only (spec §5).

    Without `wait` (a keystroke), it never queues behind a build another request is running: the previous snapshot
    answers meanwhile, or None when there is none yet. None also when the build fails: that is logged once, at
    WARNING, by `SchemaCache`, which then re-raises it for a while without reconnecting; those repeats are only DEBUG
    here, or typing would log a warning per keystroke.
    """
    try:
        return container.schema_cache.index(profile_id, database, wait=wait)
    except Exception as exc:
        logger.debug("No schema snapshot for {} ({}: {}); keywords only", profile_id, type(exc).__name__, exc)
        return None


@router.post("/split", response_model=SplitResponse)
def split_statements(body: SplitRequest) -> SplitResponse:
    index = Utf16Index(body.text)
    return SplitResponse(statements=[
        StatementRange(start=index.to_utf16(s.start), end=index.to_utf16(s.end),
                       terminator_end=index.to_utf16(s.terminator_end))
        for s in split(body.text, body.dialect)
    ])


@router.post("/analyze", response_model=list[GuardDecision])
def analyze(body: AnalyzeRequest, container: AppContainer = Depends(get_container)) -> list[GuardDecision]:
    """What the safety policy would say about each statement if the user had not confirmed it yet."""
    profile = container.profiles.get(body.profile_id)
    dialect = _profile_dialect(container, body.profile_id)
    return [container.policy.check(sql, dialect, profile.safety, profile.read_only) for sql in body.statements]


@router.post("/complete", response_model=CompletionResponse)
def complete_at(body: CompleteRequest, container: AppContainer = Depends(get_container)) -> CompletionResponse:
    dialect = _completion_dialect(container, body)
    snapshot_index = None
    if body.profile_id is not None:
        snapshot_index = _snapshot_index(container, body.profile_id, body.database, wait=False)
    index = Utf16Index(body.text)
    result = complete(body.text, index.from_utf16(body.cursor), dialect, snapshot_index)
    logger.debug("Completion at {}: {} items{}", body.cursor, len(result.items),
                 "" if snapshot_index is not None else " (keywords only)")
    return CompletionResponse(start=index.to_utf16(result.start), end=index.to_utf16(result.end), items=result.items)


@router.post("/describe", response_model=DescribeResult | None)
def describe(body: DescribeRequest, container: AppContainer = Depends(get_container)) -> DescribeResult | None:
    dialect = _profile_dialect(container, body.profile_id)
    # An explicit user action, not a keystroke: worth waiting for a snapshot that is being built.
    snapshot_index = _snapshot_index(container, body.profile_id, body.database, wait=True)
    return describe_at(body.text, Utf16Index(body.text).from_utf16(body.cursor), dialect, snapshot_index)


@router.post("/format-rows", response_model=FormatResult)
def format_rows_for_copy(body: FormatRowsRequest) -> FormatResult:
    return format_rows(body.kind, body.columns, body.rows, body.options, body.dialect, "encoded")


@router.post("/browse-select", response_model=BrowseSelectResponse)
def browse_select(body: BrowseSelectRequest, container: AppContainer = Depends(get_container)) -> BrowseSelectResponse:
    dialect = _profile_dialect(container, body.profile_id)
    return BrowseSelectResponse(sql=build_browse_select(dialect, body.path, body.where, body.order_by, body.count))
