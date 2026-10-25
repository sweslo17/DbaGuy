import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from uuid import UUID, uuid4

from loguru import logger
from pydantic import BaseModel

from ..domain.edits import PLAN_MISMATCH, PlanUnavailable
from ..domain.errors import SessionNotFound
from ..domain.metadata import ServerInfo
from ..domain.transfer import COPY_PLAN_EXPIRED
from ..services.connection_service import ConnectionService
from ..services.profile_service import ProfileService
from .hooks import SessionHooksFactory
from .jobs.context import SourceLease
from .jobs.registry import JobRegistry
from .worker import Emitter, SessionWorker


class SessionCreateRequest(BaseModel):
    profile_id: UUID
    database: str | None = None


class SessionInfo(BaseModel):
    session_id: str
    profile_id: UUID
    database: str | None
    server: ServerInfo


@dataclass
class _Entry:
    worker: SessionWorker
    info: SessionInfo
    attached: int = 0
    detached_at: float = 0.0
    agent: bool = False  # an MCP agent's session (spec ⑧ D-08-1)


class SessionManager:
    """User sessions (one per editor tab, one per data-tools job) and MCP agents' sessions (spec ⑧ D-08-1). Closed
    explicitly, or once idle without a WebSocket: nothing running or queued (D-06-10), and no attach and no request
    ending for `idle_timeout`."""

    def __init__(
        self, connections: ConnectionService, profiles: ProfileService, hooks: SessionHooksFactory | None = None,
        idle_timeout: float = 600.0, jobs: JobRegistry | None = None, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connections = connections
        self._profiles = profiles
        self._hooks = hooks  # None: workers keep their defaults (everything runs, nothing is recorded)
        self._idle_timeout = idle_timeout
        self._clock = clock  # idle time, here and in each worker (its `finished_at`)
        self.jobs = jobs if jobs is not None else JobRegistry()
        self._sessions: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def create(self, request: SessionCreateRequest) -> SessionInfo:
        return self._open(request.profile_id, request.database, agent=False)

    def create_agent(self, profile_id: UUID, database: str | None) -> SessionInfo:
        """An MCP agent's session (D-08-1): on a read-only copy of the profile (`ConnectionService.open(agent=True)`),
        with the agent's hooks; the reaper closes it like any other. A password it does not have is PasswordRequired:
        it never asks (R-08-9)."""
        return self._open(profile_id, database, agent=True)

    def _open(self, profile_id: UUID, database: str | None, *, agent: bool) -> SessionInfo:
        profile = self._profiles.get(profile_id)
        secrets = self._profiles.resolve_secrets(profile)
        session_id = uuid4().hex
        worker = SessionWorker(
            f"{'mcp:' if agent else ''}{profile.name}:{session_id[:8]}",
            lambda: self._connections.open(profile, database, secrets, agent=agent),
            clock=self._clock,
        )
        try:
            worker.start()
            server = worker.call(lambda c: c.server_info())
            if self._hooks is not None and agent:
                worker.configure(self._hooks.build_agent(profile.id))  # before it is registered
            elif self._hooks is not None:
                hooks = self._hooks.build(profile.id, server.current_database)
                jobs = replace(hooks.jobs, announce=partial(self.jobs.begin, session_id, profile.id),
                               report=partial(self.jobs.report, session_id),
                               source=partial(self._source, from_session=session_id))
                worker.configure(replace(hooks, jobs=jobs))  # before it is registered
        except BaseException:
            worker.close()  # whatever failed, never leave the thread, connection or tunnel behind
            raise
        info = SessionInfo(session_id=session_id, profile_id=profile.id, database=server.current_database, server=server)
        with self._lock:
            self._sessions[session_id] = _Entry(worker=worker, info=info, detached_at=self._clock(), agent=agent)
        logger.info("Opened {}session {} for '{}'", "an agent's " if agent else "", session_id, profile.name)
        return info

    def hold(self, session_id: str) -> SessionWorker:
        """Count `session_id` as attached until `release`: the reaper never closes it in between (a copy's source
        session between two tables, an agent's session between the statements of one call). Found and counted under
        one lock: a session the reaper took meanwhile is not found, never handed out while it closes."""
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is not None:
                entry.attached += 1
        if entry is None:
            raise SessionNotFound(f"Session {session_id} not found")
        return entry.worker

    def release(self, session_id: str) -> None:
        self._detached(session_id)

    def _source(self, profile_id: UUID, database: str | None, session_id: str | None, *,
                from_session: str) -> SourceLease:
        """A copy's source session (06c), for the copy running on `from_session`: the query tab's session the copy
        names, which must be one of the plan's source profile and never the copy's own (its read would wait behind the
        copy on the same thread, Task 3 review M-7) (`plan_mismatch`, the copy's §4 text: pre-flight L-a), and stays the
        tab's; or a session of the copy's own, closed when the copy releases it. Opening one may fail as a session does
        (a password, a server down): the copy then never started. Either counts as attached while leased (a query tab's
        too, whose socket may be gone, M-6): between two tables (a long DELETE, a COMMIT) it is idle, and the reaper
        must not close it under the copy."""
        if session_id is not None:
            entry = self._entry(session_id)
            if entry.info.profile_id != profile_id or session_id == from_session:
                raise PlanUnavailable(PLAN_MISMATCH, COPY_PLAN_EXPIRED)
            # a query tab's session the copy read from is the tab's again on release: one attach fewer
            return SourceLease(self.hold(session_id), partial(self.release, session_id))
        info = self.create(SessionCreateRequest(profile_id=profile_id, database=database))
        return SourceLease(self.hold(info.session_id), partial(self._end_copy_source, info.session_id))

    def _end_copy_source(self, session_id: str) -> None:
        """A copy's own source session ends with it (never reused, as a job's own session)."""
        self._pop_and_close(session_id)

    def _detached(self, session_id: str) -> _Entry | None:
        """One attach fewer (a socket, or a copy's lease); idle time counts from the last one. None: it is gone."""
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return None
            entry.attached = max(0, entry.attached - 1)
            if entry.attached == 0:
                entry.detached_at = self._clock()
        return entry

    def _entry(self, session_id: str) -> _Entry:
        with self._lock:
            entry = self._sessions.get(session_id)
        if entry is None:
            raise SessionNotFound(f"Session {session_id} not found")
        return entry

    def worker(self, session_id: str) -> SessionWorker:
        return self._entry(session_id).worker

    def attach(self, session_id: str, emit: Emitter) -> SessionWorker:
        entry = self._entry(session_id)
        with self._lock:
            entry.attached += 1
        # A reloaded window gets the last progress or the outcome of the session's job first (D-06-10, pre-flight M2)
        entry.worker.attach_emitter(emit)
        # A client attaching mid-transaction learns the state before any `finished`; the frontend's job runner also
        # sends a job's command only on this first state (06b Task 5 L2), so every attach must replay one
        # (tests/test_api.py pins it)
        entry.worker.replay_state()
        return entry.worker

    def detach(self, session_id: str, emit: Emitter) -> None:
        if (entry := self._detached(session_id)) is not None:
            entry.worker.clear_emitter(emit)

    def close(self, session_id: str) -> None:
        if not self._pop_and_close(session_id):
            raise SessionNotFound(f"Session {session_id} not found")

    def close_for_profile(self, profile_id: UUID) -> None:
        with self._lock:
            ids = [sid for sid, e in self._sessions.items() if e.info.profile_id == profile_id]
        for sid in ids:
            self._pop_and_close(sid)

    def close_agents(self, profile_id: UUID | None = None) -> None:
        """Close MCP agents' sessions: a profile's (it changed), or all (the MCP server stopped)."""
        with self._lock:
            ids = [sid for sid, e in self._sessions.items()
                   if e.agent and (profile_id is None or e.info.profile_id == profile_id)]
        for sid in ids:
            self._pop_and_close(sid)

    def reap_idle(self) -> int:
        closed = 0
        for sid in self._idle_ids():
            if self._pop_and_close(sid, idle_only=True):
                logger.info("Closed idle session {}", sid)
                closed += 1
        return closed

    def close_all(self) -> None:
        with self._lock:
            ids = list(self._sessions)
        for sid in ids:
            self._pop_and_close(sid)

    def check_idle_transactions(self, warn_after_s: float, rollback_after_s: float) -> None:
        """Warn about idle transactions and roll them back in time."""
        self._check_each("Idle-transaction check", lambda w: w.check_idle_transaction(warn_after_s, rollback_after_s))

    def check_paused_results(self) -> None:
        """Close paused results that waited too long for Load more (engines that set a limit)."""
        self._check_each("Paused-result check", SessionWorker.check_paused_result)

    def _check_each(self, what: str, check: Callable[[SessionWorker], None]) -> None:
        """Run a housekeeping check on every session's worker; one failing session does not stop the rest."""
        with self._lock:
            entries = list(self._sessions.items())
        for session_id, entry in entries:
            try:
                check(entry.worker)
            except Exception:
                logger.exception("{} failed for session {}", what, session_id)

    def _is_quiet(self, entry: _Entry) -> bool:
        """No WebSocket, and no attach and no request ending for `idle_timeout`. The caller holds the lock."""
        return not entry.attached and (
            self._clock() - max(entry.detached_at, entry.worker.finished_at) >= self._idle_timeout)

    def _idle_ids(self) -> list[str]:
        """Quiet sessions with nothing running or queued: a job outlives its dialog and the window (D-06-10).
        `busy()` waits on the worker's state lock, which a cancel holds across a PostgreSQL cancel (up to 3 s), so it
        is asked outside this manager's lock: `attach` takes that lock on the event loop."""
        with self._lock:
            quiet = [(sid, e.worker) for sid, e in self._sessions.items() if self._is_quiet(e)]
        return [sid for sid, worker in quiet if not worker.busy()]

    def _pop_and_close(self, session_id: str, idle_only: bool = False) -> bool:
        """Remove a session and close its worker. False when it is gone (or, with idle_only, no longer idle: busy,
        or attached or active since). A job of it that never reported its outcome is listed as unknown (pre-flight L3).

        Never raises for a failing worker: bulk callers (reaper, profile deletion, shutdown) must reach every session.
        """
        if idle_only:
            with self._lock:
                entry = self._sessions.get(session_id)
            if entry is None or entry.worker.busy():  # outside the lock, as in _idle_ids
                return False
        with self._lock:
            entry = self._sessions.get(session_id)
            # A request is submitted by an attached client or by a copy holding a lease on the session, which counts as
            # attached too (06c): one submitted since `busy()` shows here
            if entry is None or (idle_only and not self._is_quiet(entry)):
                return False
            del self._sessions[session_id]
        try:
            entry.worker.close()
        except Exception:
            logger.exception("Error while closing session {}", session_id)
        logger.info("Closed session {}", session_id)
        if (abandoned := self.jobs.abandon(session_id)) > 0:
            logger.warning("Session {} closed before {} of its job(s) reported an outcome: listed as unknown",
                           session_id, abandoned)
        return True
