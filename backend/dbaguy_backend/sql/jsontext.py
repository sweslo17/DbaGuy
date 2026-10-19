"""Lossless helpers for JSON text as Couchbase sends and stores it: pure text functions, never parsed and re-encoded, so
numbers, key order and escapes stay exactly as they are. Here, not in the adapter, because the row formats in
`formatting.py` use them and `sql/` takes no import from an adapter.

`value_end` locates a JSON value without building it: the response parser finds where an object or array inside a row
ends this way, so a big document costs its text, not a decoded tree. The checks a document save needs are built on it
(04c): `json_problem` says where a text stops being JSON, `number_paths` names the numbers the server would change, and
`diff` lists what the server would store differently from the edited text. None of them builds a tree (ruling C3): they
walk the text level by level, and `diff` skips every subtree whose text is the same on both sides."""

import json
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import zip_longest
from json.decoder import scanstring
from typing import NoReturn

from ..domain.couchbase import ChangeCounts, ChangeKind, DocumentChange

CHANGES_PER_KIND = 100  # changes kept per kind; the counts still count every one
CHANGES_LISTED = 200  # changes listed in a preview
CHANGE_TEXT_LIMIT = 200  # characters of a change's before / after text, then "…"
KEYS_SHOWN = 10  # keys named in a key-order change


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


# Runs of tokens with no whitespace between them: a string (kept whole), or anything else that is not whitespace (the
# last alternative keeps a lone quote, an unterminated string, as it was). A compact text is one run and joins back as
# the same object, where a substitution would build an object per string token (11.6× a token-dense text, measured). The
# quantifiers are possessive: a string can only be read one way, and the plain `*` keeps a backtracking record per
# character (a 40 MB string value took 2.3 GB and 7.7 s; this takes 130 MB and 0.4 s). JSON strings hold no raw line
# breaks, so `.` after a backslash never needs to match one.
_RUN = re.compile(r'(?:"(?:[^"\\]++|\\.)*+"|[^ \t\n\r"]++|")++')


def one_line(text: str) -> str:
    """`text` without the whitespace between its tokens: one line for JSON Lines, tokens unchanged (R-04-15)."""
    return "".join(_RUN.findall(text))


def value_end(text: str, start: int) -> int:
    """The index right after the JSON value that begins at text[start] (no whitespace before it), checked as
    `json.loads` would check it; nothing of it is kept. json.JSONDecodeError when there is no complete value there,
    NotJson for NaN or Infinity, RecursionError for nesting deeper than Python allows."""
    return _LEAN.raw_decode(text, start)[1]


@dataclass(frozen=True)
class JsonProblem:
    offset: int  # code points from the start of the text
    reason: str  # a fixed text: Python's decoder's own words or ours, never a quote of the text


_SPACE = re.compile(r"[ \t\n\r]*")
_STRING = re.compile(r'"(?:[^"\\]++|\\.)*+"')
_CONSTANT = re.compile(r'"(?:[^"\\]++|\\.)*+"|(NaN|-?Infinity)')


def json_problem(text: str) -> JsonProblem | None:
    """Where `text` stops being one JSON value with optional whitespace around it, or None. Checked by the C decoder
    with nothing kept (a 20 MiB text costs its text)."""
    start = _SPACE.match(text).end()
    try:
        end = value_end(text, start)
    except json.JSONDecodeError as exc:
        return JsonProblem(exc.pos, exc.msg)
    except NotJson:
        found = next((m for m in _CONSTANT.finditer(text, start) if m.group(1)), None)
        return JsonProblem(found.start(1) if found else start, "NaN and Infinity are not JSON")
    except RecursionError:
        return JsonProblem(start, "Nested too deeply")
    end = _SPACE.match(text, end).end()
    return None if end == len(text) else JsonProblem(end, "Extra data after the value")


def without_strings(text: str) -> str:
    """`text` with every JSON string replaced by `""`: what is left is the structure and the numbers."""
    return _STRING.sub('""', text)


_PLAIN_KEY = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")
_NUMBER_START = frozenset("-0123456789")
_KIND_ORDER: tuple[ChangeKind, ...] = ("other", "duplicate_key", "number", "key_order")
Steps = tuple[str | int, ...]


