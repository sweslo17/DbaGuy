from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Literal

from ..domain.agent import LoginPrivileges
from ..domain.couchbase import FieldSample, Keyspace
from ..domain.data_jobs import DumpObjectKinds, DumpScope
from ..domain.edits import EditTarget
from ..domain.errors import ValidationFailed
from ..domain.execution import ColumnMeta, ErrorPosition, Presentation, QueryContext, ServerMessage, StatementParams
from ..domain.metadata import Folder, NodeKind, ObjectDetail, ObjectNode, PathSegment, ServerInfo, child_path
from ..domain.profiles import AnyDraft, AnyProfile, Engine, EngineInfo, ServerProfile
from ..domain.schema import RelationKind, SchemaSnapshot
from ..sql.dialects import Dialect
from .columns import TableColumn
from .dump import DumpCatalog, DumpSession
from .references import Reference
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
    # A paused result reads up to this many bytes of rows the server has already sent (`StatementResult.read_ahead`), so
    # the server is not held back while the user looks at a page; None: the engine does not read ahead (not in
    # EngineInfo)
    read_ahead_bytes: int | None = None
    row_editing: bool = False  # a table-data grid can be edited (spec ⑤): the connection is RowEditing
    # A table-data tab's first browse waits for the edit target and sorts by its `stable_order` (PostgreSQL, 05b K13)
    orders_browse_by_key: bool = False
    run_file: bool = False  # Run SQL file (spec ⑥ R-06-1, D-06-13)
    dump: bool = False  # a native SQL dump (spec ⑥ R-06-1): the connection is Dumping
    copy_data: bool = False  # copy rows between connections (spec ⑥ R-06-1, G3): the connection is DataTools


@dataclass(frozen=True)
class RunOptions:
    """Per-request options of `run()` (D-04-3). The worker checks them against the engine's capabilities, so an adapter
    never receives one it did not declare."""

    query_context: QueryContext | None = None  # needs capabilities.query_context
    params: StatementParams | None = None  # needs capabilities.parameters
    profile_plan: bool = False  # needs capabilities.executed_plan (04b)
    retry_safe: bool = False  # the statement was classified READ: an adapter may send it again after a stale connection
    # False: the statement returns no result set to show (a grid commit's own texts, spec ⑤ §3.5; a job's statements),
    # so an adapter may skip reading its result's column types first (SQL Server's describe: a round trip per statement,
    # 05a review M1), and reads no rows ahead of what `fetch()` asks for (the rows it peeks at for the types, SQLite's
    # and SQL Server's; PostgreSQL's cursor read-ahead): a dump sizes its reads by the rows' size (06a pre-flight M12)
    describe: bool = True


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
CONNECTION_LOST = "connection_lost"  # the link to the server is gone (a statement may or may not have run)
INVALID_PARAMS = "invalid_params"  # a parameter refused before sending; the message names it, never its value


class InvalidText(StatementError):
    """Text the driver could not decode (SQLite's bytes that are not UTF-8): the column the driver names, never the
    text itself (it would reach the screen and the log garbled). `in_catalog`: it was a catalog read's (the column is
    then the catalog's own, `name` or `sql`, so the message says the schema instead: fix round 2, N2)."""

    def __init__(self, column: str | None, *, in_catalog: bool = False) -> None:
        if in_catalog:
            message = "A name or definition in the database's schema holds text that is not valid UTF-8"
        else:
            message = f"{f'Column {column}' if column else 'A value'} holds text that is not valid UTF-8"
        super().__init__(message, code=INVALID_TEXT)
        self.column = None if in_catalog else column


def invalid_text_error(sql: str, index: int) -> StatementError:
    """The character at `index` cannot be sent: a lone surrogate (half of an emoji, e.g. from a broken paste) is not
    UTF-8, and drivers cut the text at U+0000 (so a filter after it would be lost)."""
    line, column = offset_to_line_col(sql, index + 1)
    return StatementError(
        f"The statement contains a character that is not valid text (U+{ord(sql[index]):04X}); delete it and "
        "retype it", code=INVALID_TEXT, position=ErrorPosition(line=line, column=column),
    )


