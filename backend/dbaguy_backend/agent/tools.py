"""The MCP tools' logic (spec ⑧ §3.5), without the SDK: each method returns a JSON-able dict or raises ToolFailure.
`agent/server.py` turns both into a CallToolResult (D-08-6). Every call reads the profiles again, so a connection the
user set to Off is gone from the next call on (R-08-5)."""

import re
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseAdapter, DatabaseConnection, StatementError
from ..adapters.registry import AdapterRegistry
from ..domain.agent import McpAccess, ToolErrorCode
from ..domain.errors import (
    ConnectionFailed, NotFound, PasswordRequired, ProfileNotFound, QueryFailed, SecretStoreError, SessionBusy,
    ValidationFailed,
)
from ..domain.execution import QueryContext
from ..domain.metadata import Folder, NodeKind, ObjectDetail, ObjectNode, PathSegment
from ..domain.profiles import AnyProfile, Engine, profile_database
from ..domain.schema import SchemaSnapshot, SnapshotRelation
from ..domain.well_formed import has_lone_surrogate
from ..services.metadata_service import MetadataService
from ..services.profile_service import ProfileService
from ..services.schema_cache import SchemaCache
from .login import LoginRegistry
from .shape import MAX_CELL_DEFAULT, NOTE_ROOM, RESPONSE_CHARS, cut_text, fit_lists, fit_text

LIST_LIMIT, SEARCH_LIMIT, LIMIT_MAX = 100, 50, 1000  # list_objects, search_objects (§3.5)
COLUMNS_PER_HIT = 50  # search_objects' matching columns of one object: one wide table never fills the response alone
_UNKNOWN = "No connection '{}' is shared with AI tools; list_connections lists them"
_LOCKED = ("'{}' needs its password: connect to it in DbaGuy first (or unlock it). DbaGuy never asks for a password "
           "over MCP")
_NO_OBJECT = "Pass the object's path (from list_objects or search_objects), or its name"
_NO_SUCH_NAME = "No table, view or collection named {} on '{}'"
_SEVERAL_NAMES = "Several objects match {}: {}"
_NO_SNAPSHOT = "Keys and foreign keys could not be read: {}"
_NO_SECRET = "'{}': DbaGuy could not read its saved password from the Keychain: open DbaGuy and connect to it there first"
_CLOSED = "'{}': the connection was closed while the call waited (DbaGuy reset it): try again"
_NO_DATABASE = "No database named '{}' on '{}'"
_SEVERAL_DATABASES = "Several databases match '{}': {}"
_LEFT_OUT = f"{{}} left out to keep the response under {RESPONSE_CHARS:,} characters: {{}}"
_LONG_TEXT = f"Comments and defaults longer than {MAX_CELL_DEFAULT:,} characters are cut"
_AMBIGUOUS = "Several connections are named '{}': pass one of their ids ({})"
_SCHEMA_ONLY = "'{}' allows schema only: {} needs 'Read-only queries' in DbaGuy's connection settings"
_BAD_DATABASE = ("`database` holds a control character or half of a surrogate pair: pass a database's name as "
                 "list_objects shows it")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # C0, DEL, C1: never in a name an agent passes on, nor in a log


