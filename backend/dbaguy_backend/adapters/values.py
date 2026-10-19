import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, final
from uuid import UUID

MAX_SAFE_INT = 2**53 - 1
TEXT_LIMIT = 64 * 1024  # the longest text or JSON cell sent whole; a longer one goes as a preview
PLAN_TEXT_LIMIT = 4 * 1024 * 1024  # the same for a cell of a `plan` set (EXPLAIN, ADVISE, a profile): drawn whole
BYTES_PREVIEW = 64


@dataclass(frozen=True, slots=True)
class JsonText:
    """A JSON value kept as the server's text (Couchbase): an object or array, or a number no Python number holds
    exactly (`1.10`, `1e400`, an integer past ±(2^53−1)). The text is never re-encoded (spec §3.2.4)."""

    text: str


@final
class MissingType:
    """The row has no such key (SQL++ MISSING), which is not NULL (R-04-8). A singleton: `MissingType()`, a copy, a
    deep copy and an unpickled one are all the `MISSING` instance, so `value is MISSING` always holds."""

    __slots__ = ()
    _instance: "MissingType | None" = None

    def __new__(cls) -> "MissingType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __reduce__(self) -> str:
        return "MISSING"  # pickle: the module attribute, i.e. the singleton

    def __copy__(self) -> "MissingType":
        return self

    def __deepcopy__(self, memo: dict) -> "MissingType":
        return self

    def __repr__(self) -> str:
        return "MISSING"


MISSING = MissingType()


def _truncated(text: str, limit: int) -> dict[str, Any]:
    """A text cell too long to send whole: its first `limit` characters and its full size."""
    return {"$type": "text", "preview": text[:limit], "size": len(text), "truncated": True}


def encode_value(value: Any, text_limit: int = TEXT_LIMIT) -> Any:
    """Convert a driver value into a JSON-safe cell without losing precision. `text_limit`: the longest text or JSON
    cell sent whole (characters)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if -MAX_SAFE_INT <= value <= MAX_SAFE_INT else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, JsonText):
        if len(value.text) > text_limit:
            return _truncated(value.text, text_limit)
        return {"$type": "json", "text": value.text}
    if value is MISSING:
        return {"$type": "missing"}
    if isinstance(value, str):
        return value if len(value) <= text_limit else _truncated(value, text_limit)
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        return {"$type": "bytes", "size": len(data), "preview": data[:BYTES_PREVIEW].hex()}
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)


def encode_row(row: Any, text_limit: int = TEXT_LIMIT) -> list[Any]:
    return [encode_value(v, text_limit) for v in row]
