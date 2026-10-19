"""Lossless helpers for JSON text as Couchbase sends and stores it: pure text functions, never parsed and re-encoded, so
numbers, key order and escapes stay exactly as they are. Here, not in the adapter, because the row formats in
`formatting.py` use them and `sql/` takes no import from an adapter.

`value_end` locates a JSON value without building it: the response parser finds where an object or array inside a row
ends this way, so a big document costs a few times its text (how many depends on its shape, see `_LEAN`), never a
decoded tree. The checks a document save needs are built on it (04c): `json_problem` says where a text stops being
JSON (or that it nests deeper than MAX_DOCUMENT_DEPTH levels), `number_paths` names the numbers the server would
change, and `diff` lists what the server would store differently from the edited text. None of them builds a tree
(ruling C3): they walk the text level by level, `diff` skips every subtree whose text is the same on both sides, and
what a comparison remembers is offsets (arrays for a big object), never one Python object per member. Every level reads
its children again, so their time is about the text's size times its levels, which is what the depth limit bounds."""

import json
import re
from array import array
from collections.abc import Callable, Iterable, Iterator, MutableSequence, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import partial
from itertools import islice, zip_longest
from json.decoder import scanstring
from typing import NoReturn

from ..domain.couchbase import ChangeCounts, ChangeKind, DocumentChange

CHANGES_PER_KIND = 100  # changes kept per kind; the counts still count every one
CHANGES_LISTED = 200  # changes listed in a preview
CHANGE_TEXT_LIMIT = 200  # characters of a change's before / after text, then "…"
KEYS_SHOWN = 10  # keys named in a key-order or key-set change
EXCERPT_LEAD = 40  # characters kept before the first difference when a long text is cut around it
MAX_DOCUMENT_DEPTH = 100  # levels of nesting a document may have (ruling C38): the checks below read each level again


class NotJson(ValueError):
    """NaN or Infinity: Python's decoder reads them, JSON has no such values."""


def reject_constant(name: str) -> NoReturn:
    """A JSON decoder's `parse_constant`: NaN, Infinity and -Infinity are not JSON."""
    raise NotJson(f"{name} is not JSON")


# The C decoder with hooks that keep nothing: each object and number is handed to `bool` as soon as it has been read and
# only a shared True or False is kept. Strings are still decoded, which also checks their escapes. Arrays have no hook:
# an array holds its elements until it closes (each closed inner array stays a list in it), and an open object holds its
# members in a dict. So what locating a value costs depends on its shape. Peaks over a whole 4 MiB row (its texts
# included), decoding the tree in brackets: objects or numbers 3.0× (9.9–11×); an object keyed by id 4.4× (12×);
# `[[ts, value], …]` 6.2× (13×); an array of short strings 6.8× (8.8×); one flat object with many keys 8.5× (12×), and
# 11.6× (14×) at 20 MiB; `[[1, 2], …]` 17× (19×); `[[], …]` 23× (25×). Never more than decoding.
# `bool` rather than a Python function: no Python frame runs per number or object (number-dense text is located
# 20% faster than the row decoder decodes it, not 20% slower), and the nesting limit stays the row decoder's (a Python
# hook called at the deepest level refused rows two levels shallower). `object_hook` rather than `object_pairs_hook`:
# an open object's member takes a dict slot, not a tuple and a list slot (one flat object with many keys 8.5×, not
# 11.8×).
_LEAN = json.JSONDecoder(object_hook=bool, parse_int=bool, parse_float=bool, parse_constant=reject_constant)


_STRING_SRC = r'"(?:[^"\\]++|\\.)*+"'  # a JSON string, possessive: it can only be read one way

# Runs of tokens with no whitespace between them: a string (kept whole), or anything else that is not whitespace (the
# last alternative keeps a lone quote, an unterminated string, as it was). A compact text is one run and joins back as
# the same object, where a substitution would build an object per string token (11.6× a token-dense text, measured). The
# quantifiers are possessive: a string can only be read one way, and the plain `*` keeps a backtracking record per
# character (a 40 MB string value took 2.3 GB and 7.7 s; this takes 130 MB and 0.4 s). JSON strings hold no raw line
# breaks, so `.` after a backslash never needs to match one.
_RUN = re.compile(rf'(?:{_STRING_SRC}|[^ \t\n\r"]++|")++')


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
    reason: str  # a fixed text: Python's decoder's own words (without their trailing "at") or ours, never a quote of it


