"""Data tools (spec ⑥ §3.2): the jobs of this backend run. A job itself is a WebSocket command on a session of its
own (dump, run_file); these routes find it again after the window reloads, and cancel it."""

from typing import Self
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..container import AppContainer
from ..domain.data_jobs import (
    DUMP_FILTER_LENGTH, FILTER_ONE_TABLE, DumpContent, DumpObjectKinds, DumpPlan, DumpScope, FileInspection, JobInfo,
    filter_fits,
)
from ..domain.errors import NotFound
from .deps import get_container, require_token

router = APIRouter(prefix="/data", tags=["data"], dependencies=[Depends(require_token)])


class JobCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    request_id: str


class RunFileInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID  # the connection the file would run on
    path: str = Field(min_length=1, max_length=4096)


class DumpPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    database: str | None = None  # the database node's (PostgreSQL, SQL Server); else the profile's
    scope: DumpScope
    content: DumpContent = "both"
    objects: DumpObjectKinds = DumpObjectKinds()
    path: str | None = Field(default=None, min_length=1, max_length=4096)  # where it would go: checks and free space
    # as the dump command has them: a filtered file that drops and recreates its table is warned of (fix wave F2)
    drop_first: bool = False
    filter: str | None = Field(default=None, max_length=DUMP_FILTER_LENGTH)

    @model_validator(mode="after")
    def _a_filter_for_one_table_only(self) -> Self:
        if not filter_fits(self.scope, self.filter):
            raise ValueError(FILTER_ONE_TABLE)
        return self


class RevealRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)


@router.post("/dump/plan", response_model=DumpPlan)
def dump_plan(body: DumpPlanRequest, container: AppContainer = Depends(get_container)) -> DumpPlan:
    """The dump dialog's object list, what the header leaves out, the consistency and the warnings (the metadata
    connection; nothing is written). 422 `not_supported` for an engine without dumps."""
    return container.data.dump_plan(body.profile_id, body.database, body.scope, body.content, body.objects, body.path,
                                    drop_first=body.drop_first, filter=body.filter)


@router.post("/reveal", status_code=204)
def reveal(body: RevealRequest, container: AppContainer = Depends(get_container)) -> Response:
    """Show in Finder, for a file a completed dump of this backend run wrote (404 for any other path)."""
    container.data.reveal(body.path)
    return Response(status_code=204)


@router.post("/run-file/inspect", response_model=FileInspection)
def inspect_run_file(body: RunFileInspectRequest, container: AppContainer = Depends(get_container)) -> FileInspection:
    """Run SQL file's dialog: the file's size, first lines and warnings. A read-only connection is refused (403)."""
    return container.data.inspect(body.profile_id, body.path)


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
