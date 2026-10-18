"""Field sampling (R-04-11): the field paths of a keyspace, derived in Python from documents read with a plain SELECT
(never INFER, which can overload the server). Pure: no I/O, no logging; nothing of a document is kept but its paths.

A sample row is `{"t": META(d).type, "d": the document}`. The type is what tells a binary document from a string one:
the query service shows a binary document as a string `"<binary (9 b)>"` (A24, ruling B16), so such a document counts
as kind `binary` at its root and nothing below.

A sample row is decoded once, whole (`parse_sample_row`), and its document goes to the collector as decoded: a row
decoded twice (once to split `t` from `d`, once more for the walk) would hold a second copy of a document that can be
20 MiB. `FieldCollector` walks each decoded document once. Numbers are never turned into Python numbers (a 5,000-digit
integer or `1e400` is just "number"). A path is `(key, …)` from the root, `None` for the elements of an array; a
document contributes each `(path, kind)` once however many elements of an array have it. Three caps keep a sample
bounded: paths go no deeper than FIELD_PATH_MAX_DEPTH segments, a path met after FIELD_PATHS_MAX others is left out,
and so is a key longer than FIELD_KEY_MAX_CHARS (with everything below it). A left-out path sets `paths_capped`."""

import json
from typing import Any

from ...domain.couchbase import FieldPath, Keyspace
from ...sql.dialects import keyspace_path

# The rows read for a sample stop after the row that takes their texts (in characters, as the `*` peek counts) past this.
SAMPLE_MAX_BYTES = 32 * 1024 * 1024
FIELD_PATH_MAX_DEPTH = 6
FIELD_PATHS_MAX = 2000
FIELD_KEY_MAX_CHARS = 1024  # 2,000 paths of 10 KB keys would make a 20 MiB sample, and as large a response

BINARY = "binary"  # `META(d).type` of a document that is not JSON (A24): the server answers `base64`
_BINARY_META_TYPE = "base64"


class _Number:
    """What stands for every number in a decoded document: its value is never read."""

    __slots__ = ()


_NUMBER = _Number()


def sample_statement(keyspace: Keyspace) -> str:
    """The sample's SELECT; `$n` is the number of documents (ruling B16: `META(d).type` tells binary from string)."""
    path = keyspace_path(keyspace.bucket, keyspace.scope, keyspace.collection)
    return f"SELECT META(d).type AS t, d FROM {path} AS d LIMIT $n"


def _number(_text: str) -> _Number:
    return _NUMBER


# parse_constant: NaN and Infinity are no JSON, but the server never sends them and a number is all they could be
_DECODER = json.JSONDecoder(parse_int=_number, parse_float=_number, parse_constant=_number)
_KINDS: dict[type, str] = {dict: "object", list: "array", str: "string", bool: "boolean", type(None): "null",
                           _Number: "number"}  # a decoded document holds nothing else (no int, no float)


def parse_sample_row(row_text: str) -> tuple[str, Any]:
    """`(META(d).type, the document)` of one sample row, decoded once with every number as one marker. ValueError (or
    RecursionError, for nesting beyond what the decoder takes) for a row that is not one."""
    row = _DECODER.decode(row_text)
    if type(row) is not dict or type(row.get("t")) is not str or "d" not in row:
        raise ValueError("not a sample row")
    return row["t"], row["d"]


class _PathStats:
    __slots__ = ("docs", "last_doc", "types")

    def __init__(self) -> None:
        self.docs = 0
        self.last_doc = 0  # the number of the last document counted in `docs`
        self.types: dict[str, int] = {}


class FieldCollector:
    """The field paths of the documents `add()`ed, in the order they were first seen."""

    def __init__(self) -> None:
        self.documents = 0
        self.paths_capped = False
        self._stats: dict[tuple[str | None, ...], _PathStats] = {}

    def add(self, meta_type: str, document_text: str) -> None:
        """One document as text: `meta_type` is its `META(d).type`, `document_text` its JSON as the server sent it.
        ValueError (or RecursionError) when the text is not JSON."""
        self.add_decoded(meta_type, _DECODER.decode(document_text))

    def add_decoded(self, meta_type: str, document: Any) -> None:
        """One document as `parse_sample_row` decoded it."""
        self.documents += 1
        seen: set[tuple[tuple[str | None, ...], str]] = set()
        if meta_type == _BINARY_META_TYPE:  # the document is a string `<binary (9 b)>`: nothing below the root
            self._record((), BINARY, seen)
        else:
            self._walk(document, (), seen)

    def _walk(self, value: Any, path: tuple[str | None, ...], seen: set[tuple[tuple[str | None, ...], str]]) -> None:
        kind = _KINDS[type(value)]
        self._record(path, kind, seen)
        if len(path) >= FIELD_PATH_MAX_DEPTH:
            return
        if kind == "object":
            for key, child in value.items():
                if len(key) > FIELD_KEY_MAX_CHARS:  # its path, and every path below it, is left out
                    self.paths_capped = True
                    continue
                self._walk(child, (*path, key), seen)
        elif kind == "array":
            element_path = (*path, None)
            for child in value:
                self._walk(child, element_path, seen)

    def _record(self, path: tuple[str | None, ...], kind: str, seen: set[tuple[tuple[str | None, ...], str]]) -> None:
        if (path, kind) in seen:
            return
        stats = self._stats.get(path)
        if stats is None:
            if len(self._stats) >= FIELD_PATHS_MAX:  # read now, not at import: a test lowers it (ruling B38)
                self.paths_capped = True
                return  # a dropped path is never kept per document either: `seen` stays within the cap
            stats = self._stats[path] = _PathStats()
        seen.add((path, kind))
        if stats.last_doc != self.documents:
            stats.docs += 1
            stats.last_doc = self.documents
        stats.types[kind] = stats.types.get(kind, 0) + 1

    def fields(self) -> list[FieldPath]:
        return [
            FieldPath(path=list(path), types=dict(stats.types), docs=stats.docs,
                      percent=round(stats.docs / self.documents * 100, 1))
            for path, stats in self._stats.items()
        ]
