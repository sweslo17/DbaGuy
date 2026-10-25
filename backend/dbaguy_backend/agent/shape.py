"""How big one MCP tool result may be (spec ⑧ §3.6, CR-08-7), pure: the response budget every tool keeps, the helpers
the schema tools cut their lists and free text with (task 2 fix round 1, M-1), and what one query result may hold:
cells cut at `max_cell_chars`, rows kept while the response stays under RESPONSE_BYTES and `max_rows`, and the size of
the next page. Memory holds the kept rows plus one page, which the worker ends once its rows reach PAGE_RAW_CHARS of raw
values (they are fetched one at a time: ruling T6-I1), plus the row the worker peeks at.

The budget is UTF-8 bytes of the answer's JSON, not characters (final review I4): a character of Chinese is about a
token, so 40,000 characters of it were 27–40k tokens, past Claude Code's 25,000; 40,000 bytes are about 10–13k tokens in
any script. Cells, comments and defaults are still cut in characters (`max_cell_chars` is the agent's own unit)."""

import json
from typing import Any

from ..adapters.values import raw_chars
from ..domain.agent import PAGE_RAW_CHARS, READ_RAW_CHARS

MAX_ROWS_DEFAULT, MAX_ROWS = 100, 1_000
# run_query's default and largest max_cell_chars; the schema tools cut comments and defaults at the default too
MAX_CELL_DEFAULT, MAX_CELL = 1_000, 20_000
RESPONSE_BYTES = 40_000  # a whole answer's JSON in UTF-8: Claude Code warns past 10,000 tokens and stops at 25,000
NOTE_ROOM = 500  # bytes kept free for the note that says what was left out
RESPONSE_OVERHEAD = 1_000  # bytes for a query result's keys, counts and the truncation notes
__all__ = ["PAGE_RAW_CHARS", "READ_RAW_CHARS"]  # in domain/agent.py (final review M7); re-exported for the agent layer
PAGE_ROWS_MAX = 100
FIRST_PAGE = 1  # the first page is one row: its size sizes the next (as ⑥'s dump's FIRST_FETCH)
NOTES_MAX, NOTE_CHARS = 20, 500  # server messages (PG NOTICE, SQL Server PRINT)
ERROR_CHARS = 4_000  # a tool's error message: a server's text may quote a value of any size (review I4)


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def size(value: Any) -> int:
    """`compact(value)`'s size in UTF-8 bytes: what the budget counts (a lone surrogate as the 3 bytes it would
    take)."""
    return len(compact(value).encode("utf-8", "surrogatepass"))


def cut_text(text: str | None, limit: int = MAX_CELL_DEFAULT) -> tuple[str | None, bool]:
    """`text` cut at `limit` characters, and whether it was."""
    if text is None or len(text) <= limit:
        return text, False
    return text[:limit], True


def fit_text(result: dict[str, Any], key: str, room: int) -> bool:
    """Cut `result[key]` (a string) from the end until the result is at most `room` bytes; whether it was cut: the
    longest start of it whose JSON takes the room left (a binary search: its size grows with every character kept)."""
    text = result.get(key)
    over = size(result) - room
    if over <= 0 or not text:
        return False
    allowed = size(text) - over  # what the text's JSON (its quotes included) may take
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if size(text[:middle]) <= allowed:
            low = middle
        else:
            high = middle - 1
    result[key] = text[:low]
    return True


def fit_lists(result: dict[str, Any], keys: tuple[str, ...], room: int) -> dict[str, int]:
    """Drop items from the ends of the lists `result[key]`, from the largest list each time, until the result is at most
    `room` bytes (or the lists are empty); how many each lost, for those that lost any."""
    sizes = {key: size(result[key]) for key in keys}
    total = size(result)
    dropped = dict.fromkeys(keys, 0)
    while total > room:
        key = max((k for k in keys if result[k]), key=lambda k: sizes[k], default=None)
        if key is None:
            break
        items = result[key]
        removed = size(items.pop()) + (1 if items else 0)  # the item, and the comma before it
        sizes[key] -= removed
        total -= removed
        dropped[key] += 1
    return {key: n for key, n in dropped.items() if n}


