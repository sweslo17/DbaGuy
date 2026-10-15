from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from ..domain.execution import ColumnMeta, ErrorPosition, ServerMessage
from ..domain.metadata import Folder, NodeKind, ObjectDetail, ObjectNode, PathSegment, ServerInfo, child_path
from ..domain.profiles import AnyDraft, AnyProfile, Engine, EngineInfo
from ..domain.schema import RelationKind, SchemaSnapshot
from ..sql.dialects import Dialect


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    tls_server_name: str | None = None  # the real host name when reached through an SSH tunnel


@dataclass(frozen=True)
class AdapterCapabilities:
    per_database_connection: bool = False  # PostgreSQL: one connection can only see one database


@dataclass(frozen=True)
class ParsedUrl:
    profile: AnyDraft
    password: str | None = None


class StatementError(Exception):
    """A statement failed. Adapters translate driver errors into this."""

    def __init__(self, message: str, code: str | None = None, position: ErrorPosition | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.position = position


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
        """Server messages (NOTICE, PRINT…) received since the last call."""

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

    @abstractmethod
    def in_transaction(self) -> bool: ...

    def transaction_failed(self) -> bool:
        """True when the open transaction is aborted and only accepts ROLLBACK (PostgreSQL); False otherwise."""
        return False

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
            dialect=self.dialect,
        )

    def relation_path(self, database: str | None, schema: str, name: str, kind: RelationKind) -> list[PathSegment]:
        """The explorer path that opens a table/view, e.g. [database, schema, folder, <kind>:name]."""
        path: list[PathSegment] = []
        if self.capabilities.per_database_connection and database:
            path = child_path(path, NodeKind.DATABASE, database)
        path = child_path(path, NodeKind.SCHEMA, schema)
        path = child_path(path, NodeKind.FOLDER, self._FOLDER_FOR_KIND[kind])
        return child_path(path, kind, name)