class ToolFailure(Exception):
    """A tool's error result (§4): its code and a message for the agent. `logged`: whoever raised it wrote its log line
    already (a refusal names its reason), so the server does not add a second. `connection`: the id of the profile it
    concerns, once known (the log line names it, never what the agent typed)."""

    def __init__(self, code: ToolErrorCode, message: str, *, logged: bool = False,
                 connection: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.logged = logged
        self.connection = connection


@dataclass(frozen=True)
class Shared:
    """A connection shared with AI tools, as this call read it."""

    profile: AnyProfile
    adapter: DatabaseAdapter


@contextmanager
def answered(profile: AnyProfile) -> Iterator[None]:
    """What the app's services raise, as a tool's error (§4), marked with the profile's id. The server's own text stays
    in the message, which only the agent sees (`server.run_tool` logs the code and the id)."""
    connection = str(profile.id)

    def failure(code: ToolErrorCode, message: str) -> ToolFailure:
        return ToolFailure(code, message, connection=connection)

    try:
        yield
    except ToolFailure as raised:
        raised.connection = raised.connection or connection
        raise
    except PasswordRequired:
        raise failure("password_required", _LOCKED.format(profile.name)) from None
    except SecretStoreError:  # the Keychain refused: a fixed text, never the store's own
        raise failure("connection_failed", _NO_SECRET.format(profile.name)) from None
    except SessionBusy:  # closed under the call twice (the profile saved, the MCP server stopped)
        raise failure("connection_failed", _CLOSED.format(profile.name)) from None
    except NotFound as exc:
        raise failure("not_found", exc.message) from None
    except ConnectionFailed as exc:
        raise failure("connection_failed", exc.message) from None
    except (QueryFailed, StatementError) as exc:
        raise failure("query_failed", exc.message) from None
    except ValidationFailed as exc:
        raise failure("invalid_argument", exc.message) from None


def _path_text(path: list[PathSegment]) -> str:
    """For DEBUG only: an agent's own text (a PostgreSQL schema that is not there lists nothing: it need not exist).
    `repr` keeps a newline or an escape from making a log line of its own."""
    return repr("/".join(segment.name for segment in path))[:300]


def _path_json(path: list[PathSegment]) -> list[dict[str, Any]]:
    return [segment.model_dump() for segment in path]


def _node(node: ObjectNode) -> dict[str, Any]:
    return {"kind": node.kind, "name": node.name, "path": _path_json(node.path), "has_children": node.has_children,
            "detail": node.detail}


def elapsed_ms(started: float) -> int:
    """Milliseconds since `started` (time.monotonic())."""
    return int((time.monotonic() - started) * 1000)


def checked_database(database: str | None) -> str | None:
    """An agent's `database` argument, refused (`invalid_argument`) when it holds a control character or a lone
    surrogate (review I3: MySQL's handshake reads a database up to a NUL, so `dbaguy\0` and a forged log line after it
    connected to `dbaguy` and reached the connect line)."""
    if database is not None and (CONTROL_CHARS.search(database) or has_lone_surrogate(database)):
        raise ToolFailure("invalid_argument", _BAD_DATABASE)
    return database


def _one_named(names: list[str], name: str) -> list[str]:
    """`names` that are `name`: exact, else ignoring case."""
    return [n for n in names if n == name] or [n for n in names if n.casefold() == name.casefold()]


def _current_and_listed(connection: DatabaseConnection, name: str) -> tuple[str | None, list[str]]:
    """The catalog connection's own database, and (unless that is `name`) the databases the server lists."""
    current = connection.current_database()
    if name == current:
        return current, []
    return current, [node.name for node in connection.list_children([]) if node.kind == NodeKind.DATABASE]


def _narrowed_to(shared: Shared, database: str | None) -> str | None:
    """The database to keep of a snapshot that holds several (MySQL's databases and SQLite's attached files are the
    tree's schemas; Couchbase's are its buckets); None when the snapshot is that one database's already."""
    return None if shared.adapter.capabilities.per_database_connection else database


def _within(narrow: str | None, database: str | None, schema: str) -> bool:
    """Whether an object (its snapshot's database or bucket, its schema or scope) is in `narrow` (`_narrowed_to`)."""
    return narrow is None or (database if database is not None else schema) == narrow


_RELATION_KINDS = frozenset({NodeKind.TABLE, NodeKind.PARTITION, NodeKind.VIEW, NodeKind.MATERIALIZED_VIEW,
                             NodeKind.SYNONYM})


def _relation_of(snapshot: SchemaSnapshot, path: list[PathSegment]) -> SnapshotRelation | None:
    """The snapshot's relation at a tree path (its schema segment and its last segment), if it has one. Only a path to a
    relation has one: a column or an index named like a table of its schema never borrows that table's keys (M-5)."""
    if not path or path[-1].kind not in _RELATION_KINDS:
        return None
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
        started = time.monotonic()
        connections = [self._describe(profile) for profile in self._shared_profiles()]
        logger.info("MCP list_connections: ok, {} connections, {} ms", len(connections), elapsed_ms(started))
        logger.debug("MCP list_connections by {!r}", client)
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

    def _done(self, tool: str, shared: Shared, started: float, count: int, what: str, client: str | None,
              detail: str = "") -> None:
        """D-08-14 and fix round 1 (I-2): at INFO only the tool, the connection's id, the outcome, a count and the time;
        what the agent chose (its client's name, a path, a pattern) at DEBUG."""
        logger.info("MCP {} on {}: ok, {} {}, {} ms", tool, shared.profile.id, count, what, elapsed_ms(started))
        logger.debug("MCP {} on {} by {!r}{}", tool, shared.profile.id, client, detail)

    def _catalog[T](self, shared: Shared, key: str | None, read: Callable[[DatabaseConnection], T]) -> T:
        return self._metadata.with_connection(shared.profile.id, key, read, agent=True)

    def _database_key(self, shared: Shared, name: str | None) -> tuple[str | None, str | None]:
        """(the server's spelling of database `name`, the key of its catalog connection and snapshot), for an engine
        with a connection per database (PostgreSQL, SQL Server). Fix round 1:
        - I-1: only a name the server lists reaches a connection or a snapshot key (exact, then ignoring case);
          another is not_found, and opens nothing. SQL Server logs in to `ANALYTICS`, `Analytics`, … and, past a NUL,
          to the login's default database: one connection per spelling, without this.
        - M-3: the profile's own database (None: the login's default) has one key whatever names it, so a path that
          spells it out reaches the connection and the snapshot a call without a database used.
        The list is read on the profile's own database's catalog connection, only when the name is not that one."""
        default = profile_database(shared.profile) or None
        if name is None:
            return None, default
        current, listed = self._catalog(shared, default, lambda c: _current_and_listed(c, name))
        if name == current:
            return current, default
        found = _one_named(listed, name)
        if not found:
            raise ToolFailure("not_found", _NO_DATABASE.format(name, shared.profile.name))
        if len(found) > 1:
            raise ToolFailure("ambiguous", _SEVERAL_DATABASES.format(name, ", ".join(found[:20])))
        return found[0], default if found[0] == current else found[0]

    def _located(self, shared: Shared, path: list[PathSegment]) -> tuple[list[PathSegment], str | None]:
        """`path` with its database spelled as the server lists it, and the key of the catalog connection it goes to."""
        if not shared.adapter.capabilities.per_database_connection:
            return path, None  # one catalog connection whatever the path
        if path and path[0].kind == NodeKind.DATABASE:
            spelled, key = self._database_key(shared, path[0].name)
            return [PathSegment(kind=NodeKind.DATABASE, name=spelled or path[0].name), *path[1:]], key
        return path, profile_database(shared.profile) or None  # the root (M-2), or a path its adapter refuses

    def list_objects(self, connection: str, path: list[PathSegment] | None = None, limit: int = LIST_LIMIT, *,
                     client: str | None = None) -> dict[str, Any]:
        started = time.monotonic()
        shared = self.shared(connection, "list_objects")
        with answered(shared.profile):
            self._login.ensure(shared.profile)
            path, key = self._located(shared, path or [])
            nodes = self._catalog(shared, key, lambda c: c.list_children(path))
        result: dict[str, Any] = {"objects": [_node(node) for node in nodes[:limit]], "truncated": len(nodes) > limit}
        if dropped := fit_lists(result, ("objects",), RESPONSE_CHARS - NOTE_ROOM):
            result["truncated"] = True
            result["notes"] = [_LEFT_OUT.format(f"{dropped['objects']} more objects",
                                                "pass a smaller limit or a deeper path")]
        self._done("list_objects", shared, started, len(result["objects"]), "objects", client,
                   f": {_path_text(path)}")
        return result

    def search_objects(self, connection: str, pattern: str, database: str | None = None,
                       include_columns: bool = False, limit: int = SEARCH_LIMIT, *,
                       client: str | None = None) -> dict[str, Any]:
        """Tables, views and collections by name (⌘P's ranking), in `database` when one is named; with
        `include_columns`, also those with a column whose name holds `pattern` (and those columns)."""
        started = time.monotonic()
        shared = self.shared(connection, "search_objects")
        database = checked_database(database)
        pid = shared.profile.id
        narrow = _narrowed_to(shared, database)
        with answered(shared.profile):
            self._login.ensure(shared.profile)
            _, scope = self._database_key(shared, database) if narrow is None else (None, None)
            # One more than `limit`, so `truncated` says whether more match; every match when they are narrowed here
            hits = self._schema_cache.search(pid, pattern, scope, limit + 1 if narrow is None else sys.maxsize,
                                             agent=True)
            found = [{"database": h.database, "schema": h.schema_name, "name": h.name, "kind": h.kind,
                      "path": _path_json(h.path)}
                     for h in hits if _within(narrow, h.database, h.schema_name)][:limit + 1]
            if include_columns and (needle := pattern.strip().lower()):
                snapshot = self._schema_cache.get(pid, scope, agent=True)
                self._add_column_hits(shared, snapshot, needle, narrow, found, limit)
        result: dict[str, Any] = {"objects": found[:limit], "truncated": len(found) > limit}
        if dropped := fit_lists(result, ("objects",), RESPONSE_CHARS - NOTE_ROOM):
            result["truncated"] = True
            result["notes"] = [_LEFT_OUT.format(f"{dropped['objects']} more objects",
                                                "narrow the pattern or pass a smaller limit")]
        self._done("search_objects", shared, started, len(result["objects"]), "objects", client,
                   f": {repr(pattern)[:300]}")
        return result

    @staticmethod
    def _add_column_hits(shared: Shared, snapshot: SchemaSnapshot, needle: str, narrow: str | None,
                         found: list[dict[str, Any]], limit: int) -> None:
        """Give each hit its columns that hold `needle` (at most COLUMNS_PER_HIT), and add the relations that only such a
        column matches while there is room (one past `limit`, which marks the result truncated)."""
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
                           "kind": relation.kind, "path": _path_json(path)}
                    found.append(hit)
                    by_name[(schema.name, relation.name)] = hit
                hit["columns"] = columns[:COLUMNS_PER_HIT]
                if len(columns) > COLUMNS_PER_HIT:
                    hit["more_columns"] = len(columns) - COLUMNS_PER_HIT

    def describe(self, connection: str, path: list[PathSegment] | None = None, database: str | None = None,
                 schema: str | None = None, name: str | None = None, include_ddl: bool = False, *,
                 client: str | None = None) -> dict[str, Any]:
        """One object: its columns, keys, foreign keys, indexes and comment; its DDL when asked. By `path`, or by name
        (`schema` and `database` narrow it): exact first, then ignoring case."""
        started = time.monotonic()
        shared = self.shared(connection, "describe")
        database = checked_database(database)
        snapshot: SchemaSnapshot | None = None
        with answered(shared.profile):
            self._login.ensure(shared.profile)
            if path:
                path, key = self._located(shared, path)
            elif name:
                path, snapshot, key = self._path_by_name(shared, database, schema, name)
            else:
                raise ToolFailure("invalid_argument", _NO_OBJECT)
            detail = self._catalog(shared, key, lambda c: c.object_detail(path))
            indexes = self._indexes(shared, key, path)
        notes: list[str] = []
        relation: SnapshotRelation | None = None
        if shared.profile.engine != Engine.COUCHBASE:
            try:
                if snapshot is None:
                    snapshot = self._schema_cache.get(shared.profile.id, key, agent=True)
                relation = _relation_of(snapshot, path)
            except Exception as exc:  # the detail stands without them
                notes.append(_NO_SNAPSHOT.format(getattr(exc, "message", type(exc).__name__)))
        result = self._described(detail, path, relation, indexes, include_ddl, notes)
        self._done("describe", shared, started, len(result["columns"]), "columns", client, f": {_path_text(path)}")
        return result

    @staticmethod
    def _described(detail: ObjectDetail, path: list[PathSegment], relation: SnapshotRelation | None,
                   indexes: list[dict[str, Any]], include_ddl: bool, notes: list[str]) -> dict[str, Any]:
        """describe's result, inside the response budget (M-1): comments and defaults cut as run_query cuts a cell;
        past the budget, the DDL keeps at most half of it and the lists lose their last items. `truncated` says so."""
        columns = detail.columns or []
        primary = relation.primary_key if relation is not None else [c.name for c in columns if c.is_primary_key]
        cut = False
        listed: list[dict[str, Any]] = []
        for column in columns:
            default, cut_default = cut_text(column.default)
            comment, cut_comment = cut_text(column.comment)
            cut = cut or cut_default or cut_comment
            listed.append({"name": column.name, "type": column.type_name, "nullable": column.nullable,
                           "default": default, "comment": comment})
        comment, cut_comment = cut_text(detail.comment)
        if cut or cut_comment:
            notes.append(_LONG_TEXT)
        result: dict[str, Any] = {
            "kind": detail.kind, "name": detail.name, "path": _path_json(path), "columns": listed,
            "primary_key": primary,
            "foreign_keys": [{"name": fk.name, "columns": fk.columns, "references": f"{fk.ref_schema}.{fk.ref_table}",
                              "ref_columns": fk.ref_columns} for fk in (relation.foreign_keys if relation else [])],
            "indexes": indexes, "comment": comment, "truncated": cut or cut_comment,
        }
        room = RESPONSE_CHARS - NOTE_ROOM
        cut_ddl = False
        if include_ddl:
            result["ddl"], cut_ddl = cut_text(detail.ddl, RESPONSE_CHARS // 2)
            result["ddl_is_generated"] = detail.ddl_is_generated
        dropped = fit_lists(result, ("columns", "indexes", "foreign_keys"), room)
        left_out = [f"{n} {what.replace('_', ' ')}" for what, n in dropped.items()]
        if include_ddl and (fit_text(result, "ddl", room) or cut_ddl):
            left_out.append("the end of the DDL")
        if left_out:
            result["truncated"] = True
            notes.append(_LEFT_OUT.format(", ".join(left_out), "describe it by its parts with list_objects"))
        if notes:
            result["notes"] = notes
        return result

    def _indexes(self, shared: Shared, key: str | None, path: list[PathSegment]) -> list[dict[str, Any]]:
        """The object's indexes: its `indexes` folder's nodes, or index nodes right under it (Couchbase)."""
        children = self._catalog(shared, key, lambda c: c.list_children(path))
        found = [child for child in children if child.kind == NodeKind.INDEX]
        if any(child.kind == NodeKind.FOLDER and child.name == Folder.INDEXES for child in children):
            folder = [*path, PathSegment(kind=NodeKind.FOLDER, name=Folder.INDEXES)]
            found += [child for child in self._catalog(shared, key, lambda c: c.list_children(folder))
                      if child.kind == NodeKind.INDEX]
        return [{"name": index.name, "detail": index.detail} for index in found]

    def _path_by_name(self, shared: Shared, database: str | None, schema: str | None,
                      name: str) -> tuple[list[PathSegment], SchemaSnapshot, str | None]:
        """The object named `name` in the snapshot (and the snapshot, and its catalog connection's key): exact, then
        ignoring case, in every part given; one, or `ambiguous`. Names never reach SQL here: the path they lead to goes
        through the catalog's own queries (bound or quoted)."""
        narrow = _narrowed_to(shared, database)
        key = self._database_key(shared, database)[1] if narrow is None else None
        snapshot = self._schema_cache.get(shared.profile.id, key, agent=True)
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
        return shared.adapter.relation_path(db, schema_name, relation, kind), snapshot, key

    def _login_of(self, profile: AnyProfile) -> dict[str, Any] | None:
        """CR-08-1: what the login may do beyond reading, once an agent's connection has read it (null before)."""
        privileges = self._login.peek(profile.id)
        if privileges is None:
            return None
        listed = privileges.model_dump(exclude_none="file" not in privileges.grants)  # FILE: NULL is no file access
        return {**listed, "summary": privileges.summary()}

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