class StatementCancelled(StatementError):
    """`completed`: the server finished the statement before the cancel could stop it (MySQL: a SLEEP() inside it
    returned early on the KILL QUERY and the statement went on to its end); a query tab still reports it cancelled, a
    run counts it as applied (06a ruling R8)."""

    def __init__(self, message: str = "Query cancelled", *, completed: bool = False) -> None:
        super().__init__(message, code="cancelled")
        self.completed = completed


class StatementNotSent(StatementError):
    """Nothing of the statement went out (raised where an adapter knows it): a check refused it before sending, or a
    request that had to go first on its connection failed. A write that raises it was not applied."""


class ReadAheadState(StrEnum):
    """Where one read-ahead step of a paused result left it (`StatementResult.read_ahead`)."""

    READING = "reading"  # took rows the server had sent; more may be on the socket
    # The step read nothing: nothing is on the socket now. Emptiness says nothing about the server's own stall clock (an
    # emptied socket does not mean the server's pending write completed, A23), so it neither starts nor ends a stall
    # (B42); only its `received` counts (enough bytes drained since the stall, e.g. by a Load more, ends it, B44).
    WAITING = "waiting"
    FULL = "full"  # the budget is used up: reading stopped, the server will be held back from here
    DONE = "done"  # the whole answer is in memory, or ended in an error kept for later: nothing can stall


@dataclass(frozen=True)
class ReadAhead:
    """One read-ahead step: its state, and the bytes of the server's answer read so far by any path (cumulative: rows
    taken by `fetch()` / `fetch_raw()` count too, B44)."""

    state: ReadAheadState
    received: int = 0


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
        """Up to n raw rows of the current result set. Only `[]` means the set is exhausted: fewer than n may come
        before an error later in the set (SQL Server raises it on the next call, Task 9 fix H1), so a reader that must
        know it read every row fetches until `[]`, or asks `next_result()`. An adapter that knows the set is over
        returns `[]` without a round trip."""

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

    def matched_rows(self) -> int | None:
        """How many rows the DML statement matched (spec ⑤ D-05-5), an UPDATE whose new values equal the old ones
        included. The default is affected_rows(); an engine whose count is rows changed overrides it (MySQL). None: not
        known (the apply then fails closed)."""
        return self.affected_rows()

    def presentation(self) -> Presentation:
        """How the client may show the current result set: "plan" for an EXPLAIN / ADVISE / profile result."""
        return "grid"

    def read_ahead(self, budget: int) -> ReadAhead:
        """Read on while the result is paused, so the server is not held back (engines with
        `capabilities.read_ahead_bytes`; the worker passes that as `budget`).

        Called on the worker thread only, and only while the result is paused (between Load mores). Must never block:
        it reads only rows the server has already sent, into memory, until the rows held take `budget` bytes. Rows
        read here come out of the next `fetch()` / `fetch_raw()` first, in order. A failure met while reading is kept
        and raised after the rows read before it, by those calls; never raised here.

        The default reads nothing and says FULL: the server is held back from the pause on."""
        return ReadAhead(ReadAheadState.FULL)


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

    def backslash_escapes(self) -> bool | None:
        """Whether the session reads a backslash in a string literal as an escape, as the answer to the last `run()`
        said (no round trip; a grid commit checks it after its BEGIN, ⑤ D-05-13). None when the engine's answers do not
        say (every engine but MySQL) or that answer did not (rows, an error)."""
        return None

    def login_privileges(self) -> LoginPrivileges | None:
        """What the login may do beyond reading that a read-only transaction does not stop (spec ⑧ CR-08-1): one query.
        None: the engine has nothing to tell (SQLite: the sidecar's own file access; Couchbase: its roles and
        `readonly=true`)."""
        return None

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


