"""What the query service says besides rows, as DbaGuy shows it (spec §3.5.4, §4): errors with the server's own text
(M10), its code and a UTF-16 position, and whether an error is the server's own answer; warnings and the metrics line
as messages. Pure."""

from collections.abc import Mapping, Sequence
from typing import Any

from dbaguy_backend.adapters.base import StatementError
from dbaguy_backend.domain.execution import ErrorPosition, MessageSeverity, ServerMessage
from dbaguy_backend.sql.offsets import utf16_column

PROTOCOL_ERROR = "The query service sent a malformed response"
_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB")


def _line(entry: Any) -> str:
    """`code: msg` for an error or warning object (the text alone without a code); anything else as text."""
    if isinstance(entry, dict):
        code, text = entry.get("code"), str(entry.get("msg") or "")
        return f"{code}: {text}" if code is not None else text
    return str(entry)


def _first_text(error: dict[str, Any]) -> str:
    text = str(error.get("msg") or "The query failed")
    reason = error.get("reason")
    if isinstance(reason, dict):
        if detail := reason.get("message") or reason.get("msg") or reason.get("key"):
            text += f"\nReason: {detail}"
        if role := reason.get("missing_role"):
            text += f"\nMissing role: {role}"
    elif reason:
        text += f"\nReason: {reason}"
    return text


def _position(error: dict[str, Any], sql: str) -> ErrorPosition | None:
    line, column = error.get("line"), error.get("column")
    if type(line) is int and type(column) is int and line >= 1 and column >= 1:  # not bool: True is an int
        return ErrorPosition(line=line, column=utf16_column(sql, line, column))
    return None


def statement_error(errors: Sequence[Any], sql: str) -> StatementError:
    """The statement's error: the first one's message (with `reason` and `missing_role`), code and position; any
    further errors one line each."""
    first = errors[0] if errors else {}
    if not isinstance(first, dict):  # an error that is not an object: its text is the message
        first = {"msg": first}
    message = "\n".join([_first_text(first), *(_line(error) for error in errors[1:])])
    code = first.get("code")
    return StatementError(message, code=str(code) if code is not None else None, position=_position(first, sql))


def is_server_error(exc: StatementError) -> bool:
    """An error the query service answered with (a numeric code), not a failure to reach it."""
    return exc.code is not None and exc.code.isdigit()


def warning_messages(warnings: Sequence[Any]) -> list[ServerMessage]:
    return [ServerMessage(severity=MessageSeverity.WARNING, text=_line(warning)) for warning in warnings]


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


def _size(n: int) -> str:
    value, unit = float(n), 0
    while value >= 1024 and unit < len(_BYTE_UNITS) - 1:
        value, unit = value / 1024, unit + 1
    return f"{n} B" if unit == 0 else f"{value:.1f} {_BYTE_UNITS[unit]}"


def metrics_message(metrics: Mapping[str, Any]) -> ServerMessage | None:
    """`Elapsed 12.96s · execution 12.95s · 1,000 results · 2.1 MB · 3 mutations · 1 warning`: the fields present only;
    durations as the server writes them (Go format), never parsed."""
    parts: list[str] = []
    for key, label in (("elapsedTime", "Elapsed"), ("executionTime", "execution")):
        if isinstance(duration := metrics.get(key), str) and duration:
            parts.append(f"{label} {duration}")
    for key, noun in (("resultCount", "result"), ("resultSize", None), ("mutationCount", "mutation"),
                      ("warningCount", "warning")):
        value = metrics.get(key)
        if type(value) is int:
            parts.append(_size(value) if noun is None else _count(value, noun))
    return ServerMessage(severity=MessageSeverity.INFO, text=" · ".join(parts)) if parts else None
