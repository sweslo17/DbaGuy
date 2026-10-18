"""Result rows as DbaGuy shows them (spec §3.5): columns from the query service's `signature`, `*` and alias expansion
from the first page, the overflow column, and cells that keep the server's exact numbers. Pure: no I/O, no logging.

`build_shape()` turns the signature (and, for a `*`, the first page of rows) into a `RowShape`: the columns, and
`cells(row)` for every row after. The columns never change once built; whatever they do not show goes into the overflow
column `(other fields)` whole, so no key and no value is lost. Reading the first page belongs to the result object
(Task 11): it reads up to `STAR_PEEK_ROWS` rows / `STAR_PEEK_BYTES` (at least one row), builds the shape from them, and
delivers those same rows first when the grid asks for rows.

Cells follow spec §3.2.4: strings, true, false and null as decoded; a number as a Python number only when its literal
reads back exactly (`1.10`, `-0`, `1E+3`, `1e400` and integers past ±(2^53−1) stay the server's text, as `JsonText`);
objects and arrays as `JsonText` of the exact text; a key the row does not have as `MISSING` (not NULL).

A JSON string can hold a lone surrogate (`"\\ud800"`), which decodes to a Python str that no UTF-8 encoder takes (the
WebSocket send would fail). Such a string value becomes `JsonText` of the server's literal (the escape is kept); in a
column name or an overflow key each lone surrogate is written as its `\\uXXXX` escape. Nothing else is changed."""

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import Any

from dbaguy_backend.adapters.couchbase.stream import RawRow, RowField, json_kind, split_object
from dbaguy_backend.adapters.values import MAX_SAFE_INT, MISSING, JsonText
from dbaguy_backend.domain.column_category import ColumnCategory
from dbaguy_backend.domain.execution import ColumnMeta
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.lexer import INSIGNIFICANT, TokenKind, tokenize

STAR_PEEK_ROWS = 500
STAR_PEEK_BYTES = 8 * 1024 * 1024
OVERFLOW_NAME = "(other fields)"
VALUE_COLUMN = "value"
PLAN_COLUMN = "plan"

_STAR = "*"
_SIGNATURE_CATEGORY = {"number": ColumnCategory.NUMBER, "string": ColumnCategory.TEXT, "boolean": ColumnCategory.BOOL}
_INTEGER = re.compile(r"-?\d+")
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")  # a character from a \uD800-style escape: never half of a pair
_SAFE_DIGITS = 17  # ±(2^53 − 1) has 16 digits: longer literals are never converted (and int() has a digit limit)
# Words that end the projection of a SELECT at its own nesting level (RAW / ELEMENT / VALUE make the rows plain values).
_PROJECTION_ENDS = frozenset({"FROM", "LET", "WHERE", "GROUP", "ORDER", "LIMIT", "OFFSET", "UNION", "INTERSECT",
                              "EXCEPT", "RAW", "ELEMENT", "VALUE"})
_LATE_KEYS = "Some rows have keys outside the columns (from row {:,}); export as JSON to see every key"


def _has_lone_surrogate(text: str) -> bool:
    return not text.isascii() and _LONE_SURROGATE.search(text) is not None


def _escape_surrogates(text: str) -> str:
    """`text` with each lone surrogate written as the JSON escape `\\uXXXX`: text that any UTF-8 encoder takes."""
    if text.isascii():
        return text
    return _LONE_SURROGATE.sub(lambda match: f"\\u{ord(match.group()):04x}", text)


def value_cell(text: str, value: Any) -> Any:
    """The cell for one JSON value: `text` is the server's exact text, `value` the decoded value (only used for strings,
    true, false and null: numbers are decided from the literal, so a Python number never rounds a stored one). A string
    with a lone surrogate is kept as its literal, as `JsonText`."""
    kind = json_kind(text)
    if kind in ("object", "array"):
        return JsonText(text)
    if kind != "number":
        return JsonText(text) if kind == "string" and _has_lone_surrogate(value) else value
    if _INTEGER.fullmatch(text):
        if len(text) <= _SAFE_DIGITS and str(number := int(text)) == text and abs(number) <= MAX_SAFE_INT:
            return number
        return JsonText(text)
    as_float = float(text)  # a JSON number always parses; 1e400 gives inf, which never reads back as the text
    return as_float if repr(as_float) == text else JsonText(text)


def _cell(field: RowField | None) -> Any:
    return MISSING if field is None else value_cell(field.text, field.value)


def _key_text(key: str) -> str:
    return _escape_surrogates(json.dumps(key, ensure_ascii=False))


def _overflow(fields: Sequence[RowField]) -> Any:
    """Fields without a column as one object, keys in their order and values as sent; MISSING when there are none."""
    if not fields:
        return MISSING
    return JsonText("{" + ",".join(f"{_key_text(f.key)}:{f.text}" for f in fields) + "}")


