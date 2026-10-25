"""What each shared connection's login may do beyond reading (spec ⑧ CR-08-1, §3.3): read once by an agent's connection
(the first agent session or catalog call), kept in memory until the profile changes, shown by list_connections and the
connection dialog. The queries are the adapters' (`DatabaseConnection.login_privileges`)."""

import threading
from collections.abc import Callable
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseConnection, StatementError
from ..domain.agent import LoginPrivileges
from ..domain.errors import QueryFailed
from ..domain.profiles import AnyProfile, profile_database
from ..services.metadata_service import MetadataService


def _read_on(connection: DatabaseConnection) -> LoginPrivileges | None:
    return connection.login_privileges()


class LoginRegistry:
    def __init__(self, metadata: MetadataService) -> None:
        self._metadata = metadata
        self._known: dict[UUID, LoginPrivileges | None] = {}  # None: the engine has nothing to tell
        self._changes: dict[UUID, int] = {}  # how many times each profile was forgotten: a read in flight checks it
        self._lock = threading.Lock()

    def peek(self, profile_id: UUID) -> LoginPrivileges | None:
        """What is known, without connecting: None until a connection has read it."""
        with self._lock:
            return self._known.get(profile_id)

    def remember(self, profile: AnyProfile, read: Callable[[], LoginPrivileges | None]) -> None:
        """Read it with `read` unless known. A query that fails (a privilege the catalog needs) is logged by code and
        tried again next time; a password or connection failure is the caller's (its call fails on it anyway)."""
        with self._lock:
            if profile.id in self._known:
                return
            changes = self._changes.get(profile.id, 0)
        try:
            privileges = read()
        except (StatementError, QueryFailed) as exc:
            logger.warning("MCP: the login of '{}' could not be read ({})", profile.name,
                           getattr(exc, "code", None) or type(exc).__name__)
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
        tool of it uses too: the key is the database's name, not None, so no second catalog connection opens for this."""
        return self._metadata.with_connection(profile.id, profile_database(profile) or None, _read_on, agent=True)

    def forget(self, profile_id: UUID) -> None:
        with self._lock:
            self._known.pop(profile_id, None)
            self._changes[profile_id] = self._changes.get(profile_id, 0) + 1

    def _keep(self, profile: AnyProfile, privileges: LoginPrivileges | None, changes: int) -> None:
        with self._lock:
            if self._changes.get(profile.id, 0) != changes:
                return  # the profile changed while this was read: what it says is the old login's, not the new one's
            self._known[profile.id] = privileges
        if privileges is not None:
            logger.info("MCP: the login of '{}' can: {}", profile.name, privileges.summary())