# JSON whitespace (RFC 8259): a run of it, possibly empty. Nothing else is whitespace to JSON (no NBSP, no VT).
JSON_SPACE = re.compile(r"[ \t\n\r]*")
_STRING = re.compile(_STRING_SRC)
_CONSTANT = re.compile(rf"{_STRING_SRC}|(NaN|-?Infinity)")
_TOO_DEEP = f"Nested deeper than {MAX_DOCUMENT_DEPTH} levels"


def _reason(message: str) -> str:
    """The decoder's words without the "at" that its position followed: "Unterminated string starting at" says
    "Unterminated string"."""
    return message.removesuffix(" starting at").removesuffix(" at")


_ESCAPES = ("\\\\", '\\"', "\\/", "\\b", "\\f", "\\n", "\\r", "\\t", "\\u")  # an escaped backslash first
_NOT_STRUCTURE = bytes(set(range(256)) - set(b'"[]{}'))


def _nested_deeper_than(text: str, limit: int) -> bool:
    """Whether the valid JSON `text` holds containers nested more than `limit` levels deep. Escapes go first, so every
    string is `"…"` with no quote inside; then only the brackets and quotes are kept (C-speed byte operations, no
    object per token), empty strings go, and the text between the remaining quotes (strings that hold a bracket) is
    dropped. Then the innermost pairs are removed one round at a time: a round takes exactly one level off the deepest
    nesting, so anything left after `limit` rounds is deeper than `limit` (a few rounds for a real document; no work at
    all when the text holds no more than `limit` opening brackets, strings included)."""
    if text.count("[") + text.count("{") <= limit:
        return False
    if "\\" in text:
        for escape in _ESCAPES:
            text = text.replace(escape, "")
    kept = text.encode("ascii", "ignore").translate(None, _NOT_STRUCTURE).replace(b'""', b"")
    if b'"' in kept:
        kept = b"".join(kept.split(b'"')[::2])
    brackets = kept.replace(b"{", b"[").replace(b"}", b"]")
    for _ in range(limit):
        brackets = brackets.replace(b"[]", b"")
        if not brackets:
            return False
    return True


def json_problem(text: str) -> JsonProblem | None:
    """Where `text` stops being one JSON value with optional whitespace around it (or nests deeper than
    MAX_DOCUMENT_DEPTH levels, which `number_paths` and `diff` would read again at every level), or None. Checked by
    the C decoder with nothing kept (a 20 MiB text costs its text), then a few passes over its brackets."""
    start = JSON_SPACE.match(text).end()
    try:
        end = value_end(text, start)
    except json.JSONDecodeError as exc:
        return JsonProblem(exc.pos, _reason(exc.msg))
    except NotJson:
        found = next((m for m in _CONSTANT.finditer(text, start) if m.group(1)), None)
        return JsonProblem(found.start(1) if found else start, "NaN and Infinity are not JSON")
    except RecursionError:  # far deeper than MAX_DOCUMENT_DEPTH
        return JsonProblem(start, _TOO_DEEP)
    end = JSON_SPACE.match(text, end).end()
    if end != len(text):
        return JsonProblem(end, "Extra data after the value")
    return JsonProblem(start, _TOO_DEEP) if _nested_deeper_than(text, MAX_DOCUMENT_DEPTH) else None


def without_strings(text: str) -> str:
    """`text` with every JSON string replaced by `""`: what is left is the structure and the numbers."""
    return _STRING.sub('""', text)


_PLAIN_KEY = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
# A surrogate code point in a str: a decoded `\ud800`, never half of a pair (a str holds pairs as one character).
LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")
_NUMBER_START = frozenset("-0123456789")
_KIND_ORDER: tuple[ChangeKind, ...] = ("other", "duplicate_key", "number", "key_order")
Steps = tuple[str | int, ...]


