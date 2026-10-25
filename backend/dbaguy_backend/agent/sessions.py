"""MCP agents' sessions (spec ⑧ D-08-1): one per (profile, database), at most MAX_AGENT_SESSIONS, one call at a time
each. A call leases its session: it waits for the session's lock within the call's deadline (`busy` past it), waits
for the session to open within it too (ruling T6-I2), and holds it in the SessionManager so the reaper never closes it
between the frame's statements. A session opens on a thread of its own, kept in its slot: a call whose deadline passes
leaves it there, and the next call on the key waits for the same open, so a stalled server costs one open per key, and
in-flight opens count toward MAX_AGENT_SESSIONS (re-review R-3); an open that ends with no call waiting is the next
call's. A session that opens on a server its frame cannot run on is refused (ruling T6-1). The profile's login is read
after a call, on a thread of its own (re-review R-2), on the agents' catalog connection of its own database (CR-08-1,
P16)."""

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from uuid import UUID

from loguru import logger

from ..domain.agent import ToolFailure
from ..domain.errors import SessionBusy, SessionNotFound
from ..domain.execution import QueryContext
from ..domain.profiles import AnyProfile, Engine, profile_database
from ..sessions.manager import SessionInfo, SessionManager
from ..sessions.worker import SessionWorker
from ..sql.dialects import read_frame
from .login import LoginRegistry

MAX_AGENT_SESSIONS = 8
TICK_S = 0.25  # how often a call that waits looks for its client's cancel (the lock here, the statement in the reader)
_BUSY = "Another call on '{}' was still running"
_ALL_BUSY = "All {} of DbaGuy's agent connections are busy: try again"
_CLOSED_WHILE_OPENING = "The connection was closed while it opened (its settings changed, or the MCP server stopped)"
LOGIN_RETRY_S = 60.0  # a login read that failed (a connection, a password) is not started again sooner
_Key = tuple[UUID, str | None]


class WaitCancelled(Exception):
    """The client gave up while its call waited for the session's lock (review I2): nothing was opened or sent."""


@dataclass
class _Slot:
    key: _Key
    lock: threading.Lock = field(default_factory=threading.Lock)
    session_id: str | None = None
    server_version: str = ""
    default_context: QueryContext | None = None
    last_used: float = 0.0
    gone: bool = False  # taken out of the registry (evicted, or its profile changed): a waiting call gets another
    opening: Future[SessionInfo] | None = None  # its session, opening on a thread of its own (re-review R-3)


@dataclass(frozen=True)
class Lease:
    """A session for one call: its worker, what the server said when it opened, and how to drop it (a frame that did
    not end cleanly: the next call gets a new session, never one that may hold a transaction or a lock)."""

    worker: SessionWorker
    server_version: str
    default_context: QueryContext | None
    drop: Callable[[], None]