def _shown(text: str) -> str:
    """`text` with each lone surrogate (a decoded `\\ud800`) written as its escape, so an answer encodes as UTF-8."""
    return _LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def json_path(steps: Sequence[str | int]) -> str:
    """The path of `steps` as the value viewer writes it (`features/grid/json.ts`): `$`, `.key` for a plain key,
    `["any key"]` for another, `[n]` for an array index."""
    parts = ["$"]
    for step in steps:
        if isinstance(step, int):
            parts.append(f"[{step}]")
        elif _PLAIN_KEY.fullmatch(step):
            parts.append(f".{step}")
        else:
            parts.append(f"[{json.dumps(step, ensure_ascii=False)}]")
    return _shown("".join(parts))


# The walkers below read compact text (`one_line`) that `json_problem` accepted: no whitespace between tokens, so each
# step is a fixed offset from the last one, and each value's end comes from `value_end`.
def _members(text: str, start: int) -> list[tuple[str, int, int]]:
    """The members of the object at text[start]: (key, value start, value end), in order, duplicates kept."""
    members: list[tuple[str, int, int]] = []
    i = start + 1
    if text[i] == "}":
        return members
    while True:
        key, i = scanstring(text, i + 1)
        i += 1  # past the colon
        end = value_end(text, i)
        members.append((key, i, end))
        if text[end] == "}":
            return members
        i = end + 1  # past the comma


def _elements(text: str, start: int) -> Iterator[tuple[int, int]]:
    """The elements of the array at text[start] as (start, end), one at a time (an array can hold a million)."""
    i = start + 1
    if text[i] == "]":
        return
    while True:
        end = value_end(text, i)
        yield i, end
        if text[end] == "]":
            return
        i = end + 1


def number_paths(text: str, wanted: Callable[[str], bool], limit: int) -> tuple[list[str], int]:
    """The paths of at most `limit` numbers whose literal `wanted` accepts, in document order, and how many there are.
    `text` is valid JSON (`json_problem` found nothing); RecursionError for nesting deeper than Python allows."""
    text = one_line(text)
    paths: list[str] = []
    count = 0

    def walk(start: int, end: int, steps: Steps) -> None:
        nonlocal count
        first = text[start]
        if first == "{":
            for key, member_start, member_end in _members(text, start):
                walk(member_start, member_end, (*steps, key))
        elif first == "[":
            for index, (item_start, item_end) in enumerate(_elements(text, start)):
                walk(item_start, item_end, (*steps, index))
        elif first in _NUMBER_START and wanted(text[start:end]):
            count += 1
            if len(paths) < limit:
                paths.append(json_path(steps))

    walk(0, len(text), ())
    return paths, count


@dataclass(frozen=True)
class DocumentDiff:
    changes: list[DocumentChange]  # at most CHANGES_LISTED: value changes first, then the rest
    counts: ChangeCounts  # every change found, listed or not


def _short(text: str, start: int, end: int) -> str:
    """`text[start:end]` cut to CHANGE_TEXT_LIMIT characters, then `…` (a value of megabytes is never copied)."""
    shown = _shown(text[start : min(end, start + CHANGE_TEXT_LIMIT)])
    return shown + "…" if end - start > CHANGE_TEXT_LIMIT else shown


def _keys(keys: Sequence[str]) -> str:
    """A key list as a change shows it: KEYS_SHOWN keys written as JSON strings, cut at CHANGE_TEXT_LIMIT characters,
    then `, … (N keys)` when anything was left out."""
    names = ", ".join(json.dumps(key[:CHANGE_TEXT_LIMIT], ensure_ascii=False) for key in keys[:KEYS_SHOWN])
    shown = _shown(names[:CHANGE_TEXT_LIMIT])
    if len(keys) <= KEYS_SHOWN and len(names) <= CHANGE_TEXT_LIMIT:
        return shown
    return f"{shown}, … ({len(keys)} key{'' if len(keys) == 1 else 's'})"


def _count(n: int) -> str:
    return f"{n} element{'' if n == 1 else 's'}"


def _same_value(a: str, b: str) -> bool:
    """Whether two number literals are the same number; False when either does not parse (out of Decimal's range)."""
    try:
        return Decimal(a) == Decimal(b)
    except InvalidOperation:
        return False


_CHUNK = 1 << 16


def _same_text(e: str, es: int, ee: int, s: str, ss: int, se: int) -> bool:
    """Whether e[es:ee] and s[ss:se] are the same text. A long one is compared a chunk at a time, so nothing big is
    copied and the first difference ends it."""
    size = ee - es
    if size != se - ss:
        return False
    if size <= _CHUNK:
        return e[es:ee] == s[ss:se]
    return all(
        e[es + k : es + min(k + _CHUNK, size)] == s[ss + k : ss + min(k + _CHUNK, size)] for k in range(0, size, _CHUNK)
    )