def _signature_column(name: str, type_name: Any) -> ColumnMeta:
    text = type_name if isinstance(type_name, str) else json.dumps(type_name)
    return ColumnMeta(name=_escape_surrogates(name), type_name=_escape_surrogates(text),
                      category=_SIGNATURE_CATEGORY.get(text, ColumnCategory.MIXED))


def _unique(names: Iterable[str], taken: set[str]) -> list[str]:
    """Each name, with " 2", " 3"… when an earlier column (or `taken`) has it already; `taken` grows."""
    result = []
    for name in names:
        unique, n = name, 2
        while unique in taken:
            unique, n = f"{name} {n}", n + 1
        taken.add(unique)
        result.append(unique)
    return result


def _is_star(signature: Any) -> bool:
    """`SELECT *` is signed `{"*": "*"}`; a column the user named `*` (`SELECT 1 AS \\`*\\``) is `{"*": "number"}`."""
    return isinstance(signature, dict) and signature.get(_STAR) == _STAR


def needs_first_page(signature: Any, plan: bool) -> bool:
    """Only a `*` signature is peeked: its columns come from the rows (spec §3.5.1)."""
    return not plan and _is_star(signature)


def has_bare_star(sql: str) -> bool:
    """D-04-1, condition 1: the statement's outermost projection holds a `*` of its own (`SELECT *`, `SELECT a, *`), not
    `d.*`, `COUNT(*)`, a product or one inside a subquery. Read from the tokens, so strings, quoted names and comments
    never count."""
    depth, projecting, item_start = 0, False, False
    for token in tokenize(sql, Dialect.SQLPP):
        if token.kind in INSIGNIFICANT:
            continue
        if token.kind == TokenKind.RPAREN:
            depth = max(depth - 1, 0)
            continue
        if depth == 0:
            word = token.upper if token.kind == TokenKind.WORD else ""
            if word == "SELECT":
                projecting = item_start = True
                continue
            if projecting:
                if word in ("DISTINCT", "ALL") and item_start:
                    continue
                if word in _PROJECTION_ENDS:
                    projecting = False
                elif token.kind == TokenKind.COMMA:
                    item_start = True
                    continue
                elif token.kind == TokenKind.OTHER and token.text == _STAR and item_start:
                    return True
            item_start = False
        if token.kind == TokenKind.LPAREN:
            depth += 1
    return False


class RowShape(ABC):
    """The columns of one result and the cell list of each of its rows."""

    def __init__(self, columns: list[ColumnMeta]) -> None:
        self.columns = columns
        self._warning: str | None = None

    @abstractmethod
    def cells(self, row: RawRow) -> list[Any]:
        """One cell per column, in column order."""

    def take_warning(self) -> str | None:
        """The warning the rows so far raised, once: None afterwards (and for a shape that never warns)."""
        warning, self._warning = self._warning, None
        return warning


class _PlanShape(RowShape):
    """A plan (EXPLAIN, ADVISE): one column holding the whole row, whatever the signature says (A15)."""

    def __init__(self) -> None:
        super().__init__([ColumnMeta(name=PLAN_COLUMN, type_name="json", category=ColumnCategory.MIXED)])

    def cells(self, row: RawRow) -> list[Any]:
        return [JsonText(row.text)]


class _ValueShape(RowShape):
    """Rows that are plain values (`SELECT RAW …`, a string signature): one column `value`."""

    def __init__(self, signature: str) -> None:
        super().__init__([_signature_column(VALUE_COLUMN, signature)])

    def cells(self, row: RawRow) -> list[Any]:
        return [value_cell(row.text, row.value)]


class _ObjectShape(RowShape):
    """An object signature without `*`: its keys in order. It is never peeked and has no overflow column (P5): a row
    key outside the signature or a row that is not an object raises one warning, and the JSON export keeps them."""

    def __init__(self, signature: dict[str, Any]) -> None:
        super().__init__([_signature_column(name, type_name) for name, type_name in signature.items()])
        self._names = list(signature)
        self._known = frozenset(signature)
        self._rows = 0
        self._warned = False

    def _note_late(self) -> None:
        if not self._warned:
            self._warned = True
            self._warning = _LATE_KEYS.format(self._rows)

    def cells(self, row: RawRow) -> list[Any]:
        self._rows += 1
        if row.fields is None:
            self._note_late()
            return [MISSING] * len(self._names)
        found = {field.key: field for field in row.fields}
        if not found.keys() <= self._known:
            self._note_late()
        return [_cell(found.get(name)) for name in self._names]


def _alias_of(first_page: Sequence[RawRow], named: frozenset[str]) -> str | None:
    """D-04-1, condition 3 (R-04a-T8a): the one key every first-page row got from `*`. Each row must be an object whose
    `*` part (the fields the signature does not name) is exactly one field with that key; its value may be of any
    kind, which keeps JOIN, LEFT JOIN and UNION results out. None for an empty page and as soon as a row does not fit.
    Whether the alias object has members to show is the caller's question."""
    alias = None
    for row in first_page:
        if row.fields is None:
            return None
        brought = [field for field in row.fields if field.key not in named]
        if len(brought) != 1:
            return None
        if alias is None:
            alias = brought[0].key
        elif brought[0].key != alias:
            return None
    return alias


