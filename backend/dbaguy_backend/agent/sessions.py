"""MCP agents' sessions (spec ⑧ D-08-1): one per (profile, database), at most MAX_AGENT_SESSIONS, one call at a time
each. A call leases its session: it waits for the session's lock within the call's deadline (`busy` past it), and holds
the session in the SessionManager so the reaper never closes it between the frame's statements. A session that opens is
first checked for what the lexer assumes of it (pre-flight M1: PostgreSQL's standard_conforming_strings), and the
profile's login is read once, on the agents' catalog connection of its own database (CR-08-1, P16)."""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseConnection, RunOptions
from ..domain.data_jobs import ENGINE_NAMES
from ..domain.errors import SessionNotFound
from ..domain.execution import QueryContext
from ..domain.profiles import AnyProfile, Engine, profile_database
from ..sessions.manager import SessionManager
from ..sessions.worker import SessionWorker
from ..sql.dialects import SessionAssumption, read_frame
from .login import LoginRegistry
from .tools import ToolFailure

MAX_AGENT_SESSIONS = 8
_BUSY = "Another call on '{}' was still running"
_ALL_BUSY = "All {} of DbaGuy's agent connections are busy: try again"
_ASSUMED = ("This {engine} login has {setting} {found}, which DbaGuy's read-only checks assume is {wanted}: give the "
            "AI a login (or database) with it {wanted}, or duplicate this connection")
_CHECK_S = 0.1  # the least time the check of a session that opened gets, its call's deadline past
_INTERNAL = RunOptions(describe=False)
_Key = tuple[UUID, str | None]


@dataclass
class _Slot:
    key: _Key
    lock: threading.Lock = field(default_factory=threading.Lock)
    session_id: str | None = None
    server_version: str = ""
    default_context: QueryContext | None = None
    last_used: float = 0.0
    gone: bool = False  # taken out of the registry (evicted, or its profile changed): a waiting call gets another


@dataclass(frozen=True)
class Lease:
    """A session for one call: its worker, what the server said when it opened, and how to drop it (a frame that did
    not end cleanly: the next call gets a new session, never one that may hold a transaction or a lock)."""

    worker: SessionWorker
    server_version: str
    default_context: QueryContext | None
    drop: Callable[[], None]


def _value(query: str, connection: DatabaseConnection) -> str | None:
    """The first cell of `query`'s first row, as text (DbaGuy's own statement, on the worker thread)."""
    result = connection.run(query, _INTERNAL)
    try:
        rows = result.fetch(1)
    finally:
        result.close()
    return None if not rows or rows[0][0] is None else str(rows[0][0])


