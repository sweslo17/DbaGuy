"""Parameter literals whose numbers the query service would change (R-04-10, spec §3.5.4). Pure: no I/O, no logging.

The request body carries each literal exactly as typed (http.build_body), but the query service reads a JSON number as
an int64 only when it is an integer that fits; anything else becomes a float64 (A26: `18446744073709551615` comes back
as `18446744073709552000`, `3.141592653589793238462643383279` as `3.141592653589793`, `1.10` as `1.1`). A parameter
is flagged when that changes a value (ruling B11): an integer outside int64, or a decimal / exponent number whose
float64 is a different value (`1.10`, `0.1`, `1E+3`, `-0.0` are not: their float64 is the value written). The warning
names the parameter, never its value (spec §4, ruling B10)."""

import json
from decimal import Decimal, InvalidOperation
from typing import NoReturn

from ...domain.execution import MessageSeverity, ServerMessage, StatementParams

INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1
# "-9223372036854775808" is the longest int64 text: a longer JSON integer (no leading zeros) is out of range, decided
# without int(), whose digit limit a 5,000-digit literal would hit
_INT64_TEXT_MAX = 20
_WARNING = ("Parameter {label}: the query service reads a number in it as a double, which changes its value — "
            "send it as a string to keep it exact")


def _not_json(name: str) -> NoReturn:
    raise ValueError(f"{name} is not JSON")


def _number_texts(literal: str) -> tuple[list[str], list[str]] | None:
    """The integer and the decimal / exponent number texts in `literal`, anywhere in it (inside arrays and objects too,
    never inside strings); None when it is not one JSON value."""
    integers: list[str] = []
    decimals: list[str] = []

    def integer(text: str) -> int:
        integers.append(text)
        return 0

    def decimal(text: str) -> float:
        decimals.append(text)
        return 0.0

    try:
        json.JSONDecoder(parse_int=integer, parse_float=decimal, parse_constant=_not_json).decode(literal)
    except (ValueError, RecursionError):
        return None
    return integers, decimals


def _integer_changes(text: str) -> bool:
    return len(text) > _INT64_TEXT_MAX or not INT64_MIN <= int(text) <= INT64_MAX


def _decimal_changes(text: str) -> bool:
    """The float64 the text reads as (its shortest repr) is not the value written. `1e400` reads as inf, `1e-400` as
    0.0: both changes."""
    try:
        written = Decimal(text)
    except InvalidOperation:  # an exponent past ±10**18 (Decimal's own limit): the value is 0 or no float64's
        mantissa = text.lower().partition("e")[0]
        return any(digit in mantissa for digit in "123456789")
    return Decimal(repr(float(text))) != written


def changes_as_double(literal: str) -> bool:
    """Whether the query service would change a number in this JSON literal by reading it as a double (module
    docstring). A literal that is not JSON gives False: `sessions.options.check_params` has refused it already."""
    numbers = _number_texts(literal)
    if numbers is None:
        return False
    integers, decimals = numbers
    return any(map(_integer_changes, integers)) or any(map(_decimal_changes, decimals))


def parameter_warnings(params: StatementParams | None) -> list[ServerMessage]:
    """One WARNING per parameter that `changes_as_double`, named `$name` or `$n` (positional, 1-based): named ones
    first, as the request sends them. Never the value."""
    if params is None:
        return []
    labels = [*(f"${name}" for name, literal in params.named.items() if changes_as_double(literal)),
              *(f"${index}" for index, literal in enumerate(params.positional, start=1) if changes_as_double(literal))]
    return [ServerMessage(severity=MessageSeverity.WARNING, text=_WARNING.format(label=label)) for label in labels]
