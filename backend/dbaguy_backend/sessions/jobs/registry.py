"""The jobs of this backend run (spec ⑥ D-06-10): which session runs each, its kind and title, its last progress and
its outcome — what GET /data/jobs lists, so a reloaded window finds its jobs again. Kept until cleared or the backend
ends; at most MAX_JOBS_LISTED, the oldest finished first to go. Any thread."""

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel

from ...domain.data_jobs import MAX_JOBS_LISTED, JobInfo, JobKind, JobOutcomeEvent, JobProgressEvent

SESSION_CLOSED_FIRST = "Its session closed before it reported an outcome"


class JobRegistry:
    def __init__(self, limit: int = MAX_JOBS_LISTED, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._limit = limit
        self._clock = clock
        self._jobs: dict[tuple[str, str], JobInfo] = {}  # in the order they started
        self._lock = threading.Lock()

    def begin(self, session_id: str, profile_id: UUID, request_id: str, kind: JobKind, title: str,
              source_profile_id: UUID | None = None) -> None:
        info = JobInfo(session_id=session_id, request_id=request_id, job=kind, title=title, profile_id=profile_id,
                       source_profile_id=source_profile_id, started_at=self._clock())
        with self._lock:
            self._jobs[(session_id, request_id)] = info
            self._trim()

    def report(self, session_id: str, event: BaseModel) -> None:
        """A job_progress or the job_outcome of a job `begin` announced; any other event is ignored."""
        if not isinstance(event, JobProgressEvent | JobOutcomeEvent):
            return
        with self._lock:
            info = self._jobs.get((session_id, event.request_id))
            if info is None:
                return
            if isinstance(event, JobProgressEvent):
                info.progress = event
            else:
                info.outcome = event

    def list(self) -> list[JobInfo]:
        """Newest first."""
        with self._lock:
            return [info.model_copy() for info in reversed(self._jobs.values())]

    def get(self, session_id: str, request_id: str) -> JobInfo | None:
        with self._lock:
            info = self._jobs.get((session_id, request_id))
            return info.model_copy() if info is not None else None

    def dumped(self, path: str) -> bool:
        """Whether a dump of this backend run wrote the file at `path` (Show in Finder reveals only those). A cleared
        job's file is forgotten with it."""
        with self._lock:
            return any(info.job == "dump" and info.outcome is not None and info.outcome.outcome == "completed"
                       and info.outcome.path == path for info in self._jobs.values())

    def clear_finished(self) -> int:
        """Forget every job that has ended; the number forgotten."""
        with self._lock:
            done = [key for key, info in self._jobs.items() if info.outcome is not None]
            for key in done:
                del self._jobs[key]
        return len(done)

    def abandon(self, session_id: str) -> int:
        """`session_id` closed: its jobs with no outcome yet (a worker that never got to report one, say stuck in the
        driver past close()) are listed as unknown from now on, never as running forever (pre-flight L3). An outcome
        the worker reports later still replaces it. The number marked."""
        with self._lock:
            open_jobs = [info for (sid, _), info in self._jobs.items() if sid == session_id and info.outcome is None]
            for info in open_jobs:
                info.outcome = JobOutcomeEvent(request_id=info.request_id, job=info.job, outcome="unknown",
                                               warnings=[SESSION_CLOSED_FIRST])
        return len(open_jobs)

    def _trim(self) -> None:
        """The caller holds the lock. Running jobs are never dropped."""
        finished = [key for key, info in self._jobs.items() if info.outcome is not None]
        for key in finished[:max(0, len(self._jobs) - self._limit)]:
            del self._jobs[key]
