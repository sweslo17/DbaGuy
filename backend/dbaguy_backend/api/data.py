"""Data tools (spec ⑥ §3.2): the jobs of this backend run. A job itself is a WebSocket command on a session of its
own (dump, run_file); these routes find it again after the window reloads, and cancel it."""

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from ..container import AppContainer
from ..domain.data_jobs import JobInfo
from ..domain.errors import NotFound
from .deps import get_container, require_token

router = APIRouter(prefix="/data", tags=["data"], dependencies=[Depends(require_token)])


class JobCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    request_id: str


@router.get("/jobs", response_model=list[JobInfo])
def list_jobs(container: AppContainer = Depends(get_container)) -> list[JobInfo]:
    """Newest first: running and finished, until cleared (D-06-10)."""
    return container.sessions.jobs.list()


@router.post("/jobs/cancel", status_code=204)
def cancel_job(body: JobCancelRequest, container: AppContainer = Depends(get_container)) -> Response:
    """The WebSocket's `cancel`, for a job whose dialog is gone: a job that has ended is left as it is."""
    if container.sessions.jobs.get(body.session_id, body.request_id) is None:
        raise NotFound("No such job")
    container.sessions.worker(body.session_id).cancel(body.request_id)  # SessionNotFound once its session closed
    return Response(status_code=204)


@router.post("/jobs/clear", status_code=204)
def clear_jobs(container: AppContainer = Depends(get_container)) -> Response:
    """Forget the jobs that have ended (the Jobs list's Clear finished)."""
    container.sessions.jobs.clear_finished()
    return Response(status_code=204)