def cap_cell(cell: Any, max_chars: int) -> tuple[Any, int]:
    """The cell as the agent gets it (the grid's encoding, text past `max_chars` as `{"$type": "text", preview, size,
    truncated}`, P4), and its raw size in characters (`values.raw_chars`: the worker's pages are measured alike)."""
    raw = raw_chars(cell)
    if isinstance(cell, str) and len(cell) > max_chars:
        return {"$type": "text", "preview": cell[:max_chars], "size": len(cell), "truncated": True}, raw
    if isinstance(cell, dict):
        kind = cell.get("$type")
        if kind == "text" and len(cell.get("preview", "")) > max_chars:  # cut at the grid's 64 KiB already
            return {**cell, "preview": cell["preview"][:max_chars]}, raw
        if kind == "json" and len(text := cell.get("text", "")) > max_chars:
            return {"$type": "text", "preview": text[:max_chars], "size": len(text), "truncated": True}, raw
    return cell, raw


class ResultBudget:
    """The rows of one result as they arrive, page by page. Columns whose list alone would take more than half the room
    are cut to the leading ones that fit in it (review I5: 50 tables side by side are 1,650 columns, more than the
    response holds), and each row with them: `columns` is what the answer holds, `columns_dropped` how many it lost."""

    def __init__(self, columns: list[dict[str, str | None]], max_rows: int, max_cell_chars: int,
                 budget_bytes: int = RESPONSE_BYTES) -> None:
        self.max_rows = max_rows
        self.max_cell_chars = max_cell_chars
        self.rows: list[list[Any]] = []
        self.dropped = 0  # rows read but left out: the response had no room for them
        self.largest_encoded = 1  # the largest row so far, as sent, in bytes (sizes the next page against the room)
        room = budget_bytes - RESPONSE_OVERHEAD
        self.columns = _leading(columns, room // 2) if size(columns) > room // 2 else columns
        self.columns_dropped = len(columns) - len(self.columns)
        self.remaining = room - size(self.columns)  # bytes
        self.raw_read = 0  # the kept rows' values, raw (and the cut columns' values read with them)

    @property
    def full(self) -> bool:
        """No more rows are wanted: `max_rows` kept, one did not fit, or READ_RAW_CHARS of values were read."""
        return len(self.rows) >= self.max_rows or self.dropped > 0 or self.remaining <= 0 or self.read_capped

    @property
    def read_capped(self) -> bool:
        return self.raw_read >= READ_RAW_CHARS

    def take(self, rows: list[list[Any]]) -> None:
        """Keep the rows that fit. A row is cut to the kept columns only when columns were left out (T8 re-review 1:
        an explain's later result set may be wider than its first)."""
        for row in rows:
            shown, cut = (row[:len(self.columns)], row[len(self.columns):]) if self.columns_dropped else (row, [])
            capped = [cap_cell(cell, self.max_cell_chars) for cell in shown]
            cells = [cell for cell, _ in capped]
            raw = (sum(chars for _, chars in capped) + sum(raw_chars(cell) for cell in cut)) or 1
            encoded = size(cells) + 1  # its comma
            self.largest_encoded = max(self.largest_encoded, encoded)
            if self.full or encoded > self.remaining:
                self.dropped += 1
                continue
            self.rows.append(cells)
            self.remaining -= encoded
            self.raw_read += raw

    def spend(self, taken: int) -> None:
        """Room (bytes) taken by something else in the response (a note)."""
        self.remaining -= taken

    def next_page(self) -> int:
        """How many rows to ask for: at most PAGE_ROWS_MAX, the rows still wanted, and the rows of the largest size so
        far the room left holds (at least 1). Only a count: the worker ends the page sooner once its rows reach
        PAGE_RAW_CHARS (ruling T6-I1 amends §3.6, which sized the page by the rows before it)."""
        by_room = self.remaining // self.largest_encoded
        return max(1, min(PAGE_ROWS_MAX, self.max_rows - len(self.rows), by_room))


def _leading(columns: list[dict[str, str | None]], room: int) -> list[dict[str, str | None]]:
    """The leading columns whose list fits in `room` bytes."""
    taken, kept = 2, 0  # the brackets
    for column in columns:
        taken += size(column) + 1
        if taken > room:
            break
        kept += 1
    return columns[:kept]


def note(text: str, limit: int = NOTE_CHARS) -> str:
    """A server message as a note (or, with `limit`, as an error's message: review I4): at most `limit` characters, the
    cut marked with an ellipsis."""
    return text if len(text) <= limit else text[:limit - 1] + "…"
