import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from loguru import logger
from pydantic import BaseModel

from ..domain.errors import SessionNotFound
from ..domain.metadata import ServerInfo
from ..services.connection_service import ConnectionService
from ..services.profile_service import ProfileService
from .hooks import SessionHooksFactory
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


class SessionManager:
    """User sessions (one per editor tab). Closed explicitly or after idling without a WebSocket."""

    def __init__(
        self, connections: ConnectionService, profiles: ProfileService, hooks: SessionHooksFactory | None = None,
        idle_timeout: float = 600.0,
    ) -> None:
        self._connections = connections
        self._profiles = profiles
        self._hooks = hooks  # None: workers keep their defaults (everything runs, nothing is recorded)
        self._idle_timeout = idle_timeout
        self._sessions: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def create(self, request: SessionCreateRequest) -> SessionInfo:
        profile = self._profiles.get(request.profile_id)
        secrets = self._profiles.resolve_secrets(profile)
        session_id = uuid4().hex
        worker = SessionWorker(
            f"{profile.name}:{session_id[:8]}",
            lambda: self._connections.open(profile, request.database, secrets),
        )
        try:
            worker.start()
            server = worker.call(lambda c: c.server_info())
            if self._hooks is not None:
                worker.configure(self._hooks.build(profile.id, server.current_database))  # before it is registered
        except BaseException:
            worker.close()  # whatever failed, never leave the thread, connection or tunnel behind
            raise
        info = SessionInfo(session_id=session_id, profile_id=profile.id, database=server.current_database, server=server)
        with self._lock:
            self._sessions[session_id] = _Entry(worker=worker, info=info, detached_at=time.monotonic())
        logger.info("Opened session {} for '{}'", session_id, profile.name)
        return info

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
        entry.worker.set_emitter(emit)
        entry.worker.replay_state()  # a client attaching mid-transaction learns the state before any `finished`
        return entry.worker

    def detach(self, session_id: str, emit: Emitter) -> None:
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return
            entry.attached = max(0, entry.attached - 1)
            if entry.attached == 0:
                entry.detached_at = time.monotonic()
        entry.worker.clear_emitter(emit)

    def close(self, session_id: str) -> None:
        if not self._pop_and_close(session_id):
            raise SessionNotFound(f"Session {session_id} not found")

    def close_for_profile(self, profile_id: UUID) -> None:
        with self._lock:
            ids = [sid for sid, e in self._sessions.items() if e.info.profile_id == profile_id]
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

    def _is_idle(self, entry: _Entry) -> bool:
        return entry.attached == 0 and time.monotonic() - entry.detached_at >= self._idle_timeout

    def _idle_ids(self) -> list[str]:
        with self._lock:
            return [sid for sid, e in self._sessions.items() if self._is_idle(e)]

    def _pop_and_close(self, session_id: str, idle_only: bool = False) -> bool:
        """Remove a session and close its worker. False when it is gone (or, with idle_only, no longer idle).

        Never raises for a failing worker: bulk callers (reaper, profile deletion, shutdown) must reach every session.
        """
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None or (idle_only and not self._is_idle(entry)):
                return False
            del self._sessions[session_id]
        try:
            entry.worker.close()
        except Exception:
            logger.exception("Error while closing session {}", session_id)
        logger.info("Closed session {}", session_id)
        return True
