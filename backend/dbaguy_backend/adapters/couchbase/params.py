"""Parameter literals whose numbers the query service would change (R-04-10, spec §3.5.4). Pure: no I/O, no logging.

The request body carries each literal exactly as typed (http.build_body), but the query service reads a JSON number as
an int64 only when it is an integer that fits; anything else becomes a float64, which it then shows as an int64 (its
exact integer) when it is whole and within ±2⁶³, else by its shortest repr (A26 and the 04b Task 4 review, live on
7.6.12: `18446744073709551615` → `18446744073709552000`, `3.141592653589793238462643383279` → `3.141592653589793`,
`1.10` → `1.1`, `1.2345678901234568e18` → `1234567890123456768`; `1e20` and `1234567890123456768.0` come back as
written). A parameter is flagged when the value shown differs from the value written (ruling B11; `1.10`, `0.1`,
`1E+3`, `-0.0` are not flagged). 2⁶³ itself overflows the int64 (what comes back depends on the platform): flagged. A
number beyond a double (`1e400`) is refused by the server; `sessions.options.check_params` refuses it before (B46).
The warning names the parameter, never its value (spec §4, ruling B10)."""

import json
import math
from decimal import Decimal, InvalidOperation

from ...domain.execution import MessageSeverity, ServerMessage, StatementParams
from ...sql.jsontext import reject_constant

INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1
# "-9223372036854775808" is the longest int64 text: a longer JSON integer (no leading zeros) is out of range, decided
# without int(), whose digit limit a 5,000-digit literal would hit
_INT64_TEXT_MAX = 20
_WARNING = ("Parameter {label}: the query service reads a number in it as a double, which changes its value — "
            "send it as a string to keep it exact")


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
        json.JSONDecoder(parse_int=integer, parse_float=decimal, parse_constant=reject_constant).decode(literal)
    except (ValueError, RecursionError):
        return None
    return integers, decimals


def _integer_changes(text: str) -> bool:
    """An integer that fits int64 is read as one; any other is read as a double."""
    fits = len(text) <= _INT64_TEXT_MAX and INT64_MIN <= int(text) <= INT64_MAX
    return not fits and _double_changes(text)


def _double_changes(text: str) -> bool:
    """The value the server shows for the double `text` reads as differs from the value written (module docstring).
    `1e400` reads as inf (a change; refused before it is sent, B46), `1e-400` as 0.0 (a change)."""
    value = float(text)
    if math.isinf(value):
        return True
    try:
        written = Decimal(text)
    except InvalidOperation:  # an exponent past ±10**18 (Decimal's own limit): the double is 0.0 here (inf is above)
        mantissa = text.lower().partition("e")[0]
        return any(digit in mantissa for digit in "123456789")
    if value.is_integer() and abs(value) <= 2.0**63:  # shown as an int64: its exact integer
        return value == 2.0**63 or Decimal(int(value)) != written
    return Decimal(repr(value)) != written


def changes_as_double(literal: str) -> bool:
    """Whether the query service would change a number in this JSON literal by reading it as a double (module
    docstring). A literal that is not JSON gives False: `sessions.options.check_params` has refused it already."""
    numbers = _number_texts(literal)
    if numbers is None:
        return False
    integers, decimals = numbers
    return any(map(_integer_changes, integers)) or any(map(_double_changes, decimals))


def parameter_warnings(params: StatementParams | None) -> list[ServerMessage]:
    """One WARNING per parameter that `changes_as_double`, named `$name` or `$n` (positional, 1-based): named ones
    first, as the request sends them. Never the value."""
    if params is None:
        return []
    labels = [*(f"${name}" for name, literal in params.named.items() if changes_as_double(literal)),
              *(f"${index}" for index, literal in enumerate(params.positional, start=1) if changes_as_double(literal))]
    return [ServerMessage(severity=MessageSeverity.WARNING, text=_WARNING.format(label=label)) for label in labels]