class AgentSessions:
    def __init__(self, sessions: SessionManager, login: LoginRegistry,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._sessions = sessions
        self._login = login
        self._clock = clock
        self._slots: dict[_Key, _Slot] = {}
        self._lock = threading.Lock()
        self._login_reading: set[UUID] = set()  # profiles whose login is being read (one read each at a time)
        self._login_failed: dict[UUID, float] = {}  # when a login read last failed with anything but a query
        self._stops = 0  # how many times the MCP server stopped (`close()`): a call that began before opens nothing

    def key(self, profile: AnyProfile, database: str | None) -> _Key:
        """SQLite has one database; Couchbase's requests carry their context instead: one session each. No database
        means the profile's own: one session for both."""
        if profile.engine in (Engine.SQLITE, Engine.COUCHBASE):
            return profile.id, None
        return profile.id, database or profile_database(profile) or None

    def stops(self) -> int:
        """How many times the MCP server stopped: a tool call reads it as it begins (`lease(stops=)`, final review
        M2)."""
        with self._lock:
            return self._stops

    @contextmanager
    def lease(self, profile: AnyProfile, database: str | None, deadline: float, *,
              admit: Callable[[], object] | None = None, cancelled: Callable[[], bool] | None = None,
              stops: int | None = None) -> Iterator[Lease]:
        """The session of (profile, database), its lock taken and, when there is none, the session opened before
        `deadline` (TimeoutError past it). A slot left with neither a session nor an open in flight (it could not open,
        or its call closed it) leaves the registry; sessions and opens in flight count toward the cap (re-review R-3),
        and an open that failed with no call waiting stays until a call tries it again or the cap evicts it. While the
        lock is busy, and while the session opens, `cancelled` is looked at every TICK_S (WaitCancelled: the client gave
        up, review I2, T6 re-review 2 observation 2). With the lock, before anything opens: `cancelled` again, and the
        MCP server's stops since the tool call began (`stops`; now when not given): either is WaitCancelled (final
        review M2: a call that waited for a slot the stop closed would open a session in the key's new slot); then
        `admit`, which raises when the call may no longer read (review I1: the profile changed while the call
        waited)."""
        key = self.key(profile, database)
        began = self.stops() if stops is None else stops
        while True:
            slot = self._slot(key)
            self._acquire(slot, profile, deadline, cancelled)
            if not slot.gone:
                break
            slot.lock.release()  # taken out while this call waited for it: the key's slot now
        held: str | None = None
        try:
            if (cancelled is not None and cancelled()) or self.stops() != began:
                raise WaitCancelled
            if admit is not None:
                admit()
            worker = self._worker(slot, profile, key[1], deadline, cancelled)
            held = slot.session_id
            yield Lease(worker, slot.server_version, slot.default_context, partial(self._drop, slot))
        finally:
            if held is not None:
                self._sessions.release(held)
            slot.last_used = self._clock()
            if slot.gone and slot.session_id is not None:  # taken out during the call: nothing else would close it
                self._close_session(slot.session_id)
                slot.session_id = None
            if slot.session_id is None and slot.opening is None:  # an open in flight stays, for the key's next call
                self._forget(slot)
            slot.lock.release()

    def _acquire(self, slot: _Slot, profile: AnyProfile, deadline: float,
                 cancelled: Callable[[], bool] | None) -> None:
        """The slot's lock before `deadline` (`busy` past it), waiting TICK_S at a time for the client's cancel."""
        while not slot.lock.acquire(timeout=max(0.0, min(TICK_S, deadline - self._clock()))):
            if cancelled is not None and cancelled():
                raise WaitCancelled
            if self._clock() >= deadline:
                raise ToolFailure("busy", _BUSY.format(profile.name))

    def read_login(self, profile: AnyProfile, cancelled: bool = False) -> None:
        """After a call: the profile's login (CR-08-1), read unless known, on a thread of its own (re-review R-2: the
        call's answer never waits for it), at most one read per profile at a time, none after the client's cancel. A
        query that failed is tried again after a minute (LoginRegistry), and so is anything else here (a connection or
        a password, LOGIN_RETRY_S), logged by its code."""
        if cancelled or not self._login.due(profile.id):
            return
        with self._lock:
            failed = self._login_failed.get(profile.id)
            if profile.id in self._login_reading or (failed is not None and self._clock() - failed < LOGIN_RETRY_S):
                return
            self._login_reading.add(profile.id)
        threading.Thread(target=self._read_login, args=(profile,), name=f"mcp-login-{profile.id}", daemon=True).start()

    def _read_login(self, profile: AnyProfile) -> None:
        try:
            self._login.ensure(profile)
        except Exception as exc:
            with self._lock:
                self._login_failed[profile.id] = self._clock()
            logger.warning("MCP: the login of {} could not be read ({})", profile.id,
                           getattr(exc, "code", None) or type(exc).__name__)
        finally:
            with self._lock:
                self._login_reading.discard(profile.id)

    def close(self, profile_id: UUID | None = None) -> None:
        """A profile changed (its sessions close), or the MCP server stopped (all close: `stops` counts it)."""
        with self._lock:
            if profile_id is None:
                self._stops += 1
            slots = [s for k, s in self._slots.items() if profile_id is None or k[0] == profile_id]
            for slot in slots:
                slot.gone = True  # an open in flight closes its session itself when it ends
                del self._slots[slot.key]
            if profile_id is None:
                self._login_failed.clear()
            else:
                self._login_failed.pop(profile_id, None)  # a saved profile's login is read again at once
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
        """The least recently used slot whose call has ended and whose session is not still opening gives way (an open
        in flight counts toward the cap until it ends, re-review R-3); its session id, to close (outside `_lock`)."""
        for slot in sorted(self._slots.values(), key=lambda s: s.last_used):
            if slot.opening is not None and not slot.opening.done():
                continue
            if slot.lock.acquire(blocking=False):
                try:
                    slot.gone = True
                    del self._slots[slot.key]
                    session_id, slot.session_id = slot.session_id or _opened(slot.opening), None
                    slot.opening = None
                    return session_id
                finally:
                    slot.lock.release()
        raise ToolFailure("busy", _ALL_BUSY.format(MAX_AGENT_SESSIONS))

    def _worker(self, slot: _Slot, profile: AnyProfile, database: str | None, deadline: float,
                cancelled: Callable[[], bool] | None = None) -> SessionWorker:
        """The slot's session, held for this call; a new one, opened within `deadline`, when it has none or the reaper
        closed it. One on a server the frame cannot run on is refused and closed (ruling T6-1: the version its opening
        read, no round trip)."""
        while True:
            opened = slot.session_id is None
            if opened:
                self._take_open(slot, profile, database, deadline, cancelled)
            assert slot.session_id is not None
            try:
                worker = self._sessions.hold(slot.session_id)
            except SessionNotFound:  # reaped while idle, or closed once it opened (its profile changed)
                slot.session_id = None
                if slot.gone:
                    raise SessionBusy(_CLOSED_WHILE_OPENING) from None
                continue
            break
        required = read_frame(worker.dialect).requires
        if opened and required is not None and (why := required.refusal(slot.server_version)) is not None:
            logger.warning("MCP: refused an agent session on {}: the server's version", profile.id)
            self._sessions.release(slot.session_id)
            self._drop(slot, quietly=True)
            raise ToolFailure("refused", why)
        return worker

    def _take_open(self, slot: _Slot, profile: AnyProfile, database: str | None, deadline: float,
                   cancelled: Callable[[], bool] | None = None) -> None:
        """The slot's session from the open in flight, or from a new one (none once the deadline has passed, nor once
        an earlier open failed with no call waiting: that one is tried again). Waited for TICK_S at a time, looking at
        `cancelled` (WaitCancelled, T6 re-review 2 observation 2). TimeoutError when it is still opening at `deadline`,
        WaitCancelled when the client gave up: the open stays in the slot for the key's next call (re-review R-3). An
        open that ended just as a wait timed out is the call's (T6 re-review 2 N-1: it was orphaned, a session no slot
        knew of). The caller holds the slot's lock."""
        if slot.opening is not None and slot.opening.done() and slot.opening.exception() is not None:
            slot.opening = None
        if slot.opening is None:
            if self._clock() >= deadline:
                raise TimeoutError
            slot.opening = self._open(slot, profile.id, database)
        opening = slot.opening
        while True:
            if cancelled is not None and cancelled():
                raise WaitCancelled  # the open stays in its slot
            left = deadline - self._clock()
            try:
                info = opening.result(timeout=max(0.0, min(TICK_S, left)))
                break
            except BaseException:
                if opening.done() and opening.exception() is None:
                    info = opening.result()  # it ended as the wait timed out (N-1)
                    break
                if opening.done():
                    slot.opening = None  # it failed while this call waited: the call says why
                    raise
                if left <= TICK_S:
                    raise TimeoutError from None  # still opening: kept for the next call on the key
        slot.opening = None
        slot.session_id, slot.server_version = info.session_id, info.server.version
        slot.default_context = info.server.default_query_context

    def _open(self, slot: _Slot, profile_id: UUID, database: str | None) -> Future[SessionInfo]:
        """The slot's session, opened on a thread of its own within the SessionManager's own limits (30 s to connect,
        120 s for its server_info). One that ends after its slot left the registry is closed: no call would take it."""
        opening: Future[SessionInfo] = Future()

        def run() -> None:
            try:
                info = self._sessions.create_agent(profile_id, database)
            except BaseException as exc:
                opening.set_exception(exc)
                return
            if slot.gone:
                self._close_session(info.session_id)
                opening.set_exception(SessionBusy(_CLOSED_WHILE_OPENING))
                return
            opening.set_result(info)

        threading.Thread(target=run, name=f"mcp-open-{profile_id}", daemon=True).start()
        return opening

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


def _opened(opening: Future[SessionInfo] | None) -> str | None:
    """The session id of an open that ended well and no call took yet."""
    if opening is None or not opening.done() or opening.exception() is not None:
        return None
    return opening.result().session_id
