from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from ..domain.errors import ValidationFailed
from ..domain.execution import ColumnMeta, ErrorPosition, Presentation, QueryContext, ServerMessage, StatementParams
from ..domain.metadata import Folder, NodeKind, ObjectDetail, ObjectNode, PathSegment, ServerInfo, child_path
from ..domain.profiles import AnyDraft, AnyProfile, Engine, EngineInfo, ServerProfile
from ..domain.schema import RelationKind, SchemaSnapshot
from ..sql.dialects import Dialect
from .positions import offset_to_line_col


MessageListener = Callable[[ServerMessage], None]


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    tls_server_name: str | None = None  # the real host name when reached through an SSH tunnel


@dataclass(frozen=True)
class AdapterCapabilities:
    per_database_connection: bool = False  # PostgreSQL: one connection can only see one database
    # PostgreSQL: cancelling a statement aborts the transaction around it (the export-cancel warning)
    cancel_aborts_transaction: bool = False
    server_read_only: bool = True  # the database can enforce a read-only session; SQL Server cannot (03b)
    transactions: bool = True  # BEGIN / COMMIT / ROLLBACK and the auto-commit switch (Couchbase: none)
    query_context: bool = False  # run() takes RunOptions.query_context (Couchbase)
    parameters: bool = False  # run() takes RunOptions.params
    documents: bool = False  # the engine stores JSON documents by key
    executed_plan: bool = False  # run() takes RunOptions.profile_plan (04b)
    plan_format: Literal["couchbase"] | None = None  # how EXPLAIN / profile results are laid out
    # A paused result idle this long is closed by the worker (Couchbase: the server drops an idle stream); None: never
    paused_result_max_idle_s: float | None = None


@dataclass(frozen=True)
class RunOptions:
    """Per-request options of `run()` (D-04-3). The worker checks them against the engine's capabilities, so an adapter
    never receives one it did not declare."""

    query_context: QueryContext | None = None  # needs capabilities.query_context
    params: StatementParams | None = None  # needs capabilities.parameters
    profile_plan: bool = False  # needs capabilities.executed_plan (04b)
    retry_safe: bool = False  # the statement was classified READ: an adapter may send it again after a stale connection


@dataclass(frozen=True)
class ParsedUrl:
    profile: AnyDraft
    password: str | None = None


# Fixed on purpose, never urllib's own text: that quotes the authority, which holds the password (a fullwidth "＠"
# typed by an IME is enough), and the message reaches the API response and the log.
URL_ENCODING_HINT = "Percent-encode special characters in the user name and password."
MALFORMED_URL_MESSAGE = f"The host or port part of the URL is malformed. {URL_ENCODING_HINT}"


