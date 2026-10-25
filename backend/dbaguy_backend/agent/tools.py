"""The MCP tools' logic (spec ⑧ §3.5), without the SDK: each method returns a JSON-able dict or raises ToolFailure.
`agent/server.py` turns both into a CallToolResult (D-08-6). Every call reads the profiles again, so a connection the
user set to Off is gone from the next call on (R-08-5)."""

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseAdapter, StatementError
from ..adapters.registry import AdapterRegistry
from ..domain.agent import McpAccess, ToolErrorCode
from ..domain.errors import (
    ConnectionFailed, NotFound, PasswordRequired, ProfileNotFound, QueryFailed, ValidationFailed,
)
from ..domain.execution import QueryContext
from ..domain.metadata import Folder, MetadataRequest, NodeKind, ObjectNode, PathSegment, path_database
from ..domain.profiles import AnyProfile, Engine, profile_database
from ..domain.schema import SchemaSnapshot, SnapshotRelation
from ..services.metadata_service import MetadataService
from ..services.profile_service import ProfileService
from ..services.schema_cache import SchemaCache
from .login import LoginRegistry

LIST_LIMIT, SEARCH_LIMIT, LIMIT_MAX = 100, 50, 1000  # list_objects, search_objects (§3.5)
_UNKNOWN = "No connection '{}' is shared with AI tools; list_connections lists them"
_LOCKED = ("'{}' needs its password: connect to it in DbaGuy first (or unlock it). DbaGuy never asks for a password "
           "over MCP")
_NO_OBJECT = "Pass the object's path (from list_objects or search_objects), or its name"
_NO_SUCH_NAME = "No table, view or collection named {} on '{}'"
_SEVERAL_NAMES = "Several objects match {}: {}"
_NO_SNAPSHOT = "Keys and foreign keys could not be read: {}"
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


@contextmanager
def answered(profile: AnyProfile) -> Iterator[None]:
    """What the app's services raise, as a tool's error (§4). The server's own text stays in the message, which only the
    agent sees (`server.run_tool` logs the code)."""
    try:
        yield
    except PasswordRequired:
        raise ToolFailure("password_required", _LOCKED.format(profile.name)) from None
    except NotFound as exc:
        raise ToolFailure("not_found", exc.message) from None
    except ConnectionFailed as exc:
        raise ToolFailure("connection_failed", exc.message) from None
    except (QueryFailed, StatementError) as exc:
        raise ToolFailure("query_failed", exc.message) from None
    except ValidationFailed as exc:
        raise ToolFailure("invalid_argument", exc.message) from None


def _path_text(path: list[PathSegment]) -> str:
    return "/".join(segment.name for segment in path) or "(root)"


def _node(node: ObjectNode) -> dict[str, Any]:
    return {"kind": node.kind, "name": node.name, "path": [s.model_dump() for s in node.path],
            "has_children": node.has_children, "detail": node.detail}


def _snapshot_database(shared: Shared, database: str | None) -> str | None:
    """The database whose snapshot a schema tool reads. With a connection per database (PostgreSQL, SQL Server): the
    one named, else the profile's own, so the snapshot and the catalog connection are the ones a path through it uses
    (and the app's own). Elsewhere one snapshot holds every database (`_narrowed_to`)."""
    if shared.adapter.capabilities.per_database_connection:
        return database or profile_database(shared.profile) or None
    return None


def _narrowed_to(shared: Shared, database: str | None) -> str | None:
    """The database to keep of a snapshot that holds several (MySQL's databases and SQLite's attached files are the
    tree's schemas; Couchbase's are its buckets); None when the snapshot is that one database's already."""
    return None if shared.adapter.capabilities.per_database_connection else database


def _within(narrow: str | None, database: str | None, schema: str) -> bool:
    """Whether an object (its snapshot's database or bucket, its schema or scope) is in `narrow` (`_narrowed_to`)."""
    return narrow is None or (database if database is not None else schema) == narrow


def _relation_of(snapshot: SchemaSnapshot, path: list[PathSegment]) -> SnapshotRelation | None:
    """The snapshot's relation at a tree path (its schema segment and its last segment), if it has one."""
    schema = next((s.name for s in path if s.kind == NodeKind.SCHEMA), None)
    for candidate in snapshot.schemas:
        if candidate.name == schema:
            return next((r for r in candidate.relations if r.name == path[-1].name), None)
    return None


