"""How big one MCP tool result may be (spec ⑧ §3.6), pure: the response budget every tool keeps, and the helpers the
schema tools cut their lists and free text with (task 2 fix round 1, M-1). The query tools' pages and cells join them
here (Task 6)."""

import json
from typing import Any

MAX_CELL_DEFAULT = 1_000  # run_query's default max_cell_chars; the schema tools cut comments and defaults at it too
RESPONSE_CHARS = 40_000  # Claude Code warns past 10,000 tokens; about 4 characters a token
NOTE_ROOM = 500  # kept free for the notes that say what was left out


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
