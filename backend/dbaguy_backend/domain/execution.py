from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .classification import Classification, GuardReason
from .column_category import ColumnCategory
from .data_jobs import DumpContent, DumpObjectKinds, DumpScope, JobCommand, JobOutcomeEvent, JobProgressEvent
from .formats import FormatKind, RowFormatOptions

Presentation = Literal["grid", "plan"]  # "plan": an EXPLAIN / ADVISE / profile result the client may show as a plan


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
    column: int  # 1-based, in UTF-16 code units (the editor's unit)


_DEFAULT_NAMESPACE = "default:"


def _context_part(name: str, *, quote_dots: bool) -> str:
    """A name as context text: back-ticked when plain text would not read back as this name — it holds a back-tick, a
    dot where a dot would be read as the split, ends in a dot (`a.` before a scope would read `a..s`), has surrounding
    spaces (`parse` strips the text) or starts like the `default:` namespace. Quoted as `_path_parts` reads it, which is
    SQL++'s `force_quote_ident` (back-ticks and backslashes doubled); the domain cannot import `sql/`."""
    if (
        "`" in name or (quote_dots and "." in name) or name.endswith(".") or name != name.strip()
        or name[:len(_DEFAULT_NAMESPACE)].lower() == _DEFAULT_NAMESPACE
    ):
        return "`" + name.replace("\\", "\\\\").replace("`", "``") + "`"
    return name


def _path_parts(text: str) -> list[str]:
    """`a.b`, `` `a.b` ``, `` `a`.`b` ``: SQL++ path parts, back-ticks with `` and \\ escapes."""
    parts: list[str] = []
    i, n = 0, len(text)
    while True:
        if i < n and text[i] == "`":
            name: list[str] = []
            i += 1
            while True:
                if i >= n:
                    raise ValueError("unclosed back-tick")
                if text[i] == "\\" and i + 1 < n:
                    name.append(text[i + 1])
                    i += 2
                elif text[i] == "`" and text[i + 1:i + 2] == "`":
                    name.append("`")
                    i += 2
                elif text[i] == "`":
                    i += 1
                    break
                else:
                    name.append(text[i])
                    i += 1
            parts.append("".join(name))
        else:
            end = text.find(".", i)
            end = n if end < 0 else end
            if "`" in text[i:end]:
                raise ValueError("a back-tick inside a name")
            parts.append(text[i:end])
            i = end
        if i == n:
            return parts
        if text[i] != ".":
            raise ValueError("expected a dot")
        i += 1


