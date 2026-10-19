from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from ..adapters.values import PLAN_TEXT_LIMIT
from ..container import AppContainer
from ..domain.couchbase import (
    CAS_PATTERN, DOCUMENT_ID_MAX_BYTES, DOCUMENT_TEXT_LIMIT, ContextOption, DocumentSaved, DocumentView, FieldSample,
    Keyspace, PlanView, SaveAnswer, SavePreview,
)
from ..domain.errors import ValidationFailed
from ..domain.execution import QueryContext
from ..domain.settings import SAMPLE_SIZE_MAX, SAMPLE_SIZE_MIN
from .deps import get_container, require_token

router = APIRouter(prefix="/couchbase", tags=["couchbase"], dependencies=[Depends(require_token)])

_STATEMENT_LIMIT = 1_048_576


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanRequest(_Input):
    text: str = Field(max_length=PLAN_TEXT_LIMIT)  # an EXPLAIN / ADVISE / profile answer, as the grid shows it


class AdviseRequest(_Input):
    profile_id: UUID
    statement: str = Field(min_length=1, max_length=_STATEMENT_LIMIT)
    query_context: QueryContext | None = None  # the tab's context, as every request of the tab carries it


class StatementKeyspaceRequest(_Input):
    sql: str = Field(min_length=1, max_length=_STATEMENT_LIMIT)  # the statement a result came from
    query_context: QueryContext | None = None  # the context the run used


class SampleRequest(_Input):
    profile_id: UUID
    keyspace: Keyspace
    size: int | None = Field(default=None, ge=SAMPLE_SIZE_MIN, le=SAMPLE_SIZE_MAX)  # None: the setting's sample_size


_Cas = Annotated[str, Field(pattern=CAS_PATTERN)]  # decimal text: a CAS is beyond 2^53 (the store checks its range)
# Which message a long text gets, not a memory bound (the body is read before this is checked): up to here the service
# refuses a text beyond DOCUMENT_TEXT_LIMIT with its own message ("Documents are limited to 20 MiB"), which covers a
# large compact document after Format adds its indentation; beyond this, the request is invalid.
_TEXT_ACCEPTED = 4 * DOCUMENT_TEXT_LIMIT
_DocumentText = Annotated[str, Field(max_length=_TEXT_ACCEPTED)]


class DocumentRequest(_Input):
    profile_id: UUID
    keyspace: Keyspace
    id: str = Field(min_length=1, max_length=DOCUMENT_ID_MAX_BYTES)  # characters here; the service checks the bytes


class SaveDocumentRequest(DocumentRequest):
    cas: _Cas  # the CAS the document had when it was opened or last saved
    text: _DocumentText
    confirmed: bool = False  # the user confirmed the preview of what the server would store


class InsertDocumentRequest(DocumentRequest):
    text: _DocumentText
    confirmed: bool = False


class RemoveDocumentRequest(DocumentRequest):
    cas: _Cas


@router.get("/contexts", response_model=list[ContextOption])
def contexts(
    profile_id: UUID,
    bucket: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    scope: Annotated[str | None, Query(min_length=1, max_length=251)] = None,
    container: AppContainer = Depends(get_container),
) -> list[ContextOption]:
    """The query contexts a tab can pick. `bucket` and `scope` name the tab's current one, listed first when the
    schema snapshot does not have it."""
    if scope is not None and bucket is None:
        raise ValidationFailed("A scope needs its bucket")
    current = QueryContext(bucket=bucket, scope=scope) if bucket is not None else None
    return container.couchbase.contexts(profile_id, current)


@router.post("/plan", response_model=PlanView)
def plan(body: PlanRequest, container: AppContainer = Depends(get_container)) -> PlanView:
    return container.couchbase.plan(body.text)


@router.post("/advise", response_model=PlanView)
def advise(body: AdviseRequest, container: AppContainer = Depends(get_container)) -> PlanView:
    return container.couchbase.advise(body.profile_id, body.statement, body.query_context)


@router.post("/statement-keyspace", response_model=Keyspace | None)
def statement_keyspace(
    body: StatementKeyspaceRequest, container: AppContainer = Depends(get_container)
) -> Keyspace | None:
    """The collection the statement reads when it names exactly one (fully qualified, or resolved against the run's
    context as SQL++ resolves it), else null. Pure: no connection, nothing logged."""
    return container.couchbase.statement_keyspace(body.sql, body.query_context)


@router.post("/fields/sample", response_model=FieldSample)
def sample_fields(body: SampleRequest, container: AppContainer = Depends(get_container)) -> FieldSample:
    """Sample a keyspace's documents (R-04-11): a manual action, whose result is kept for `GET /fields`."""
    container.couchbase.require(body.profile_id)
    return container.field_samples.sample(body.profile_id, body.keyspace, body.size)


@router.get("/fields", response_model=FieldSample | None)
def cached_fields(
    profile_id: UUID,
    bucket: Annotated[str, Query(min_length=1, max_length=100)],
    scope: Annotated[str, Query(min_length=1, max_length=251)],
    collection: Annotated[str, Query(min_length=1, max_length=251)],
    container: AppContainer = Depends(get_container),
) -> FieldSample | None:
    """The keyspace's last sample, or null when it has none (nothing samples by itself)."""
    container.couchbase.require(profile_id)
    return container.field_samples.cached(profile_id, Keyspace(bucket=bucket, scope=scope, collection=collection))


@router.post("/documents/get", response_model=DocumentView)
def get_document(body: DocumentRequest, container: AppContainer = Depends(get_container)) -> DocumentView:
    """The document as stored: its exact text and META (spec §3.9.4). 404 `document_missing` when there is none."""
    return container.documents.get(body.profile_id, body.keyspace, body.id)


@router.post("/documents/save", response_model=SaveAnswer)
def save_document(
    body: SaveDocumentRequest, container: AppContainer = Depends(get_container)
) -> DocumentSaved | SavePreview:
    """Write the text over the document if it still has `cas`, or answer what the server would store instead
    (`needs_confirmation`) until `confirmed`."""
    return container.documents.save(body.profile_id, body.keyspace, body.id, body.cas, body.text, body.confirmed)


@router.post("/documents/insert", response_model=SaveAnswer)
def insert_document(
    body: InsertDocumentRequest, container: AppContainer = Depends(get_container)
) -> DocumentSaved | SavePreview:
    """Create the document, previewed like a save; 409 `document_exists` when the ID is taken."""
    return container.documents.insert(body.profile_id, body.keyspace, body.id, body.text, body.confirmed)


@router.post("/documents/remove", status_code=204)
def remove_document(body: RemoveDocumentRequest, container: AppContainer = Depends(get_container)) -> Response:
    """Delete the document if it still has `cas`."""
    container.documents.remove(body.profile_id, body.keyspace, body.id, body.cas)
    return Response(status_code=204)
