"""What the Couchbase query workflow needs beyond a session: the contexts a tab can pick, plan analysis, index advice
on the metadata connection (B12) and the context of a History entry (B15)."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseConnection, StatementError
from ..adapters.couchbase.adapter import CouchbaseConnection, is_server_error
from ..adapters.couchbase.catalog import DEFAULT
from ..adapters.couchbase.explain import analyze, analyze_value
from ..adapters.registry import AdapterRegistry
from ..domain.couchbase import ContextOption, PlanNode, PlanView
from ..domain.errors import NotFound, QueryFailed, ValidationFailed
from ..domain.execution import QueryContext
from ..domain.history import HistoryPage
from .metadata_service import MetadataService
from .profile_service import ProfileService
from .schema_cache import SchemaCache

_NOT_COUCHBASE = "This is not a Couchbase connection"


@contextmanager
def server_errors_are_final() -> Iterator[None]:
    """Around a statement sent on a metadata connection: the server's own error answer (a numeric code) is raised as a
    DomainError (QueryFailed, with the server's text), so the metadata call does not reconnect and send the statement
    again. That resend is for a lost connection, and ADVISE on `app.users` panics the server (B39, A15)."""
    try:
        yield
    except StatementError as exc:
        if is_server_error(exc):
            raise QueryFailed(exc.message) from exc
        raise


def _nodes(view: PlanView) -> int:
    """How many operators the view holds (a count for the log: the plan's own text never goes there)."""
    pending: list[PlanNode] = [root for root in (view.root, *(s.root for s in view.subqueries)) if root is not None]
    count = 0
    while pending:
        node = pending.pop()
        count += 1
        pending.extend(node.children)
    return count


def _answered(view: PlanView) -> PlanView:
    """What the endpoint answers with, and the one log line about it (kind and counts, never the plan's content). A
    view whose text cannot be sent (an escaped lone surrogate, `"\\ud800"`, is valid JSON but not UTF-8) is `unknown`."""
    try:
        view.model_dump_json().encode("utf-8")
    except ValueError:  # PydanticSerializationError, UnicodeEncodeError
        logger.debug("Plan view of kind {} holds text that cannot be sent: answering unknown", view.kind)
        view = PlanView(kind="unknown")
    logger.debug("Plan view: {}, {} operators, {} findings, {} advice", view.kind, _nodes(view), len(view.findings),
                 len(view.advice))
    return view


class CouchbaseService:
    def __init__(self, registry: AdapterRegistry, profiles: ProfileService, schema_cache: SchemaCache,
                 metadata: MetadataService) -> None:
        self._registry = registry
        self._profiles = profiles
        self._schema_cache = schema_cache
        self._metadata = metadata

    def _takes_context(self, profile_id: UUID) -> bool:
        return self._registry.get(self._profiles.get(profile_id).engine).capabilities.query_context

    def require(self, profile_id: UUID) -> None:
        """Only a connection whose engine takes a query context (Couchbase) has these features."""
        if not self._takes_context(profile_id):
            raise ValidationFailed(_NOT_COUCHBASE)

    def contexts(self, profile_id: UUID, current: QueryContext | None) -> list[ContextOption]:
        """The picker's entries (§3.11.1): per bucket the bucket alone, its `_default` scope, its other scopes by name,
        system scopes last. `current` (the tab's own: from History, or the profile's default) comes first, marked
        `listed=False`, when the snapshot does not list it (B13); the frontend never quotes a name, `text` does."""
        self.require(profile_id)
        scopes: dict[str, dict[str, bool]] = {}  # bucket -> scope -> is a system scope
        for keyspace in self._schema_cache.get(profile_id).keyspaces:
            scopes.setdefault(keyspace.bucket, {})[keyspace.scope] = keyspace.system
        options: list[ContextOption] = []
        for bucket in sorted(scopes):
            options.append(_option(QueryContext(bucket=bucket)))
            for scope in sorted(scopes[bucket], key=lambda name: (scopes[bucket][name], name != DEFAULT, name)):
                options.append(_option(QueryContext(bucket=bucket, scope=scope)))
        if current is not None and not any((o.bucket, o.scope) == (current.bucket, current.scope) for o in options):
            options.insert(0, _option(current, listed=False))
        logger.debug("Query contexts of {}: {} entries", profile_id, len(options))
        return options

    def plan(self, text: str) -> PlanView:
        """The view of an EXPLAIN / ADVISE / profile text the user pasted: pure, no connection."""
        return _answered(analyze(text))

    def advise(self, profile_id: UUID, statement: str, context: QueryContext | None) -> PlanView:
        """`ADVISE <statement>` on the profile's metadata connection, read-only like every internal query, in `context`
        (B12). It does not run the statement, and it never touches a session's paused result."""
        self.require(profile_id)
        logger.debug("ADVISE on {}: {} characters", profile_id, len(statement))

        def run(connection: DatabaseConnection) -> list[Any]:
            if not isinstance(connection, CouchbaseConnection):
                raise ValidationFailed(_NOT_COUCHBASE)
            with server_errors_are_final():
                return connection.query("ADVISE " + statement, context=context)

        rows = self._metadata.with_connection(profile_id, None, run)
        return _answered(analyze_value(rows[0]) if rows else PlanView(kind="unknown"))

    def with_query_contexts(self, page: HistoryPage) -> HistoryPage:
        """The page with each entry's context read back from its `database` (B15), for the entries of existing profiles
        whose engine takes one; a malformed text gives None. Never stored: the history keeps only `database`."""
        takes_context: dict[UUID, bool] = {}
        items = []
        for entry in page.items:
            if entry.profile_id not in takes_context:
                try:
                    takes_context[entry.profile_id] = self._takes_context(entry.profile_id)
                except NotFound:  # the profile was deleted: its entries stay readable, without a context
                    takes_context[entry.profile_id] = False
            items.append(entry.model_copy(update={"query_context": _parsed(entry.database)})
                         if takes_context[entry.profile_id] else entry)
        return page.model_copy(update={"items": items})


def _option(context: QueryContext, listed: bool = True) -> ContextOption:
    return ContextOption(bucket=context.bucket, scope=context.scope, text=context.text(), listed=listed)


def _parsed(text: str | None) -> QueryContext | None:
    try:
        return QueryContext.parse(text)
    except ValueError:
        return None
