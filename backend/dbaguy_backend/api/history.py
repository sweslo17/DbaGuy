from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from ..container import AppContainer
from ..domain.history import HistoryPage, HistorySource
from .deps import get_container, require_token

router = APIRouter(prefix="/history", tags=["history"], dependencies=[Depends(require_token)])


@router.get("", response_model=HistoryPage)
def search_history(
    q: str = "",
    profile_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    before: int | None = None,
    source: HistorySource | None = None,  # app: the user's; mcp: AI tools' (spec ⑧ §3.7)
    container: AppContainer = Depends(get_container),
) -> HistoryPage:
    return container.couchbase.with_query_contexts(container.history.search(q, profile_id, limit, before, source))


@router.delete("", status_code=204)
def clear_history(container: AppContainer = Depends(get_container)) -> Response:
    container.history.clear()
    return Response(status_code=204)
