from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from ..adapters.values import PLAN_TEXT_LIMIT
from ..container import AppContainer
from ..domain.couchbase import ContextOption, PlanView
from ..domain.errors import ValidationFailed
from ..domain.execution import QueryContext
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