class StatementError(Exception):
    """A statement failed. Adapters translate driver errors into this."""

    def __init__(self, message: str, code: str | None = None, position: ErrorPosition | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.position = position


TRANSACTION_OPEN = "transaction_open"
# PostgreSQL answers COMMIT of an aborted transaction with a silent ROLLBACK: said by the Commit button and a typed one.
FAILED_COMMIT_MESSAGE = "The transaction had failed, so COMMIT rolled it back"


def transaction_open_error() -> StatementError:
    return StatementError("Commit or roll back the open transaction first", code=TRANSACTION_OPEN)


INVALID_TEXT = "invalid_text"


def invalid_text_error(sql: str, index: int) -> StatementError:
    """The character at `index` cannot be sent: a lone surrogate (half of an emoji, e.g. from a broken paste) is not
    UTF-8, and drivers cut the text at U+0000 (so a filter after it would be lost)."""
    line, column = offset_to_line_col(sql, index + 1)
    return StatementError(
        f"The statement contains a character that is not valid text (U+{ord(sql[index]):04X}); delete it and "
        "retype it", code=INVALID_TEXT, position=ErrorPosition(line=line, column=column),
    )


class StatementCancelled(StatementError):
    def __init__(self, message: str = "Query cancelled") -> None:
        super().__init__(message, code="cancelled")


class StatementResult(ABC):
    """Results of one executed statement or batch, possibly with several result sets.

    Rows are raw driver values (after the adapter's own driver-level loading, e.g. PostgreSQL's text-first loaders);
    the session worker turns them into JSON-safe cells with `values.encode_row` before they reach the client.
    """

    @abstractmethod
    def columns(self) -> list[ColumnMeta] | None:
        """Columns of the current result set; None when it returns no rows (DML/DDL)."""

    @abstractmethod
    def fetch(self, n: int) -> list[list[Any]]:
        """Up to n raw rows of the current result set; fewer (or none) means exhausted."""

    @abstractmethod
    def affected_rows(self) -> int | None: ...

    @abstractmethod
    def next_result(self) -> bool:
        """Advance to the next result set. False when there are no more."""

    @abstractmethod
    def take_messages(self) -> list[ServerMessage]:
        """Server messages (NOTICE, PRINT…) received since the last call. Messages the connection delivers to its
        message listener (`DatabaseConnection.set_message_listener`) are not returned here as well."""

    @abstractmethod
    def close(self) -> None:
        """Stop early: discard anything left. Must be idempotent.

        Must not change the transaction state: closing a partly read result inside the user's transaction leaves that
        transaction open and healthy (e.g. PostgreSQL closes a cursor instead of cancelling the statement).
        """

    def presentation(self) -> Presentation:
        """How the client may show the current result set: "plan" for an EXPLAIN / ADVISE / profile result."""
        return "grid"


class RawJsonRows(ABC):
    """A result whose rows are JSON documents as the server sent them (Couchbase): JSON / JSONL exports write them."""

    @abstractmethod
    def fetch_raw(self, n: int) -> list[str]:
        """Up to n rows of the current result set, each the server's JSON text; fewer (or none) means exhausted.
        Rows read here are not returned by `fetch()` as well."""


class DatabaseConnection(ABC):
    """All methods except cancel(), force_cancel() and abort() must be called on the owning worker thread."""

    @abstractmethod
    def server_info(self) -> ServerInfo: ...

    @abstractmethod
    def current_database(self) -> str | None: ...

    @abstractmethod
    def list_children(self, path: list[PathSegment]) -> list[ObjectNode]: ...

    @abstractmethod
    def object_detail(self, path: list[PathSegment]) -> ObjectDetail: ...

    @abstractmethod
    def schema_snapshot(self) -> SchemaSnapshot:
        """Schemas, relations, columns, PK/FK of the connection's current database.

        Built with a small fixed number of queries, never one query per table.
        """

    @abstractmethod
    def run(self, sql: str, options: RunOptions | None = None) -> StatementResult:
        """Execute one statement/batch. `options` holds only what the adapter's capabilities declare (the worker
        checks); adapters without such capabilities ignore it. Raises StatementError."""

    def set_message_listener(self, listener: MessageListener | None) -> None:
        """Deliver server messages (SQL Server PRINT, RAISERROR … WITH NOWAIT) to `listener` the moment they arrive,
        on the thread running the statement, instead of through `StatementResult.take_messages()`; None stops it.
        The default does nothing: such connections keep reporting messages through take_messages().

        While a listener is set, the adapter delivers each message live and `take_messages()` must not return the same
        messages. With no listener set, messages may be buffered for `take_messages()` or dropped, as the adapter
        decides; an adapter must not let messages from internal queries (commit/rollback, state refresh, metadata)
        reach a later request."""

    @abstractmethod
    def in_transaction(self) -> bool: ...

    def transaction_failed(self) -> bool:
        """True when the open transaction is aborted and only accepts ROLLBACK (PostgreSQL); False otherwise."""
        return False

    @abstractmethod
    def autocommit(self) -> bool: ...

    @abstractmethod
    def set_autocommit(self, enabled: bool) -> None:
        """Switch between auto-commit and manual commit. Raises StatementError(code="transaction_open") while a
        transaction is open; a no-op when the mode is already `enabled`."""

    @abstractmethod
    def commit(self) -> None:
        """Commit the open transaction; a no-op when none is open."""

    @abstractmethod
    def rollback(self) -> None:
        """Roll back the open transaction; a no-op when none is open."""

    def arm_cancel(self) -> None:
        """Called by the worker thread only, around one statement: right before its last cancel check, with no result
        open. A cancel that arrives from now until run() takes the statement over would find nothing running (the
        server ignores a cancel for an idle session, and an adapter must ignore one too, R-03a-P4), and the statement
        would then run to its end. So while armed, cancel() (and force_cancel() / abort()) must keep such a cancel for
        this statement, and run() must check on entry and raise StatementCancelled without sending anything. Once run()
        has taken over, a cancel works as before. The default does nothing."""

    def disarm_cancel(self) -> None:
        """Called by the worker thread only, once run() has returned or raised, or the request left before run(): the
        armed state ends and a cancel kept in it is dropped, so it can never stop a later statement. Idempotent. The
        default does nothing."""

    @abstractmethod
    def cancel(self) -> None:
        """Best-effort cancel of the running statement. Safe to call from any thread."""

    def force_cancel(self) -> None:
        """A repeated Cancel of the same running request (never a timeout): an adapter whose cancel can fail to end
        the statement may now drop the connection instead. Safe to call from any thread. Default: cancel()."""
        self.cancel()

    def abort(self) -> None:
        """The session is closing (its close guard has already asked about an open transaction): stop the running
        statement now, dropping the connection if need be. Safe to call from any thread. Default: cancel()."""
        self.cancel()

    @abstractmethod
    def close(self) -> None: ...


class DatabaseAdapter(ABC):
    engine: ClassVar[Engine]
    url_schemes: ClassVar[tuple[str, ...]]
    default_port: ClassVar[int | None]
    dialect: ClassVar[Dialect]
    capabilities: ClassVar[AdapterCapabilities] = AdapterCapabilities()

    _FOLDER_FOR_KIND: ClassVar[dict[str, str]] = {
        "table": Folder.TABLES, "view": Folder.VIEWS, "materialized_view": Folder.MATERIALIZED_VIEWS,
        "synonym": Folder.SYNONYMS,
    }

    @abstractmethod
    def parse_url(self, url: str) -> ParsedUrl: ...

    @abstractmethod
    def open(
        self,
        profile: AnyProfile,
        endpoint: Endpoint | None,
        password: str | None,
        database: str | None,
    ) -> DatabaseConnection:
        """Open a connection. Raises ConnectionFailed."""

    def endpoint_target(self, profile: ServerProfile) -> tuple[str, int]:
        """Where a connection to `profile` goes (directly, or as the SSH tunnel's target): the profile's host and port,
        or the engine's default port. An engine whose host field holds more (Couchbase: a seed list) picks one."""
        port = profile.port or self.default_port
        if port is None:
            raise ValidationFailed("A port is required")
        return profile.host, port

    def info(self) -> EngineInfo:
        caps = self.capabilities
        return EngineInfo(
            engine=self.engine, default_port=self.default_port, url_schemes=list(self.url_schemes),
            dialect=self.dialect, per_database_connection=caps.per_database_connection,
            cancel_aborts_transaction=caps.cancel_aborts_transaction, server_read_only=caps.server_read_only,
            transactions=caps.transactions, query_context=caps.query_context, parameters=caps.parameters,
            documents=caps.documents, executed_plan=caps.executed_plan, plan_format=caps.plan_format,
        )

    def relation_path(self, database: str | None, schema: str, name: str, kind: RelationKind) -> list[PathSegment]:
        """The explorer path that opens a table/view, e.g. [database, schema, folder, <kind>:name]."""
        path: list[PathSegment] = []
        if self.capabilities.per_database_connection and database:
            path = child_path(path, NodeKind.DATABASE, database)
        path = child_path(path, NodeKind.SCHEMA, schema)
        path = child_path(path, NodeKind.FOLDER, self._FOLDER_FOR_KIND[kind])
        return child_path(path, kind, name)