Texts = Callable[[], tuple[str, str, bool]]  # a change's (before, after, value_changed), made only if it is kept


class _Found:
    """Changes as the walk meets them: counted all, kept CHANGES_PER_KIND per kind. A kept change's path and texts are
    built when it is kept, not when it is met: a document the server re-sorts throughout meets one change per object."""

    def __init__(self) -> None:
        self.kept: dict[ChangeKind, list[DocumentChange]] = {kind: [] for kind in _KIND_ORDER}
        self.counts: dict[ChangeKind, int] = dict.fromkeys(_KIND_ORDER, 0)

    def add(self, steps: Steps, kind: ChangeKind, texts: Texts) -> None:
        self.counts[kind] += 1
        kept = self.kept[kind]
        if len(kept) < CHANGES_PER_KIND:
            before, after, value_changed = texts()
            kept.append(
                DocumentChange(
                    path=json_path(steps), kind=kind, before=before, after=after, value_changed=value_changed
                )
            )

    def result(self) -> DocumentDiff:
        ordered = [change for kind in _KIND_ORDER for change in self.kept[kind]]
        listed = [c for c in ordered if c.value_changed] + [c for c in ordered if not c.value_changed]
        return DocumentDiff(changes=listed[:CHANGES_LISTED], counts=ChangeCounts(**self.counts))


def _compare(e: str, es: int, ee: int, s: str, ss: int, se: int, steps: Steps, found: _Found) -> None:
    """The edited value e[es:ee] against the server's s[ss:se], both compact and not the same text."""
    first, server_first = e[es], s[ss]
    if first == "{" and server_first == "{":
        last: dict[str, tuple[int, int]] = {}
        for key, start, end in _members(e, es):
            if key in last:  # the server keeps the last value of a repeated key
                dropped = last[key]
                found.add(
                    (*steps, key), "duplicate_key", lambda: (_short(e, *dropped), _short(e, start, end), True)
                )
            last[key] = (start, end)
        server = _members(s, ss)
        order, server_order = list(last), [key for key, _, _ in server]
        if last.keys() != set(server_order):
            found.add(steps, "other", lambda: (_keys(order), _keys(server_order), True))
            return
        if order != server_order:
            found.add(steps, "key_order", lambda: (_keys(order), _keys(server_order), False))
        for key, start, end in server:
            edited_start, edited_end = last[key]
            if not _same_text(e, edited_start, edited_end, s, start, end):
                _compare(e, edited_start, edited_end, s, start, end, (*steps, key), found)
    elif first == "[" and server_first == "[":
        edited_items, server_items = _elements(e, es), _elements(s, ss)
        for index, (a, b) in enumerate(zip_longest(edited_items, server_items)):
            if a is None or b is None:  # different lengths: counted to the end, said once
                left = index + (a is not None) + sum(1 for _ in edited_items)
                right = index + (b is not None) + sum(1 for _ in server_items)
                found.add(steps, "other", lambda: (_count(left), _count(right), True))
                return
            if not _same_text(e, *a, s, *b):
                _compare(e, *a, s, *b, (*steps, index), found)
    elif first == '"' and server_first == '"':
        if scanstring(e, es + 1)[0] != scanstring(s, ss + 1)[0]:
            found.add(steps, "other", lambda: (_short(e, es, ee), _short(s, ss, se), True))
    elif first in _NUMBER_START and server_first in _NUMBER_START:
        found.add(steps, "number", lambda: (_short(e, es, ee), _short(s, ss, se), not _same_value(e[es:ee], s[ss:se])))
    else:
        found.add(steps, "other", lambda: (_short(e, es, ee), _short(s, ss, se), True))


def diff(edited: str, server: str) -> DocumentDiff:
    """What storing `edited` would change, given `server`, the server's form of it (`SELECT RAW $doc`, A2): key order,
    number spelling, dropped duplicate keys, anything else as `other`. Whitespace and string escape spelling are not
    changes. Both texts are valid JSON; never builds a tree (ruling C3); RecursionError for nesting deeper than Python
    allows."""
    found = _Found()
    edited, server = one_line(edited), one_line(server)
    if edited != server:
        _compare(edited, 0, len(edited), server, 0, len(server), (), found)
    return found.result()
