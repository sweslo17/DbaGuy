"""The MCP tools' logic (spec ⑧ §3.5), without the SDK: each method returns a JSON-able dict or raises ToolFailure.
`agent/server.py` turns both into a CallToolResult (D-08-6). Every call reads the profiles again, so a connection the
user set to Off is gone from the next call on (R-08-5)."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseAdapter
from ..adapters.registry import AdapterRegistry
from ..domain.agent import McpAccess, ToolErrorCode
from ..domain.errors import PasswordRequired, ProfileNotFound
from ..domain.execution import QueryContext
from ..domain.profiles import AnyProfile, Engine, ServerProfile
from ..services.profile_service import ProfileService

_UNKNOWN = "No connection '{}' is shared with AI tools; list_connections lists them"
_AMBIGUOUS = "Several connections are named '{}': pass one of their ids ({})"
_SCHEMA_ONLY = "'{}' allows schema only: {} needs 'Read-only queries' in DbaGuy's connection settings"


class ToolFailure(Exception):
    """A tool's error result (§4): its code and a message for the agent. `logged`: whoever raised it wrote its log line
    already (a refusal names its reason), so the server does not add a second."""

    def __init__(self, code: ToolErrorCode, message: str, *, logged: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.logged = logged


@dataclass(frozen=True)
class Shared:
    """A connection shared with AI tools, as this call read it."""

    profile: AnyProfile
    adapter: DatabaseAdapter


def _database(profile: AnyProfile) -> str | None:
    return profile.database if isinstance(profile, ServerProfile) else None


class AgentTools:
    def __init__(self, profiles: ProfileService, registry: AdapterRegistry) -> None:
        self._profiles = profiles
        self._registry = registry

    # ---- connections ----

    def _shared_profiles(self) -> list[AnyProfile]:
        return [profile for profile in self._profiles.list_profiles() if profile.mcp_access != "off"]

    def shared(self, connection: str, tool: str, need: McpAccess = "schema") -> Shared:
        """The connection `connection` names (a profile's id, or its name: exact, then ignoring case), when it is shared
        at `need` or more. Off, missing: unknown_connection; a name several have: ambiguous; `schema` when the tool
        needs `query`: not_allowed."""
        profile = self._find(connection.strip())
        if need == "query" and profile.mcp_access != "query":
            raise ToolFailure("not_allowed", _SCHEMA_ONLY.format(profile.name, tool))
        return Shared(profile, self._registry.get(profile.engine))

    def _find(self, connection: str) -> AnyProfile:
        try:
            profile = self._profiles.get(UUID(connection))
        except (ValueError, ProfileNotFound):
            profile = None
        if profile is not None:
            if profile.mcp_access == "off":
                raise ToolFailure("unknown_connection", _UNKNOWN.format(connection))
            return profile
        shared = self._shared_profiles()
        named = [p for p in shared if p.name == connection] or [
            p for p in shared if p.name.casefold() == connection.casefold()]
        if not named:
            raise ToolFailure("unknown_connection", _UNKNOWN.format(connection))
        if len(named) > 1:
            raise ToolFailure("ambiguous", _AMBIGUOUS.format(connection, ", ".join(str(p.id) for p in named)))
        return named[0]

    def shared_counts(self) -> tuple[int, int]:
        """How many connections are shared at `schema` and at `query` (the Settings status line)."""
        shared = self._shared_profiles()
        return sum(p.mcp_access == "schema" for p in shared), sum(p.mcp_access == "query" for p in shared)

    def list_connections(self, *, client: str | None = None) -> dict[str, Any]:
        """Every connection whose access is not Off (§3.3), without connecting."""
        connections = [self._describe(profile) for profile in self._shared_profiles()]
        logger.info("MCP list_connections by {}: {} connections", client, len(connections))
        return {"connections": connections}

    def _describe(self, profile: AnyProfile) -> dict[str, Any]:
        adapter = self._registry.get(profile.engine)
        entry: dict[str, Any] = {
            "id": str(profile.id), "name": profile.name, "engine": profile.engine, "access": profile.mcp_access,
            "folder": profile.folder or None, "color": profile.color, "database": _database(profile),
            "read_only_by": "server" if adapter.capabilities.server_read_only else "dbaguy",
            "needs_unlock": self._needs_unlock(profile),
        }
        if profile.engine == Engine.COUCHBASE:
            try:
                context = QueryContext.parse(_database(profile))
            except ValueError:
                context = None
            entry["default_query_context"] = context.text() if context is not None else None
        return entry

    def _needs_unlock(self, profile: AnyProfile) -> bool:
        """A password it would ask for: a profile that does not save its password and has none unlocked this run. One
        that saves it says False without reading the Keychain (an agent's list must never make the Keychain ask)."""
        if profile.save_password:
            return False
        try:
            self._profiles.resolve_secrets(profile)  # its secrets live in memory only: no Keychain read
        except PasswordRequired:
            return True
        return False
