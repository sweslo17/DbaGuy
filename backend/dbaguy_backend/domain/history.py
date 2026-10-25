from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from .execution import QueryContext

# running: a Run SQL file or a copy that started and has not ended (D-06-9); interrupted: one that was still running
# when the backend ended (set at the next start); refused: an MCP agent's statement the gate turned away (spec ⑧ OP-7)
HistoryStatus = Literal["ok", "error", "cancelled", "timeout", "running", "interrupted", "refused"]
# Who ran it: the user in DbaGuy, or an AI tool through the MCP server (spec ⑧ D-08-13)
HistorySource = Literal["app", "mcp"]


class HistoryRecord(BaseModel):
    """One statement that was executed, as it is written to the history."""

    profile_id: UUID
    profile_name: str  # as it was when the session opened: the history stays readable after a rename or a delete
    database: str | None
    sql: str
    started_at: datetime
    elapsed_ms: int
    rows: int
    status: HistoryStatus
    error: str | None = None
    source: HistorySource = "app"
    client: str | None = None  # mcp: the name the client gave for itself (self-reported, D-08-7)
    repeats: int = 1  # a refused statement the same client sent again within a minute (CR-08-3)


class HistoryEntry(HistoryRecord):
    id: int
    # Engines with a per-request query context (Couchbase): `database` parsed back. Filled when the history is read,
    # never stored (B15: a tab reopens from this, not from `database`).
    query_context: QueryContext | None = None


class HistoryPage(BaseModel):
    items: list[HistoryEntry]  # newest first
    next_before: int | None  # pass it as `before` to get the next page; None on the last one