class AgentTools:
    def __init__(self, profiles: ProfileService, registry: AdapterRegistry, metadata: MetadataService,
                 schema_cache: SchemaCache, login: LoginRegistry) -> None:
        self._profiles = profiles
        self._registry = registry
        self._metadata = metadata
        self._schema_cache = schema_cache
        self._login = login

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
            "folder": profile.folder or None, "color": profile.color, "database": profile_database(profile),
            "read_only_by": "server" if adapter.capabilities.server_read_only else "dbaguy",
            "needs_unlock": self._needs_unlock(profile),
            "login": self._login_of(profile),
        }
        if profile.engine == Engine.COUCHBASE:
            try:
                context = QueryContext.parse(profile_database(profile))
            except ValueError:
                context = None
            entry["default_query_context"] = context.text() if context is not None else None
        return entry

    # ---- schema tools: the agents' own read-only metadata connection (D-08-11) ----

    def list_objects(self, connection: str, path: list[PathSegment] | None = None, limit: int = LIST_LIMIT, *,
                     client: str | None = None) -> dict[str, Any]:
        shared = self.shared(connection, "list_objects")
        path = path or []
        with answered(shared.profile):
            self._login.ensure(shared.profile)
            nodes = self._metadata.children(MetadataRequest(profile_id=shared.profile.id, path=path), agent=True)
        logger.info("MCP list_objects on '{}' by {}: {} ({} objects)", shared.profile.name, client, _path_text(path),
                    len(nodes))
        return {"objects": [_node(node) for node in nodes[:limit]], "truncated": len(nodes) > limit}

    def search_objects(self, connection: str, pattern: str, database: str | None = None,
                       include_columns: bool = False, limit: int = SEARCH_LIMIT, *,
                       client: str | None = None) -> dict[str, Any]:
        """Tables, views and collections by name (⌘P's ranking), in `database` when one is named; with
        `include_columns`, also those with a column whose name holds `pattern` (and those columns)."""
        shared = self.shared(connection, "search_objects")
        pid = shared.profile.id
        scope, narrow = _snapshot_database(shared, database), _narrowed_to(shared, database)
        with answered(shared.profile):
            self._login.ensure(shared.profile)
            # One more than `limit`, so `truncated` says whether more match; every match when they are narrowed here
            hits = self._schema_cache.search(pid, pattern, scope, limit + 1 if narrow is None else sys.maxsize,
                                             agent=True)
            found = [{"database": h.database, "schema": h.schema_name, "name": h.name, "kind": h.kind,
                      "path": [s.model_dump() for s in h.path]}
                     for h in hits if _within(narrow, h.database, h.schema_name)][:limit + 1]
            if include_columns and (needle := pattern.strip().lower()):
                snapshot = self._schema_cache.get(pid, scope, agent=True)
                self._add_column_hits(shared, snapshot, needle, narrow, found, limit)
        logger.info("MCP search_objects on '{}' by {}: {} matches", shared.profile.name, client, len(found))
        return {"objects": found[:limit], "truncated": len(found) > limit}

    @staticmethod
    def _add_column_hits(shared: Shared, snapshot: SchemaSnapshot, needle: str, narrow: str | None,
                         found: list[dict[str, Any]], limit: int) -> None:
        """Give each hit its columns that hold `needle`, and add the relations that only such a column matches while
        there is room (one past `limit`, which marks the result truncated)."""
        by_name = {(hit["schema"], hit["name"]): hit for hit in found}
        for schema in snapshot.schemas:
            if not _within(narrow, snapshot.database, schema.name):
                continue
            for relation in schema.relations:
                columns = [c.name for c in relation.columns if needle in c.name.lower()]
                if not columns:
                    continue
                hit = by_name.get((schema.name, relation.name))
                if hit is None:
                    if len(found) > limit:
                        continue  # no room: the hits found so far still get their columns
                    path = shared.adapter.relation_path(snapshot.database, schema.name, relation.name, relation.kind)
                    hit = {"database": snapshot.database, "schema": schema.name, "name": relation.name,
                           "kind": relation.kind, "path": [s.model_dump() for s in path]}
                    found.append(hit)
                    by_name[(schema.name, relation.name)] = hit
                hit["columns"] = columns

    def describe(self, connection: str, path: list[PathSegment] | None = None, database: str | None = None,
                 schema: str | None = None, name: str | None = None, include_ddl: bool = False, *,
                 client: str | None = None) -> dict[str, Any]:
        """One object: its columns, keys, foreign keys, indexes and comment; its DDL when asked. By `path`, or by name
        (`schema` and `database` narrow it): exact first, then ignoring case."""
        shared = self.shared(connection, "describe")
        pid = shared.profile.id
        snapshot: SchemaSnapshot | None = None
        with answered(shared.profile):
            self._login.ensure(shared.profile)
            if not path:
                if not name:
                    raise ToolFailure("invalid_argument", _NO_OBJECT)
                path, snapshot = self._path_by_name(shared, database, schema, name)
            request = MetadataRequest(profile_id=pid, path=path)
            detail = self._metadata.detail(request, agent=True)
            indexes = self._indexes(request)
        notes: list[str] = []
        relation: SnapshotRelation | None = None
        if shared.profile.engine != Engine.COUCHBASE:
            try:
                if snapshot is None:
                    snapshot = self._schema_cache.get(pid, path_database(path), agent=True)
                relation = _relation_of(snapshot, path)
            except Exception as exc:  # the detail stands without them
                notes.append(_NO_SNAPSHOT.format(getattr(exc, "message", type(exc).__name__)))
        columns = detail.columns or []
        primary = relation.primary_key if relation is not None else [c.name for c in columns if c.is_primary_key]
        result: dict[str, Any] = {
            "kind": detail.kind, "name": detail.name, "path": [s.model_dump() for s in path],
            "columns": [{"name": c.name, "type": c.type_name, "nullable": c.nullable, "default": c.default,
                         "comment": c.comment} for c in columns],
            "primary_key": primary,
            "foreign_keys": [{"name": fk.name, "columns": fk.columns, "references": f"{fk.ref_schema}.{fk.ref_table}",
                              "ref_columns": fk.ref_columns} for fk in (relation.foreign_keys if relation else [])],
            "indexes": indexes, "comment": detail.comment,
        }
        if include_ddl:
            result["ddl"] = detail.ddl
            result["ddl_is_generated"] = detail.ddl_is_generated
        if notes:
            result["notes"] = notes
        logger.info("MCP describe on '{}' by {}: {}", shared.profile.name, client, _path_text(path))
        return result

    def _indexes(self, request: MetadataRequest) -> list[dict[str, Any]]:
        """The object's indexes: its `indexes` folder's nodes, or index nodes right under it (Couchbase)."""
        children = self._metadata.children(request, agent=True)
        found = [child for child in children if child.kind == NodeKind.INDEX]
        if any(child.kind == NodeKind.FOLDER and child.name == Folder.INDEXES for child in children):
            folder = request.model_copy(update={"path": [*request.path,
                                                          PathSegment(kind=NodeKind.FOLDER, name=Folder.INDEXES)]})
            found += [child for child in self._metadata.children(folder, agent=True) if child.kind == NodeKind.INDEX]
        return [{"name": index.name, "detail": index.detail} for index in found]

    def _path_by_name(self, shared: Shared, database: str | None, schema: str | None,
                      name: str) -> tuple[list[PathSegment], SchemaSnapshot]:
        """The object named `name` in the snapshot (and the snapshot): exact, then ignoring case, in every part given;
        one, or `ambiguous`. Names never reach SQL here: the path they lead to goes through the catalog's own queries
        (bound or quoted)."""
        snapshot = self._schema_cache.get(shared.profile.id, _snapshot_database(shared, database), agent=True)
        narrow = _narrowed_to(shared, database)
        candidates = [(snapshot.database, s.name, r.name, r.kind) for s in snapshot.schemas for r in s.relations]
        candidates += [(k.bucket, k.scope, k.collection, "collection") for k in snapshot.keyspaces]

        def matching(same: Callable[[str, str], bool]) -> list[tuple[Any, ...]]:
            return [c for c in candidates if same(c[2], name) and (schema is None or same(c[1], schema))
                    and (narrow is None or same(c[0] if c[0] is not None else c[1], narrow))]

        found = matching(str.__eq__) or matching(lambda a, b: a.casefold() == b.casefold())
        shown = ".".join(part for part in (database, schema, name) if part)
        if not found:
            raise ToolFailure("not_found", _NO_SUCH_NAME.format(shown, shared.profile.name))
        if len(found) > 1:
            raise ToolFailure("ambiguous", _SEVERAL_NAMES.format(shown, ", ".join(
                ".".join(part for part in (c[0], c[1], c[2]) if part) for c in found[:20])))
        db, schema_name, relation, kind = found[0]
        return shared.adapter.relation_path(db, schema_name, relation, kind), snapshot

    def _login_of(self, profile: AnyProfile) -> dict[str, Any] | None:
        """CR-08-1: what the login may do beyond reading, once an agent's connection has read it (null before)."""
        privileges = self._login.peek(profile.id)
        if privileges is None:
            return None
        return {**privileges.model_dump(exclude_none=True), "summary": privileges.summary()}

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
