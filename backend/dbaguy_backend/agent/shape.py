"""How big one MCP tool result may be (spec ⑧ §3.6, CR-08-7), pure: the response budget every tool keeps, the helpers
the schema tools cut their lists and free text with (task 2 fix round 1, M-1), and what one query result may hold:
cells cut at `max_cell_chars`, rows kept while the response stays under RESPONSE_CHARS and `max_rows`, and the size of
the next page, so that memory never holds more than the kept rows plus one page of the driver's rows (plus the row the
worker peeks at)."""

import json
from typing import Any

MAX_ROWS_DEFAULT, MAX_ROWS = 100, 1_000
# run_query's default and largest max_cell_chars; the schema tools cut comments and defaults at the default too
MAX_CELL_DEFAULT, MAX_CELL = 1_000, 20_000
RESPONSE_CHARS = 40_000  # Claude Code warns past 10,000 tokens; about 4 characters a token
NOTE_ROOM = 500  # kept free for the notes that say what was left out
RESPONSE_OVERHEAD = 1_000  # a query result's keys, counts and the truncation note
PAGE_RAW_CHARS = 8 * 1024 * 1024  # one page of the driver's rows: about this many characters, raw
# Every value one call reads, raw: past it, no more rows are asked for. A few huge values end the call (one 100 MB
# value: that row alone), instead of dozens of them being read for their 1,000-character previews (P10)
READ_RAW_CHARS = 64 * 1024 * 1024
PAGE_ROWS_MAX = 100
FIRST_PAGE = 1  # the first page is one row: its size sizes the next (as ⑥'s dump's FIRST_FETCH)
NOTES_MAX, NOTE_CHARS = 20, 500  # server messages (PG NOTICE, SQL Server PRINT)


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def cut_text(text: str | None, limit: int = MAX_CELL_DEFAULT) -> tuple[str | None, bool]:
    """`text` cut at `limit` characters, and whether it was."""
    if text is None or len(text) <= limit:
        return text, False
    return text[:limit], True


def fit_text(result: dict[str, Any], key: str, room: int) -> bool:
    """Cut `result[key]` (a string) from the end until the result is at most `room` characters; whether it was cut.
    Each character cut takes at least one off the JSON, so one cut is enough."""
    text = result.get(key)
    over = len(compact(result)) - room
    if over <= 0 or not text:
        return False
    result[key] = text[: max(0, len(text) - over)]
    return True


def fit_lists(result: dict[str, Any], keys: tuple[str, ...], room: int) -> dict[str, int]:
    """Drop items from the ends of the lists `result[key]`, from the largest list each time, until the result is at most
    `room` characters (or the lists are empty); how many each lost, for those that lost any."""
    sizes = {key: len(compact(result[key])) for key in keys}
    total = len(compact(result))
    dropped = dict.fromkeys(keys, 0)
    while total > room:
        key = max((k for k in keys if result[k]), key=lambda k: sizes[k], default=None)
        if key is None:
            break
        items = result[key]
        removed = len(compact(items.pop())) + (1 if items else 0)  # the item, and the comma before it
        sizes[key] -= removed
        total -= removed
        dropped[key] += 1
    return {key: n for key, n in dropped.items() if n}


def cap_cell(cell: Any, max_chars: int) -> tuple[Any, int]:
    """The cell as the agent gets it (the grid's encoding, text past `max_chars` as `{"$type": "text", preview, size,
    truncated}`, P4), and its raw size in characters (`size` where the encoding has one, else its encoded length)."""
    if isinstance(cell, str):
        if len(cell) > max_chars:
            return {"$type": "text", "preview": cell[:max_chars], "size": len(cell), "truncated": True}, len(cell)
        return cell, len(cell)
    if isinstance(cell, dict):
        kind, size = cell.get("$type"), cell.get("size")
        if kind == "text" and len(cell.get("preview", "")) > max_chars:  # cut at the grid's 64 KiB already
            return {**cell, "preview": cell["preview"][:max_chars]}, int(size or 0)
        if kind == "json" and len(cell.get("text", "")) > max_chars:
            text = cell["text"]
            return {"$type": "text", "preview": text[:max_chars], "size": len(text), "truncated": True}, len(text)
        if isinstance(size, int):  # bytes, a truncated text
            return cell, size
    return cell, len(compact(cell))


class ResultBudget:
    """The rows of one result as they arrive, page by page."""

    def __init__(self, columns: list[dict[str, str | None]], max_rows: int, max_cell_chars: int,
                 budget_chars: int = RESPONSE_CHARS) -> None:
        self.max_rows = max_rows
        self.max_cell_chars = max_cell_chars
        self.rows: list[list[Any]] = []
        self.dropped = 0  # rows read but left out: the response had no room for them
        self.largest_raw = 1  # the largest row so far, raw (sizes the next page's driver read)
        self.largest_encoded = 1  # the largest row so far, as sent (sizes it against the room left)
        self.remaining = budget_chars - RESPONSE_OVERHEAD - len(compact(columns))
        self.raw_read = 0  # the kept rows' values, raw

    @property
    def full(self) -> bool:
        """No more rows are wanted: `max_rows` kept, one did not fit, or READ_RAW_CHARS of values were read."""
        return len(self.rows) >= self.max_rows or self.dropped > 0 or self.remaining <= 0 or self.read_capped

    @property
    def read_capped(self) -> bool:
        return self.raw_read >= READ_RAW_CHARS

    def take(self, rows: list[list[Any]]) -> None:
        for row in rows:
            capped = [cap_cell(cell, self.max_cell_chars) for cell in row]
            cells = [cell for cell, _ in capped]
            raw = sum(size for _, size in capped) or 1
            encoded = len(compact(cells)) + 1  # its comma
            self.largest_raw = max(self.largest_raw, raw)
            self.largest_encoded = max(self.largest_encoded, encoded)
            if self.full or encoded > self.remaining:
                self.dropped += 1
                continue
            self.rows.append(cells)
            self.remaining -= encoded
            self.raw_read += raw

    def spend(self, chars: int) -> None:
        """Room taken by something else in the response (a note)."""
        self.remaining -= chars

    def next_page(self) -> int:
        """§3.6: PAGE_RAW_CHARS over the largest row so far, between 1 and PAGE_ROWS_MAX, and no more than the rows
        still wanted or than the room left holds rows of the largest size (at least 1)."""
        by_raw = PAGE_RAW_CHARS // self.largest_raw
        by_room = self.remaining // self.largest_encoded
        return max(1, min(PAGE_ROWS_MAX, by_raw, self.max_rows - len(self.rows), by_room))


def note(text: str) -> str:
    """A server message as a note: at most NOTE_CHARS characters, the cut marked with an ellipsis."""
    return text if len(text) <= NOTE_CHARS else text[:NOTE_CHARS - 1] + "…"
