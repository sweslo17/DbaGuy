from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from .execution import QueryContext

HistoryStatus = Literal["ok", "error", "cancelled", "timeout"]


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


class HistoryEntry(HistoryRecord):
    id: int
    # Engines with a per-request query context (Couchbase): `database` parsed back. Filled when the history is read,
    # never stored (B15: a tab reopens from this, not from `database`).
    query_context: QueryContext | None = None


class HistoryPage(BaseModel):
    items: list[HistoryEntry]  # newest first
    next_before: int | None  # pass it as `before` to get the next page; None on the last one
