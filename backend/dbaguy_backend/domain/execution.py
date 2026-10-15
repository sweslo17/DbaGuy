from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class ColumnCategory(StrEnum):
    NUMBER = "number"
    TEXT = "text"
    BOOL = "bool"
    DATETIME = "datetime"
    BINARY = "binary"
    JSON = "json"
    OTHER = "other"


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


ClientCommand = Annotated[
    ExecuteCommand | FetchMoreCommand | CancelCommand | DiscardCommand,
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
    request_id: str
    severity: MessageSeverity
    text: str


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    request_id: str
    message: str
    code: str | None = None
    position: ErrorPosition | None = None


class FinishedEvent(BaseModel):
    type: Literal["finished"] = "finished"
    request_id: str
    elapsed_ms: int
    in_transaction: bool
    transaction_failed: bool = False  # the open transaction is aborted: only ROLLBACK is accepted
    cancelled: bool = False


ServerEvent = Annotated[
    StartedEvent | ResultSetEvent | RowsEvent | ResultSetEndEvent | AffectedEvent | MessageEvent | ErrorEvent | FinishedEvent,
    Field(discriminator="type"),
]


class ExecutionSchema(BaseModel):
    """Exposes the WebSocket protocol in OpenAPI so the frontend types are generated."""

    command: ClientCommand
    event: ServerEvent