class QueryContext(BaseModel):
    """Couchbase: the bucket (and scope) that unqualified keyspace names resolve against, sent with every request
    (D-04-5: a structure, because a bucket name may hold a dot)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket: str = Field(min_length=1, max_length=100)
    scope: str | None = Field(default=None, min_length=1, max_length=251)

    def text(self) -> str:
        """`travel-sample.inventory`, `dbaguy`, `` `my.bucket` `` (a bucket alone whose name holds a dot), `my.bucket.app`
        (with a scope the last dot splits). What the history keeps; `parse` reads it back."""
        if self.scope is None:
            return _context_part(self.bucket, quote_dots=True)
        scope = _context_part(self.scope, quote_dots=True)
        plain = f"{_context_part(self.bucket, quote_dots=False)}.{scope}"
        if "`" not in plain:
            return plain  # read back by splitting at the last dot
        # read back as a SQL++ path, where every dot outside back-ticks splits: the bucket's dots are quoted too
        return f"{_context_part(self.bucket, quote_dots=True)}.{scope}"

    @classmethod
    def parse(cls, text: str | None) -> "QueryContext | None":
        """A profile's database field or a history entry: `bucket`, `bucket.scope` (split at the last dot), with an
        optional `default:` in front, or SQL++ back-ticked parts. None for no text; ValueError when malformed."""
        text = (text or "").strip()
        if text[:len(_DEFAULT_NAMESPACE)].lower() == _DEFAULT_NAMESPACE:
            text = text[len(_DEFAULT_NAMESPACE):]
        if not text:
            return None
        if "`" in text:
            parts = _path_parts(text)
        else:
            parts = text.rsplit(".", 1) if "." in text else [text]
        # an empty part, or a bucket ending in a dot (`a..b` splits into `a.` and `b`), is malformed
        if not 1 <= len(parts) <= 2 or not all(parts) or ("`" not in text and parts[0].endswith(".")):
            raise ValueError("not a bucket or bucket.scope")
        return cls(bucket=parts[0], scope=parts[1] if len(parts) == 2 else None)  # pydantic's ValidationError is a ValueError


# A parameter's name, without its `$` / `@`: what the worker accepts (sessions/options) and what a tab's saved drafts
# are keyed by (domain/workspace). Defined here once.
PARAM_NAME = r"[A-Za-z_][A-Za-z0-9_]*"


class StatementParams(BaseModel):
    """Typed parameters of one statement (R-10): JSON literal texts, sent unchanged. Content is checked by the worker
    (`invalid_params`), only the shape here."""

    model_config = ConfigDict(extra="forbid")

    named: dict[str, str] = Field(default_factory=dict, max_length=200)  # name without $ / @ → JSON literal text
    positional: list[str] = Field(default_factory=list, max_length=200)  # $1, $2 … / ? in order


# ---- client -> server ----

class ExecuteCommand(BaseModel):
    type: Literal["execute"] = "execute"
    request_id: str
    sql: str
    page_size: int = Field(default=500, ge=1, le=10_000)
    confirmed: bool = False  # the user confirmed this dangerous statement; checked again on every execute
    timeout_s: float | None = Field(default=None, ge=0, le=86_400)  # covers run() and the first page; None/0 = none
    history: bool = True  # False for queries the app runs on the user's behalf (e.g. table browsing)
    query_context: QueryContext | None = None  # None: the request carries no context (ruling P1)
    params: StatementParams | None = None
    profile_plan: bool = False  # also return the executed plan (needs the engine's `executed_plan`)


class ExportCommand(BaseModel):
    """Run `sql` again on this session and write its whole result, as raw values, to the file at `path`."""

    type: Literal["export"] = "export"
    request_id: str
    sql: str
    format: FormatKind
    path: str  # absolute, in an existing folder; written through a temporary file, so a failed export keeps any old file
    options: RowFormatOptions = RowFormatOptions()
    query_context: QueryContext | None = None  # an export runs the statement again: the same context and parameters
    params: StatementParams | None = None


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


class ApplyEditsCommand(BaseModel):
    """Apply a previewed grid-edit plan (spec ⑤ §3.5): its statements exactly as previewed, in one transaction on this
    session. A Cancel stops it until its COMMIT has been sent."""

    type: Literal["apply_edits"] = "apply_edits"
    request_id: str
    plan_id: str = Field(min_length=1, max_length=64)


class RunFileCommand(JobCommand):
    """Run a SQL file on this session, a statement at a time (spec ⑥ §3.4): all of it in one transaction, or each
    statement on its own (stopping at an error, or skipping it). Statements before `from_statement` are read, never
    sent."""

    job_kind = "run_file"
    type: Literal["run_file"] = "run_file"
    path: str = Field(min_length=1, max_length=4096)  # absolute
    mode: Literal["all_or_nothing", "statements"]
    on_error: Literal["stop", "continue"] = "stop"  # continue: statement by statement only (R-06-7)
    from_statement: int = Field(default=1, ge=1)
    search_path: str | None = Field(default=None, min_length=1, max_length=1024)  # PostgreSQL: one schema
    query_context: QueryContext | None = None  # Couchbase

    @model_validator(mode="after")
    def _continue_only_statement_by_statement(self) -> Self:
        if self.mode == "all_or_nothing" and self.on_error == "continue":
            raise ValueError("an all-or-nothing run stops at the first error: skip errors statement by statement")
        return self


class DumpCommand(JobCommand):
    """Dump a scope of this session's database to a SQL file (spec ⑥ §3.3): written through a temporary file, so a
    dump that fails or is cancelled leaves no file and keeps any old one."""

    job_kind = "dump"
    type: Literal["dump"] = "dump"
    path: str = Field(min_length=1, max_length=4096)  # absolute, in an existing folder
    scope: DumpScope
    content: DumpContent = "both"
    objects: DumpObjectKinds = DumpObjectKinds()
    drop_first: bool = False  # a DROP … IF EXISTS before each object it creates (the header says so)
    filter: str | None = Field(default=None, max_length=65_536)  # one table only: the table-data tab's filter

    @model_validator(mode="after")
    def _a_filter_for_one_table_only(self) -> Self:
        if self.filter is not None and (self.scope.kind != "tables" or len(self.scope.names) != 1):
            raise ValueError("a filter applies to a dump of one table")
        return self


ClientCommand = Annotated[
    ExecuteCommand | ExportCommand | FetchMoreCommand | CancelCommand | DiscardCommand | CommitCommand
    | RollbackCommand | SetAutocommitCommand | ApplyEditsCommand | RunFileCommand | DumpCommand,
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
    presentation: Presentation = "grid"


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
    reasons: list[GuardReason]
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


EditOutcome = Literal["committed", "rolled_back", "partial", "unknown"]


class EditOutcomeEvent(BaseModel):
    """How an apply ended (spec ⑤ R-05-6): sent once, right before the request's `session_state` and `finished`."""

    type: Literal["edit_outcome"] = "edit_outcome"
    request_id: str
    outcome: EditOutcome
    applied: list[str] = []  # change ids that took effect: all when committed; those before the failure when partial
    already_gone: list[str] = []  # DELETEs that matched no row: the row was gone already (R-05-5)
    failed_change: str | None = None
    statement: int | None = None  # the failing statement's index in the plan's statements (0-based)


ServerEvent = Annotated[
    StartedEvent | ResultSetEvent | RowsEvent | ResultSetEndEvent | AffectedEvent | MessageEvent | ErrorEvent
    | ProgressEvent | ConfirmationRequiredEvent | SessionStateEvent | FinishedEvent | EditOutcomeEvent
    | JobProgressEvent | JobOutcomeEvent,
    Field(discriminator="type"),
]


class ExecutionSchema(BaseModel):
    """Exposes the WebSocket protocol in OpenAPI so the frontend types are generated."""

    command: ClientCommand
    event: ServerEvent