class _StarShape(RowShape):
    """A `*` in the signature: its names in order, `*` replaced by the keys the first page brought, then the overflow
    column. With an alias (D-04-1) the keys are those of the alias object: only when some first-page row's alias is an
    object with members (R-04a-T8a), else the alias is one column itself. The columns are fixed by the first page; a
    later row's other keys, a row that is not an object, and an alias that no longer fits go to the overflow column
    (the row's whole `*` part, as sent: `{"d":42}`)."""

    def __init__(self, signature: dict[str, Any], *, bare_star: bool, first_page: Sequence[RawRow]) -> None:
        names = list(signature)
        at = names.index(_STAR)
        self._before, self._after = names[:at], names[at + 1:]
        self._named = frozenset(names) - {_STAR}
        self._alias = _alias_of(first_page, self._named) if bare_star else None
        kinds = self._first_page_kinds(first_page)
        if self._alias is not None and not kinds:  # no alias object has a member to show: the alias is the column
            self._alias = None
            kinds = self._first_page_kinds(first_page)
        self._keys = list(kinds)
        self._key_set = frozenset(kinds)
        shown = [self._shown_name(key) for key in self._keys]
        taken = {_escape_surrogates(name) for name in self._named}
        columns = [_signature_column(name, signature[name]) for name in self._before]
        columns += [ColumnMeta(name=name, type_name="|".join(kinds[key]), category=ColumnCategory.MIXED)
                    for name, key in zip(_unique(shown, taken), self._keys)]
        columns += [_signature_column(name, signature[name]) for name in self._after]
        [overflow] = _unique([OVERFLOW_NAME], {column.name for column in columns})
        super().__init__([*columns, ColumnMeta(name=overflow, type_name="object", category=ColumnCategory.MIXED)])

    def _first_page_kinds(self, first_page: Sequence[RawRow]) -> dict[str, list[str]]:
        """Each key the first page shows in the `*` part, in first-appearance order, with its values' JSON kinds."""
        kinds: dict[str, list[str]] = {}
        for row in first_page:
            if row.fields is None:
                continue
            members, _ = self._star_part([field for field in row.fields if field.key not in self._named])
            for member in members:
                seen = kinds.setdefault(member.key, [])
                if (kind := json_kind(member.text)) not in seen:
                    seen.append(kind)
        return kinds

    def _shown_name(self, key: str) -> str:
        """The column name of an expanded key: `alias.key` when a projected name has it already (D-04-1)."""
        shown = f"{self._alias}.{key}" if self._alias is not None and key in self._named else key
        return _escape_surrogates(shown)

    def _star_part(self, brought: list[RowField]) -> tuple[list[RowField], list[RowField]]:
        """What `*` brought, as (the members the columns may show, the fields that stay whole): without an alias the
        fields themselves; with one, the alias object's members when that is all there is, else everything whole."""
        if self._alias is None:
            return brought, []
        if len(brought) == 1 and brought[0].key == self._alias and json_kind(brought[0].text) == "object":
            return list(split_object(brought[0].text)), []
        return [], brought

    def cells(self, row: RawRow) -> list[Any]:
        if row.fields is None:  # not an object: the whole row goes to the overflow column
            return [*([MISSING] * (len(self.columns) - 1)), JsonText(row.text)]
        named = {field.key: field for field in row.fields if field.key in self._named}
        members, extra = self._star_part([field for field in row.fields if field.key not in self._named])
        found: dict[str, RowField] = {}
        for member in members:
            if member.key in self._key_set and member.key not in found:
                found[member.key] = member
            else:
                extra.append(member)  # a key the first page did not show, or a key repeated within the row
        return [*(_cell(named.get(key)) for key in self._before), *(_cell(found.get(key)) for key in self._keys),
                *(_cell(named.get(key)) for key in self._after), _overflow(extra)]


def build_shape(signature: Any, *, plan: bool, bare_star: bool, first_page: Sequence[RawRow]) -> RowShape | None:
    """The shape of a result (spec §3.5.1); None when the response has no signature (DML without RETURNING, DDL).
    `first_page` is only read for a `*` signature (see `needs_first_page`); `bare_star` is `has_bare_star(sql)`."""
    if plan:
        return _PlanShape()
    if signature is None:
        return None
    if isinstance(signature, str):
        return _ValueShape(signature)
    if isinstance(signature, dict) and signature:
        if _is_star(signature):
            return _StarShape(signature, bare_star=bare_star, first_page=first_page)
        return _ObjectShape(signature)
    return _ValueShape(json.dumps(signature))  # nothing to name columns by: each row whole in one column