class AgentSessions:
    def __init__(self, sessions: SessionManager, login: LoginRegistry,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._sessions = sessions
        self._login = login
        self._clock = clock
        self._slots: dict[_Key, _Slot] = {}
        self._lock = threading.Lock()

    def key(self, profile: AnyProfile, database: str | None) -> _Key:
        """SQLite has one database; Couchbase's requests carry their context instead: one session each. No database
        means the profile's own: one session for both."""
        if profile.engine in (Engine.SQLITE, Engine.COUCHBASE):
            return profile.id, None
        return profile.id, database or profile_database(profile) or None

    @contextmanager
    def lease(self, profile: AnyProfile, database: str | None, deadline: float) -> Iterator[Lease]:
        """The session of (profile, database), opened when there is none, its lock taken before `deadline`. A session
        that opens and does not answer its check before `deadline` is closed: TimeoutError. A slot left without a
        session (it could not open, or its call closed it) leaves the registry: only sessions count toward the cap."""
        key = self.key(profile, database)
        while True:
            slot = self._slot(key)
            if not slot.lock.acquire(timeout=max(0.0, deadline - self._clock())):
                raise ToolFailure("busy", _BUSY.format(profile.name))
            if not slot.gone:
                break
            slot.lock.release()  # taken out while this call waited for it: the key's slot now
        held: str | None = None
        try:
            worker = self._worker(slot, profile, key[1], deadline)
            held = slot.session_id
            self._login.ensure(profile)  # known: nothing is read (a query that failed: tried again after a minute)
            yield Lease(worker, slot.server_version, slot.default_context, partial(self._drop, slot))
        finally:
            if held is not None:
                self._sessions.release(held)
            slot.last_used = self._clock()
            if slot.gone and slot.session_id is not None:  # taken out during the call: nothing else would close it
                self._close_session(slot.session_id)
                slot.session_id = None
            if slot.session_id is None:
                self._forget(slot)
            slot.lock.release()

    def close(self, profile_id: UUID | None = None) -> None:
        """A profile changed (its sessions close), or the MCP server stopped (all close)."""
        with self._lock:
            slots = [s for k, s in self._slots.items() if profile_id is None or k[0] == profile_id]
            for slot in slots:
                slot.gone = True
                del self._slots[slot.key]
        self._sessions.close_agents(profile_id)

    def _forget(self, slot: _Slot) -> None:
        """Out of the registry (a call waiting for it then takes the key's next slot). The caller holds its lock."""
        with self._lock:
            slot.gone = True
            if self._slots.get(slot.key) is slot:
                del self._slots[slot.key]

    def _slot(self, key: _Key) -> _Slot:
        evicted: str | None = None
        with self._lock:
            slot = self._slots.get(key)
            if slot is not None:
                return slot
            if len(self._slots) >= MAX_AGENT_SESSIONS:
                evicted = self._evict_idle_locked()
            slot = self._slots[key] = _Slot(key, last_used=self._clock())
        if evicted is not None:
            self._close_session(evicted)
        return slot

    def _evict_idle_locked(self) -> str | None:
        """The least recently used slot whose call has ended gives way; its session id, to close (outside `_lock`)."""
        for slot in sorted(self._slots.values(), key=lambda s: s.last_used):
            if slot.lock.acquire(blocking=False):
                try:
                    slot.gone = True
                    del self._slots[slot.key]
                    session_id, slot.session_id = slot.session_id, None
                    return session_id
                finally:
                    slot.lock.release()
        raise ToolFailure("busy", _ALL_BUSY.format(MAX_AGENT_SESSIONS))

    def _worker(self, slot: _Slot, profile: AnyProfile, database: str | None, deadline: float) -> SessionWorker:
        """The slot's session, held for this call; a new one when it has none or the reaper closed it. One that opens
        and is refused, or whose check fails, is closed (and not held)."""
        if slot.session_id is not None:
            try:
                return self._sessions.hold(slot.session_id)
            except SessionNotFound:
                slot.session_id = None  # idle too long: reaped
        info = self._sessions.create_agent(profile.id, database)
        slot.session_id, slot.server_version = info.session_id, info.server.version
        slot.default_context = info.server.default_query_context
        worker = self._sessions.hold(info.session_id)
        try:
            if (assumed := read_frame(worker.dialect).assumes) is not None:
                self._check(worker, profile, assumed, deadline)
        except BaseException:
            self._sessions.release(info.session_id)
            self._drop(slot, quietly=True)
            raise
        return worker

    def _check(self, worker: SessionWorker, profile: AnyProfile, assumed: SessionAssumption, deadline: float) -> None:
        """Pre-flight M1: a session whose setting the lexer reads statements by is not what it assumes is refused."""
        found = worker.call(partial(_value, assumed.query), timeout=max(_CHECK_S, deadline - self._clock()))
        if found != assumed.value:
            logger.warning("MCP: refused an agent session on {}: {} is {}", profile.id, assumed.setting, found)
            raise ToolFailure("refused", _ASSUMED.format(
                engine=ENGINE_NAMES[Engine(profile.engine)], setting=assumed.setting,
                found=found, wanted=assumed.value))

    def _drop(self, slot: _Slot, quietly: bool = False) -> None:
        session_id, slot.session_id = slot.session_id, None
        if session_id is not None:
            if not quietly:
                logger.info("MCP: closing an agent session after a call that did not end cleanly")
            self._close_session(session_id)

    def _close_session(self, session_id: str) -> None:
        try:
            self._sessions.close(session_id)
        except SessionNotFound:
            pass
