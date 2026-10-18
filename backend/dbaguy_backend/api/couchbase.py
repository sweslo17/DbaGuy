from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from ..adapters.values import PLAN_TEXT_LIMIT
from ..container import AppContainer
from ..domain.couchbase import ContextOption, FieldSample, Keyspace, PlanView
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


class SampleRequest(_Input):
    profile_id: UUID
    keyspace: Keyspace
    size: int | None = Field(default=None, ge=SAMPLE_SIZE_MIN, le=SAMPLE_SIZE_MAX)  # None: the setting's sample_size


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
