"""Lossless helpers for JSON text as Couchbase sends and stores it: never parsed and re-encoded, so numbers, key order
and escapes stay exactly as they are (04c adds the token comparison and the save diff here)."""

import re

# A JSON string (kept whole), or a run of whitespace between tokens (dropped). JSON strings hold no raw line breaks.
# The quantifiers are possessive: a string can only be read one way, and the plain `*` keeps a backtracking record per
# character (a 40 MB string value took 2.3 GB and 7.7 s; this takes 130 MB and 0.4 s).
_STRING_OR_SPACE = re.compile(r'("(?:[^"\\]++|\\.)*+")|[ \t\n\r]+')


def one_line(text: str) -> str:
    """`text` without the whitespace between its tokens: one line for JSON Lines, tokens unchanged (R-04-15)."""
    return _STRING_OR_SPACE.sub(lambda m: m.group(1) or "", text)
