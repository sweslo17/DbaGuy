"""Lossless helpers for JSON text as Couchbase sends and stores it: pure text functions, never parsed and re-encoded, so
numbers, key order and escapes stay exactly as they are (04c adds the token comparison and the save diff here). Here, not
in the adapter, because the row formats in `formatting.py` use them and `sql/` takes no import from an adapter.

`value_end` locates a JSON value without building it: the response parser finds where an object or array inside a row
ends this way, so a big document costs its text, not a decoded tree."""

import json
import re
from typing import NoReturn

# A JSON string (kept whole), or a run of whitespace between tokens (dropped). JSON strings hold no raw line breaks.
# The quantifiers are possessive: a string can only be read one way, and the plain `*` keeps a backtracking record per
# character (a 40 MB string value took 2.3 GB and 7.7 s; this takes 130 MB and 0.4 s).
_STRING_OR_SPACE = re.compile(r'("(?:[^"\\]++|\\.)*+")|[ \t\n\r]+')


class NotJson(ValueError):
    """NaN or Infinity: Python's decoder reads them, JSON has no such values."""


def _not_json(name: str) -> NoReturn:
    raise NotJson(f"{name} is not JSON")


# The C decoder with hooks that keep nothing: each object and number is handed to `bool` as soon as it has been read and
# only a shared True or False is kept, so finding where a value ends costs its text, not a tree (an array still holds
# its elements until it closes: an array of many short strings peaks at about 7× its text). Strings are still decoded,
# which also checks their escapes. `bool` rather than a Python function: no Python frame runs per number or object
# (number-dense text is located 20% faster than the row decoder decodes it, not 20% slower), and the nesting limit
# stays the row decoder's (a Python hook called at the deepest level refused rows two levels shallower).
_LEAN = json.JSONDecoder(object_pairs_hook=bool, parse_int=bool, parse_float=bool, parse_constant=_not_json)


def one_line(text: str) -> str:
    """`text` without the whitespace between its tokens: one line for JSON Lines, tokens unchanged (R-04-15)."""
    return _STRING_OR_SPACE.sub(lambda m: m.group(1) or "", text)


def value_end(text: str, start: int) -> int:
    """The index right after the JSON value that begins at text[start] (no whitespace before it), checked as
    `json.loads` would check it; nothing of it is kept. json.JSONDecodeError when there is no complete value there,
    NotJson for NaN or Infinity, RecursionError for nesting deeper than Python allows."""
    return _LEAN.raw_decode(text, start)[1]
