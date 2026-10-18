from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from ..domain.execution import ColumnMeta, ErrorPosition, ServerMessage
from ..domain.metadata import Folder, NodeKind, ObjectDetail, ObjectNode, PathSegment, ServerInfo, child_path
from ..domain.profiles import AnyDraft, AnyProfile, Engine, EngineInfo
from ..domain.schema import RelationKind, SchemaSnapshot
from ..sql.dialects import Dialect


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


class DatabaseConnection(ABC):
    """All methods except cancel() must be called on the owning worker thread."""

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
    def run(self, sql: str) -> StatementResult:
        """Execute one statement/batch (no parameters). Raises StatementError."""

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

    @abstractmethod
    def cancel(self) -> None:
        """Best-effort cancel of the running statement. Safe to call from any thread."""

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

    def info(self) -> EngineInfo:
        return EngineInfo(
            engine=self.engine, default_port=self.default_port, url_schemes=list(self.url_schemes),
            dialect=self.dialect, per_database_connection=self.capabilities.per_database_connection,
            cancel_aborts_transaction=self.capabilities.cancel_aborts_transaction,
            server_read_only=self.capabilities.server_read_only,
        )

    def relation_path(self, database: str | None, schema: str, name: str, kind: RelationKind) -> list[PathSegment]:
        """The explorer path that opens a table/view, e.g. [database, schema, folder, <kind>:name]."""
        path: list[PathSegment] = []
        if self.capabilities.per_database_connection and database:
            path = child_path(path, NodeKind.DATABASE, database)
        path = child_path(path, NodeKind.SCHEMA, schema)
        path = child_path(path, NodeKind.FOLDER, self._FOLDER_FOR_KIND[kind])
        return child_path(path, kind, name)
