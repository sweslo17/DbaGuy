import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, final
from uuid import UUID

MAX_SAFE_INT = 2**53 - 1
TEXT_LIMIT = 64 * 1024
BYTES_PREVIEW = 64


@dataclass(frozen=True, slots=True)
class JsonText:
    """A JSON value kept as the server's text (Couchbase): an object or array, or a number no Python number holds
    exactly (`1.10`, `1e400`, an integer past ±(2^53−1)). The text is never re-encoded (spec §3.2.4)."""

    text: str


@final
class MissingType:
    """The row has no such key (SQL++ MISSING), which is not NULL (R-04-8). Use the `MISSING` instance."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"


MISSING = MissingType()


def _truncated(text: str) -> dict[str, Any]:
    """A text cell too long to send whole: its first TEXT_LIMIT characters and its full size."""
    return {"$type": "text", "preview": text[:TEXT_LIMIT], "size": len(text), "truncated": True}


def encode_value(value: Any) -> Any:
    """Convert a driver value into a JSON-safe cell without losing precision."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if -MAX_SAFE_INT <= value <= MAX_SAFE_INT else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, JsonText):
        return {"$type": "json", "text": value.text} if len(value.text) <= TEXT_LIMIT else _truncated(value.text)
    if value is MISSING:
        return {"$type": "missing"}
    if isinstance(value, str):
        return value if len(value) <= TEXT_LIMIT else _truncated(value)
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


def encode_row(row: Any) -> list[Any]:
    return [encode_value(v) for v in row]
