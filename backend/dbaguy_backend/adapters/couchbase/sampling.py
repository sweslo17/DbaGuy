"""Field sampling (R-04-11): the field paths of a keyspace, derived in Python from documents read with a plain SELECT
(never INFER, which can overload the server). Pure: no I/O, no logging; nothing of a document is kept but its paths.

A sample row is `{"t": META(d).type, "d": the document}`. The type is what tells a binary document from a string one:
the query service shows a binary document as a string `"<binary (9 b)>"` (A24, ruling B16), so such a document counts
as kind `binary` at its root and nothing below.

`FieldCollector` walks each decoded document once. Numbers are never turned into Python numbers (a 5,000-digit integer
or `1e400` is just "number"). A path is `(key, …)` from the root, `None` for the elements of an array; a document
contributes each `(path, kind)` once however many elements of an array have it. Both caps keep a sample bounded: paths
go no deeper than FIELD_PATH_MAX_DEPTH segments, and a path met after FIELD_PATHS_MAX others is left out."""

import json
from typing import Any

from ...domain.couchbase import FieldPath, Keyspace
from ...sql.dialects import keyspace_path
from .stream import json_kind, split_object

# The rows read for a sample stop after the row that takes their texts (in characters, as the `*` peek counts) past this.
SAMPLE_MAX_BYTES = 32 * 1024 * 1024
FIELD_PATH_MAX_DEPTH = 6
FIELD_PATHS_MAX = 2000

BINARY = "binary"  # `META(d).type` of a document that is not JSON (A24): the server answers `base64`
_BINARY_META_TYPE = "base64"
_NUMBER = object()  # what stands for every number in a decoded document


def sample_statement(keyspace: Keyspace) -> str:
    """The sample's SELECT; `$n` is the number of documents (ruling B16: `META(d).type` tells binary from string)."""
    path = keyspace_path(keyspace.bucket, keyspace.scope, keyspace.collection)
    return f"SELECT META(d).type AS t, d FROM {path} AS d LIMIT $n"


def split_sample_row(row_text: str) -> tuple[str, str]:
    """`(META(d).type, the document's text)` of one sample row. ValueError for a row that is not one."""
    members = {member.key: member for member in split_object(row_text)}
    meta_type, document = members.get("t"), members.get("d")
    # a number's `value` is its text too (stream.py keeps numbers as text): only the text tells a string from a number
    if meta_type is None or document is None or json_kind(meta_type.text) != "string":
        raise ValueError("not a sample row")
    return meta_type.value, document.text


def _number(_text: str) -> object:
    return _NUMBER


# parse_constant: NaN and Infinity are no JSON, but the server never sends them and a number is all they could be
_DECODER = json.JSONDecoder(parse_int=_number, parse_float=_number, parse_constant=_number)


def _kind(value: Any) -> str:
    if value is _NUMBER:
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "null" if value is None else "boolean"  # a number is never a Python number here


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
        """One document: `meta_type` is its `META(d).type`, `document_text` its JSON as the server sent it."""
        self.documents += 1
        seen: set[tuple[tuple[str | None, ...], str]] = set()
        if meta_type == _BINARY_META_TYPE:
            self._record((), BINARY, seen)
            return
        try:
            document = _DECODER.decode(document_text)
        except (ValueError, RecursionError):  # the server sent it, so only absurd nesting can get here: the root alone
            self._record((), json_kind(document_text), seen)
            return
        self._walk(document, (), seen)

    def _walk(self, value: Any, path: tuple[str | None, ...], seen: set[tuple[tuple[str | None, ...], str]]) -> None:
        kind = _kind(value)
        self._record(path, kind, seen)
        if len(path) >= FIELD_PATH_MAX_DEPTH:
            return
        if kind == "object":
            for key, child in value.items():
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
