from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from ..sql.classifier import Classification
from .column_category import ColumnCategory
from .formats import FormatKind, RowFormatOptions


class ColumnMeta(BaseModel):
    name: str
    type_name: str
    category: ColumnCategory


class MessageSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"


class ServerMessage(BaseModel):
    severity: MessageSeverity
    text: str


class ErrorPosition(BaseModel):
    line: int  # 1-based, relative to the executed statement
    column: int  # 1-based


# ---- client -> server ----

class ExecuteCommand(BaseModel):
    type: Literal["execute"] = "execute"
    request_id: str
    sql: str
    page_size: int = Field(default=500, ge=1, le=10_000)
    confirmed: bool = False  # the user confirmed this dangerous statement; checked again on every execute
    timeout_s: float | None = Field(default=None, ge=0, le=86_400)  # covers run() and the first page; None/0 = none
    history: bool = True  # False for queries the app runs on the user's behalf (e.g. table browsing)


class ExportCommand(BaseModel):
    """Run `sql` again on this session and write its whole result, as raw values, to the file at `path`."""

    type: Literal["export"] = "export"
    request_id: str
    sql: str
    format: FormatKind
    path: str  # absolute, in an existing folder; written as `<path>.part` first, so a failed export keeps any old file
    options: RowFormatOptions = RowFormatOptions()


class FetchMoreCommand(BaseModel):
    type: Literal["fetch_more"] = "fetch_more"
    request_id: str
    count: int = Field(default=500, ge=1, le=10_000)


class CancelCommand(BaseModel):
    type: Literal["cancel"] = "cancel"
    request_id: str


class DiscardCommand(BaseModel):
    type: Literal["discard"] = "discard"
    request_id: str


class CommitCommand(BaseModel):
    type: Literal["commit"] = "commit"
    request_id: str


class RollbackCommand(BaseModel):
    type: Literal["rollback"] = "rollback"
    request_id: str


class SetAutocommitCommand(BaseModel):
    type: Literal["set_autocommit"] = "set_autocommit"
    request_id: str
    enabled: bool


ClientCommand = Annotated[
    ExecuteCommand | ExportCommand | FetchMoreCommand | CancelCommand | DiscardCommand | CommitCommand
    | RollbackCommand | SetAutocommitCommand,
    Field(discriminator="type"),
]


# ---- server -> client ----

class StartedEvent(BaseModel):
    type: Literal["started"] = "started"
    request_id: str


class ResultSetEvent(BaseModel):
    type: Literal["result_set"] = "result_set"
    request_id: str
    index: int
    columns: list[ColumnMeta]


class RowsEvent(BaseModel):
    type: Literal["rows"] = "rows"
    request_id: str
    index: int
    rows: list[list[Any]]


class ResultSetEndEvent(BaseModel):
    type: Literal["result_set_end"] = "result_set_end"
    request_id: str
    index: int
    row_count: int  # rows sent so far for this result set
    has_more: bool  # True: paused; send fetch_more or discard


class AffectedEvent(BaseModel):
    type: Literal["affected"] = "affected"
    request_id: str
    index: int
    rows: int


class MessageEvent(BaseModel):
    type: Literal["message"] = "message"
    request_id: str  # "" for session-level messages (e.g. an idle transaction)
    severity: MessageSeverity
    text: str


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    request_id: str
    message: str
    code: str | None = None
    position: ErrorPosition | None = None


class ProgressEvent(BaseModel):
    """An export has written `rows` rows so far; sent after every 10,000 rows."""

    type: Literal["progress"] = "progress"
    request_id: str
    rows: int


class ConfirmationRequiredEvent(BaseModel):
    """The statement was not run: resend it with `confirmed=true` once the user agreed. `finished(skipped)` follows."""

    type: Literal["confirmation_required"] = "confirmation_required"
    request_id: str
    reasons: list[str]
    classification: Classification


class SessionStateEvent(BaseModel):
    """The session's transaction state: sent right before every `finished` and every pause, on attach and after an
    idle rollback."""

    type: Literal["session_state"] = "session_state"
    autocommit: bool
    in_transaction: bool
    transaction_failed: bool
    transaction_started_at: datetime | None = None
    idle_seconds: float | None = None  # only on attach: how long the open transaction has been idle


class FinishedEvent(BaseModel):
    type: Literal["finished"] = "finished"
    request_id: str
    elapsed_ms: int
    in_transaction: bool
    transaction_failed: bool = False  # the open transaction is aborted: only ROLLBACK is accepted
    cancelled: bool = False
    timed_out: bool = False  # cancelled because the request's timeout expired
    skipped: bool = False  # never ran: it needs the user's confirmation first


ServerEvent = Annotated[
    StartedEvent | ResultSetEvent | RowsEvent | ResultSetEndEvent | AffectedEvent | MessageEvent | ErrorEvent
    | ProgressEvent | ConfirmationRequiredEvent | SessionStateEvent | FinishedEvent,
    Field(discriminator="type"),
]


class ExecutionSchema(BaseModel):
    """Exposes the WebSocket protocol in OpenAPI so the frontend types are generated."""

    command: ClientCommand
    event: ServerEvent
