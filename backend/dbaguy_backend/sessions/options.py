"""Per-request options of execute and export, checked before anything runs (spec §3.2.1–§3.2.2): an engine only ever
receives options its capabilities declare, and parameters that are valid JSON literals under valid names, with no number
beyond a double (B46). Messages name the parameter, never its value (spec §4)."""

import json
import math
import re
from typing import NoReturn

from ..adapters.base import AdapterCapabilities, RunOptions, StatementError
from ..domain.execution import ExecuteCommand, ExportCommand, StatementParams

MAX_PARAM_BYTES = 1024 * 1024
UNSUPPORTED_OPTION = "unsupported_option"
INVALID_PARAMS = "invalid_params"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BEYOND_DOUBLE = "Parameter {label}: a number in it is too large for the query service (beyond a double)"


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(name)


class _BeyondDouble(Exception):
    """A number in a literal overflows a double: the query service refuses it with an error that quotes the whole
    literal (1040 "cannot unmarshal number …"), which would reach History and the log (B46)."""


def _number(text: str) -> str:
    """A number's text, kept as text (only the shape is checked); one that overflows a double is refused (B46)."""
    if math.isinf(float(text)):
        raise _BeyondDouble()
    return text


def _invalid(text: str) -> StatementError:
    return StatementError(text, code=INVALID_PARAMS)


def _check_literal(label: str, literal: str) -> None:
    try:
        size = len(literal.encode("utf-8"))
    except UnicodeEncodeError:
        raise _invalid(f"Parameter {label} holds a character that is not valid text") from None
    if size > MAX_PARAM_BYTES:
        raise _invalid(f"Parameter {label} is larger than 1 MiB")
    try:  # numbers stay text (Decimal raises on 1e99999…): only a double's range is checked, the server judges more
        json.loads(literal, parse_int=_number, parse_float=_number, parse_constant=_reject_constant)
    except _BeyondDouble:
        raise _invalid(_BEYOND_DOUBLE.format(label=label)) from None
    except (ValueError, RecursionError):
        raise _invalid(f'Parameter {label} is not a JSON value (a string needs double quotes: "text")') from None


def check_params(params: StatementParams) -> None:
    """Every name is a SQL++ identifier and every literal one JSON value of at most 1 MiB (StatementError
    `invalid_params`)."""
    for name, literal in params.named.items():
        if not _NAME.fullmatch(name):
            raise _invalid(f"${name[:40]} is not a parameter name: use letters, digits and _")
        _check_literal(f"${name[:40]}", literal)
    for index, literal in enumerate(params.positional, start=1):
        _check_literal(f"${index}", literal)


def run_options(command: ExecuteCommand | ExportCommand, capabilities: AdapterCapabilities) -> RunOptions:
    """The options `run()` gets for `command`; raises StatementError (unsupported_option / invalid_params).
    `retry_safe` stays False: only the worker knows the classification, after the policy."""
    context = command.query_context
    given = command.params
    params = given if given is not None and (given.named or given.positional) else None  # empty counts as none
    profile_plan = isinstance(command, ExecuteCommand) and command.profile_plan
    if context is not None and not capabilities.query_context:
        raise StatementError("This engine does not take a query context", code=UNSUPPORTED_OPTION)
    if params is not None and not capabilities.parameters:
        raise StatementError("This engine does not take parameters", code=UNSUPPORTED_OPTION)
    if profile_plan and not capabilities.executed_plan:
        raise StatementError("This engine does not take an executed plan", code=UNSUPPORTED_OPTION)
    if params is not None:
        check_params(params)
    return RunOptions(query_context=context, params=params, profile_plan=profile_plan)
