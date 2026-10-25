"""What each shared connection's login may do beyond reading (spec ⑧ CR-08-1, §3.3): read once by an agent's connection
(the first agent session or catalog call), kept in memory until the profile changes, shown by list_connections and the
connection dialog. The queries are the adapters' (`DatabaseConnection.login_privileges`)."""

import threading
import time
from collections.abc import Callable
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseConnection, StatementError
from ..adapters.registry import AdapterRegistry
from ..domain.agent import LoginPrivileges
from ..domain.errors import QueryFailed
from ..domain.profiles import AnyProfile, profile_database
from ..services.metadata_service import MetadataService


RETRY_AFTER_S = 60.0  # a read that failed is not tried again sooner (a schema tool calls `ensure` every time)


def _read_on(connection: DatabaseConnection) -> LoginPrivileges | None:
    return connection.login_privileges()


class LoginRegistry:
    def __init__(self, metadata: MetadataService, registry: AdapterRegistry,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._metadata = metadata
        self._registry = registry
        self._clock = clock
        self._known: dict[UUID, LoginPrivileges | None] = {}  # None: the engine has nothing to tell
        self._failed_at: dict[UUID, float] = {}  # when a read of the profile's login last failed
        self._changes: dict[UUID, int] = {}  # how many times each profile was forgotten: a read in flight checks it
        self._lock = threading.Lock()

    def due(self, profile_id: UUID) -> bool:
        """Whether a read would run now: not read yet (None counts as read: the engine has nothing to tell), and no
        query of it failed within RETRY_AFTER_S. An agent's call starts a login read only then (T6 re-review 2 N-3: it
        started a thread on every call that `remember` ended at once)."""
        with self._lock:
            if profile_id in self._known:
                return False
            failed = self._failed_at.get(profile_id)
            return failed is None or self._clock() - failed >= RETRY_AFTER_S

    def peek(self, profile_id: UUID) -> LoginPrivileges | None:
        """What is known, without connecting: None until a connection has read it."""
        with self._lock:
            return self._known.get(profile_id)

    def remember(self, profile: AnyProfile, read: Callable[[], LoginPrivileges | None]) -> None:
        """Read it with `read` unless known. A query that fails (a privilege the catalog needs) is logged by code, not
        tried again for RETRY_AFTER_S (or until the profile is saved); a password or connection failure is the caller's
        (its call fails on it anyway)."""
        with self._lock:
            if profile.id in self._known:
                return
            failed = self._failed_at.get(profile.id)
            if failed is not None and self._clock() - failed < RETRY_AFTER_S:
                return
            changes = self._changes.get(profile.id, 0)
        try:
            privileges = read()
        except (StatementError, QueryFailed) as exc:
            logger.warning("MCP: the login of {} could not be read ({})", profile.id,
                           getattr(exc, "code", None) or type(exc).__name__)
            with self._lock:
                if self._changes.get(profile.id, 0) == changes:  # not the old login's failure held against the new
                    self._failed_at[profile.id] = self._clock()
            return
        self._keep(profile, privileges, changes)

    def ensure(self, profile: AnyProfile) -> None:
        """Read on the agents' catalog connection of the profile's own database unless known (still unknown after a
        query that failed: `remember`)."""
        self.remember(profile, lambda: self._read_on_own_database(profile))

    def read(self, profile: AnyProfile) -> LoginPrivileges | None:
        """Read now (the connection dialog), on the agents' catalog connection, and keep it."""
        with self._lock:
            changes = self._changes.get(profile.id, 0)
        privileges = self._read_on_own_database(profile)
        self._keep(profile, privileges, changes)
        return privileges

    def _read_on_own_database(self, profile: AnyProfile) -> LoginPrivileges | None:
        """P16: the profile's own database (SQL Server's `can_write` is that database's), on the connection a schema
        tool of it uses too: the key is the database's name, not None, so no second catalog connection opens for this.
        An engine with nothing to tell (SQLite, Couchbase) is not connected to for it."""
        if not self._registry.get(profile.engine).capabilities.login_privileges:
            return None
        return self._metadata.with_connection(profile.id, profile_database(profile) or None, _read_on, agent=True)

    def forget(self, profile_id: UUID) -> None:
        with self._lock:
            self._known.pop(profile_id, None)
            self._failed_at.pop(profile_id, None)
            self._changes[profile_id] = self._changes.get(profile_id, 0) + 1

    def _keep(self, profile: AnyProfile, privileges: LoginPrivileges | None, changes: int) -> None:
        with self._lock:
            if self._changes.get(profile.id, 0) != changes:
                return  # the profile changed while this was read: what it says is the old login's, not the new one's
            self._known[profile.id] = privileges
            self._failed_at.pop(profile.id, None)
        if privileges is not None:
            logger.info("MCP: the login of {} can: {}", profile.id, privileges.summary())  # ids, never names (M8)