class FieldSampling(ABC):
    """A connection whose engine can read sample documents of a keyspace and tell their field paths (Couchbase)."""

    @abstractmethod
    def sample_fields(self, keyspace: Keyspace, size: int) -> FieldSample:
        """Read up to `size` documents of `keyspace` and derive their field paths (a manual action, R-04-11). Runs on
        the owning worker thread like every method; a server's error answer is raised as StatementError."""


class RowEditing(ABC):
    """A connection whose engine can tell how a table's rows are identified and written (capability `row_editing`,
    spec ⑤ §3.2). The profile's metadata connection answers it, on its worker thread like every method."""

    @abstractmethod
    def edit_target(self, path: list[PathSegment], *, order: bool = True) -> EditTarget:
        """A view or another non-table, or a table without a usable key: `editable=False` with the reason, never an
        error. NotFound when the table does not exist. `order` False: no browse of it follows (a copy's target, 06c),
        so its `stable_order` is not planned (PostgreSQL's EXPLAIN) and stays None."""


class DataTools(ABC):
    """A connection whose relations data tools read and write (spec ⑥ D-06-1: dump, copy). Every method runs on the
    owning worker thread, like the others."""

    @abstractmethod
    def table_columns(self, path: list[PathSegment]) -> list[TableColumn]:
        """The columns of the table, view or materialized view at `path`, in order; NotFound when it is gone."""


class ReferenceReading(ABC):
    """A connection that reads its foreign keys live with their ON DELETE action (PostgreSQL, SQL Server: a copy's
    target, whose order and DELETE they decide, 06c Task 2 fix round 4), on its worker thread like every method."""

    @abstractmethod
    def references(self, schema: str, names: list[str], *, rows: bool = False) -> list[Reference]:
        """The foreign keys from or into the tables `names` of `schema`, in one catalog query; with `rows`, whether
        each table that references one of them through a key that only refuses holds rows (`Reference.child_rows`: a
        copy that replaces their rows, 06c final review I-2)."""


class Dumping(ABC):
    """A connection whose engine DbaGuy dumps (spec ⑥ R-06-2, capability `dump`): what its dump session sends
    around the reads, and what the dump writes, in order. On the owning worker thread, like every method."""

    @abstractmethod
    def dump_session(self, scope: DumpScope) -> DumpSession:
        """The output settings and the snapshot (R-06-4) the dump's own session sends before it reads, and what ends
        them."""

    @abstractmethod
    def dump_catalog(self, scope: DumpScope, kinds: DumpObjectKinds, *, ddl: bool = True) -> DumpCatalog:
        """The objects and tables of `scope`, in the order the dump writes them; `ddl` False (a dump plan): which
        there are, without their definitions. NotFound when the scope's schema or a named relation is gone."""


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
        *,
        agent: bool = False,
    ) -> DatabaseConnection:
        """Open a connection. Raises ConnectionFailed. `agent`: an MCP agent's connection (spec ⑧ D-08-4), always on a
        read-only copy of the profile; an engine whose read-only session leaves a way to write locks it further."""

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
            row_editing=caps.row_editing, orders_browse_by_key=caps.orders_browse_by_key, run_file=caps.run_file,
            dump=caps.dump, copy_data=caps.copy_data,
        )

    def relation_path(self, database: str | None, schema: str, name: str, kind: RelationKind) -> list[PathSegment]:
        """The explorer path that opens a table/view, e.g. [database, schema, folder, <kind>:name]."""
        path: list[PathSegment] = []
        if self.capabilities.per_database_connection and database:
            path = child_path(path, NodeKind.DATABASE, database)
        path = child_path(path, NodeKind.SCHEMA, schema)
        path = child_path(path, NodeKind.FOLDER, self._FOLDER_FOR_KIND[kind])
        return child_path(path, kind, name)
