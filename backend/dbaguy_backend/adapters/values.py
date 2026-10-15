import json
import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

MAX_SAFE_INT = 2**53 - 1
TEXT_LIMIT = 64 * 1024
BYTES_PREVIEW = 64


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
    if isinstance(value, str):
        if len(value) <= TEXT_LIMIT:
            return value
        return {"$type": "text", "preview": value[:TEXT_LIMIT], "size": len(value), "truncated": True}
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