def _shown(text: str) -> str:
    """`text` with each lone surrogate (a decoded `\\ud800`) written as its escape, so an answer encodes as UTF-8."""
    return LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


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
# step is a fixed offset from the last one, and each value's end comes from `value_end`. Nothing per member is kept
# as a Python object: a wide object (a million keys) is read one member at a time, and what a comparison must remember
# is held as offsets in `array("q")`, 8 bytes each. What stays is the cost `_LEAN` describes: a member whose value is a
# big array is located by `value_end` over the whole of it (`[[1, 2], …]` and `[[], …]` under an object member take
# 16-21× the text while it is located; the same arrays at the top of a text are read element by element).
def _members(text: str, start: int) -> Iterator[tuple[str, int, int, int]]:
    """The members of the object at text[start], one at a time: (key, key start, value start, value end), in order,
    duplicates kept."""
    i = start + 1
    if text[i] == "}":
        return
    while True:
        key_start = i
        key, i = scanstring(text, i + 1)
        i += 1  # past the colon
        end = value_end(text, i)
        yield key, key_start, i, end
        if text[end] == "}":
            return
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
    `text` is valid JSON that `json_problem` accepted (nested at most MAX_DOCUMENT_DEPTH levels); RecursionError for
    nesting deeper than Python allows."""
    text = one_line(text)
    paths: list[str] = []
    count = 0

    def walk(start: int, end: int, steps: Steps) -> None:
        nonlocal count
        first = text[start]
        if first == "{":
            for key, _, member_start, member_end in _members(text, start):
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


_CHUNK = 1 << 16


def _first_difference(a: str, b: str) -> int:
    """The index of the first character where `a` and `b` differ (the shorter length when one is a prefix of the other),
    found a chunk at a time."""
    shortest = min(len(a), len(b))
    i = 0
    while i < shortest and a[i : i + _CHUNK] == b[i : i + _CHUNK]:
        i += _CHUNK
    i = min(i, shortest)
    while i < shortest and a[i] == b[i]:
        i += 1
    return i


_HEX = frozenset("0123456789abcdefABCDEF")


def _escape_start(text: str, i: int) -> int:
    """`i`, or the index of the backslash when text[i] lies inside an escape (`\\n`, `\\uXXXX`) of the JSON `text`: a
    cut there would show half an escape."""
    if i <= 0 or i >= len(text):
        return i

    def escaped(k: int) -> bool:  # whether text[k] follows an odd number of backslashes
        j = k - 1
        while j >= 0 and text[j] == "\\":
            j -= 1
        return (k - 1 - j) % 2 == 1

    if escaped(i):
        return i - 1
    for k in range(max(1, i - 4), i):  # text[k] == "u" with at most 3 hex digits between it and i
        if text[k] == "u" and escaped(k) and all(c in _HEX for c in text[k + 1 : i]):
            return k - 1
    return i


def _excerpts(a: str, b: str) -> tuple[str, str]:
    """The JSON texts `a` and `b` as a change shows them: whole when both fit in CHANGE_TEXT_LIMIT characters, else each
    cut to that many from EXCERPT_LEAD characters before the first difference, with `…` on a cut side (a long text that
    differs only far in is not shown as two copies of its start); a cut never falls inside an escape."""
    if max(len(a), len(b)) <= CHANGE_TEXT_LIMIT:
        return _shown(a), _shown(b)
    start = max(0, _first_difference(a, b) - EXCERPT_LEAD)

    def cut(text: str) -> str:
        first, stop = _escape_start(text, start), _escape_start(text, start + CHANGE_TEXT_LIMIT)
        return ("…" if first else "") + _shown(text[first:stop]) + ("…" if len(text) > stop else "")

    return cut(a), cut(b)


def _string_excerpts(a: str, b: str) -> tuple[str, str]:
    """Two long decoded strings as a change shows them: CHANGE_TEXT_LIMIT characters of each (the opening quote counts
    as one) from EXCERPT_LEAD characters before the first difference. The decoded text is cut and the piece written as a
    JSON string, so the cut is whole characters and whole escapes."""
    start = max(0, _first_difference(a, b) - EXCERPT_LEAD)
    stop = start + CHANGE_TEXT_LIMIT - (start == 0)

    def cut(text: str) -> str:
        piece = _shown(json.dumps(text[start:stop], ensure_ascii=False)[1:-1])
        return ("…" if start else '"') + piece + ("…" if len(text) > stop else '"')

    return cut(a), cut(b)


def _value_texts(e: str, es: int, ee: int, s: str, ss: int, se: int) -> tuple[str, str]:
    """The values e[es:ee] and s[ss:se] as a change shows them: whole when short, else cut around where they first
    differ (from the start when their first characters differ, as two values of another type do, and then neither is
    copied whole). Two long strings are compared by their decoded values: `\\u00e9` against `é` is no difference."""
    if e[es] != s[ss]:
        return _excerpts(e[es : min(ee, es + CHANGE_TEXT_LIMIT + 1)], s[ss : min(se, ss + CHANGE_TEXT_LIMIT + 1)])
    if e[es] == '"' and max(ee - es, se - ss) > CHANGE_TEXT_LIMIT:
        return _string_excerpts(scanstring(e, es + 1)[0], scanstring(s, ss + 1)[0])
    return _excerpts(e[es:ee], s[ss:se])


def _keys(names: Iterable[str], total: int) -> str:
    """A key list as a change shows it: at most KEYS_SHOWN of `names` written as JSON strings, cut at CHANGE_TEXT_LIMIT
    characters, then `, … (N keys)` when any of the `total` is left out; "(none)" for no key."""
    if total == 0:
        return "(none)"
    written = ", ".join(json.dumps(name[:CHANGE_TEXT_LIMIT], ensure_ascii=False) for name in islice(names, KEYS_SHOWN))
    shown = _shown(written[:CHANGE_TEXT_LIMIT])
    if total <= KEYS_SHOWN and len(written) <= CHANGE_TEXT_LIMIT:
        return shown
    return f"{shown}, … ({total} key{'' if total == 1 else 's'})"


def _count(n: int) -> str:
    return f"{n} element{'' if n == 1 else 's'}"


def _same_value(a: str, b: str) -> bool:
    """Whether two number literals are the same number; False when either does not parse (out of Decimal's range)."""
    try:
        return Decimal(a) == Decimal(b)
    except InvalidOperation:
        return False


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


Texts = Callable[[], tuple[str, str]]  # a change's (before, after), made only if it is kept


class _Found:
    """Changes as the walk meets them: counted all, kept CHANGES_PER_KIND per kind and per `value_changed` (a flood of
    respelled numbers must not use up the room of a lost digit). A kept change's path and texts are built when it is
    kept, not when it is met: a document the server re-sorts throughout meets one change per object."""

    def __init__(self) -> None:
        self.kept: dict[tuple[ChangeKind, bool], list[DocumentChange]] = {
            (kind, value_changed): [] for kind in _KIND_ORDER for value_changed in (True, False)
        }
        self.counts: dict[ChangeKind, int] = dict.fromkeys(_KIND_ORDER, 0)

    def add(self, steps: Steps, kind: ChangeKind, value_changed: bool, texts: Texts) -> None:
        self.counts[kind] += 1
        kept = self.kept[kind, value_changed]
        if len(kept) < CHANGES_PER_KIND:
            before, after = texts()
            kept.append(
                DocumentChange(
                    path=json_path(steps), kind=kind, before=before, after=after, value_changed=value_changed
                )
            )

    def result(self) -> DocumentDiff:
        listed = [c for flag in (True, False) for kind in _KIND_ORDER for c in self.kept[kind, flag]]
        return DocumentDiff(changes=listed[:CHANGES_LISTED], counts=ChangeCounts(**self.counts))


_COMPACT_FROM = 1 << 16  # characters of an object from which what a comparison remembers of it goes in arrays


def _offsets(size: int) -> Callable[..., MutableSequence[int]]:
    """What holds the offsets a comparison remembers about an object of `size` characters: `array("q")` (8 bytes an
    offset) for a big one, a list (faster to fill, 32-40 bytes an offset) for the many small ones."""
    return list if size < _COMPACT_FROM else partial(array, "q")


def _first_key(text: str, start: int) -> str | None:
    """The first key of the object at text[start]; None for `{}`."""
    return None if text[start + 1] == "}" else scanstring(text, start + 2)[0]


def _aligned_differences(
    e: str, es: int, s: str, ss: int, new: Callable[..., MutableSequence[int]]
) -> MutableSequence[int] | None:
    """When the objects at e[es] and s[ss] have the same keys in the same order (a document read from the server and
    edited, which is almost every save), the members whose values differ, flat in a sequence made by `new` of
    (edited key start, edited value start and end, server value start and end) and nothing else kept; else None. The
    server's form repeats no key, so the same keys in the same order means the edited object repeats none either."""
    differing = new()
    for edited, server in zip_longest(_members(e, es), _members(s, ss)):
        if edited is None or server is None or edited[0] != server[0]:
            return None
        if not _same_text(e, edited[2], edited[3], s, server[2], server[3]):
            differing.extend((edited[1], edited[2], edited[3], server[2], server[3]))
    return differing


def _compare_objects(e: str, es: int, ee: int, s: str, ss: int, steps: Steps, found: _Found) -> None:
    """Two objects that are not the same text: dropped duplicate keys, a different key set, a different key order, then
    their members' values."""
    new = _offsets(ee - es)
    if _first_key(e, es) == _first_key(s, ss):
        differing = _aligned_differences(e, es, s, ss, new)
        if differing is not None:
            for key_start, edited_start, edited_end, server_start, server_end in zip(*[iter(differing)] * 5):
                key = scanstring(e, key_start + 1)[0]
                _compare(e, edited_start, edited_end, s, server_start, server_end, (*steps, key), found)
            return
    rank_of: dict[str, int] = {}  # each distinct edited key, in first-occurrence order
    edited = new()  # its value's (start, end), by rank: the last one of a repeated key
    for key, _, start, end in _members(e, es):
        rank = rank_of.get(key)
        if rank is None:
            rank_of[key] = len(edited) // 2
            edited.extend((start, end))
        else:  # the server keeps the last value of a repeated key; the change's `after` is that later value
            found.add(
                (*steps, key),
                "duplicate_key",
                True,
                lambda: _value_texts(e, edited[2 * rank], edited[2 * rank + 1], e, start, end),
            )
            edited[2 * rank] = start
            edited[2 * rank + 1] = end
    seen = bytearray(len(rank_of))
    server = new()  # each server member: (edited rank, key start, value start, value end)
    server_only: list[str] = []
    server_only_count = 0
    for key, key_start, start, end in _members(s, ss):
        rank = rank_of.get(key)
        if rank is None:
            server_only_count += 1
            if server_only_count <= KEYS_SHOWN:
                server_only.append(key)
        else:
            seen[rank] = 1
            server.extend((rank, key_start, start, end))
    edited_only_count = seen.count(0)
    if server_only_count or edited_only_count:  # said as the keys on one side only
        found.add(
            steps,
            "other",
            True,
            lambda: (
                _keys((key for key, rank in rank_of.items() if not seen[rank]), edited_only_count),
                _keys(server_only, server_only_count),
            ),
        )
        return
    if server[0::4] != new(range(len(rank_of))):
        found.add(
            steps,
            "key_order",
            False,
            lambda: (
                _keys(rank_of, len(rank_of)),
                _keys((scanstring(s, key_start + 1)[0] for key_start in server[1::4]), len(server) // 4),
            ),
        )
    for rank, key_start, start, end in zip(*[iter(server)] * 4):
        edited_start, edited_end = edited[2 * rank], edited[2 * rank + 1]
        if not _same_text(e, edited_start, edited_end, s, start, end):
            key = scanstring(s, key_start + 1)[0]
            _compare(e, edited_start, edited_end, s, start, end, (*steps, key), found)


def _compare(e: str, es: int, ee: int, s: str, ss: int, se: int, steps: Steps, found: _Found) -> None:
    """The edited value e[es:ee] against the server's s[ss:se], both compact and not the same text."""
    first, server_first = e[es], s[ss]
    if first == "{" and server_first == "{":
        _compare_objects(e, es, ee, s, ss, steps, found)
    elif first == "[" and server_first == "[":
        edited_items, server_items = _elements(e, es), _elements(s, ss)
        for index, (a, b) in enumerate(zip_longest(edited_items, server_items)):
            if a is None or b is None:  # different lengths: counted to the end, said once
                left = index + (a is not None) + sum(1 for _ in edited_items)
                right = index + (b is not None) + sum(1 for _ in server_items)
                found.add(steps, "other", True, lambda: (_count(left), _count(right)))
                return
            if not _same_text(e, *a, s, *b):
                _compare(e, *a, s, *b, (*steps, index), found)
    elif first == '"' and server_first == '"':
        edited_value, server_value = scanstring(e, es + 1)[0], scanstring(s, ss + 1)[0]
        if edited_value != server_value:
            found.add(steps, "other", True, lambda: _value_texts(e, es, ee, s, ss, se))
    elif first in _NUMBER_START and server_first in _NUMBER_START:
        same = _same_value(e[es:ee], s[ss:se])
        found.add(steps, "number", not same, lambda: _value_texts(e, es, ee, s, ss, se))
    else:
        found.add(steps, "other", True, lambda: _value_texts(e, es, ee, s, ss, se))


def diff(edited: str, server: str) -> DocumentDiff:
    """What storing `edited` would change, given `server`, the server's form of it (`SELECT RAW $doc`, A2): key order,
    number spelling, dropped duplicate keys (a change's `before` is the dropped value, its `after` the later one: the
    server keeps the last), a different set of keys (the keys on one side only), anything else as `other`. Whitespace
    and string escape spelling are not changes. Both texts are valid JSON that `json_problem` accepted, and `server`
    repeats no key (it is the server's form); never builds a tree (ruling C3); RecursionError for nesting deeper than
    Python allows.

    Memory: offsets in compact arrays, nothing per member as an object, so the walk costs a small part of the text
    (see the note above `_members` for the shapes `value_end` itself makes costly). Time: every level reads its
    children again, so about the text's size times the levels a change is below (hence MAX_DOCUMENT_DEPTH)."""
    found = _Found()
    edited, server = one_line(edited), one_line(server)
    if edited != server:
        _compare(edited, 0, len(edited), server, 0, len(server), (), found)
    return found.result()
