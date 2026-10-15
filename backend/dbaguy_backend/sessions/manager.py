import threading
import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from loguru import logger
from pydantic import BaseModel

from ..domain.errors import SessionNotFound
from ..domain.metadata import ServerInfo
from ..services.connection_service import ConnectionService
from ..services.profile_service import ProfileService
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

    def __init__(self, connections: ConnectionService, profiles: ProfileService, idle_timeout: float = 600.0) -> None:
        self._connections = connections
        self._profiles = profiles
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
        worker.start()
        try:
            server = worker.call(lambda c: c.server_info())
        except BaseException:
            worker.close()
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
        with self._lock:
            entry = self._sessions.pop(session_id, None)
        if entry is None:
            raise SessionNotFound(f"Session {session_id} not found")
        entry.worker.close()
        logger.info("Closed session {}", session_id)

    def close_for_profile(self, profile_id: UUID) -> None:
        with self._lock:
            ids = [sid for sid, e in self._sessions.items() if e.info.profile_id == profile_id]
        for sid in ids:
            self.close(sid)

    def reap_idle(self) -> int:
        now = time.monotonic()
        with self._lock:
            idle = [
                sid for sid, e in self._sessions.items()
                if e.attached == 0 and now - e.detached_at >= self._idle_timeout
            ]
        for sid in idle:
            logger.info("Closing idle session {}", sid)
            self.close(sid)
        return len(idle)

    def close_all(self) -> None:
        with self._lock:
            ids = list(self._sessions)
        for sid in ids:
            self.close(sid)
