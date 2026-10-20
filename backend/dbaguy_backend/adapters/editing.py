"""The engine-neutral rule of grid editing's target (spec ⑤ §3.2.1, R-05-2, ruling E4): which key identifies a row,
given the columns and key candidates an engine read from its catalog. Pure."""

from collections.abc import Sequence
from dataclasses import dataclass

from ..domain.edits import EditColumn, EditTarget
from ..domain.metadata import PathSegment

NO_KEY = "No primary key or unique key on NOT NULL columns: a row cannot be identified"
VIEWS = "Views are read-only here"
KEY_NOT_EXACT = "The key column {name} ({type}) cannot be matched exactly: a row cannot be identified"


@dataclass(frozen=True)
class KeyCandidate:
    name: str  # the index or constraint: ties between unique keys are broken by it
    columns: tuple[str, ...]  # in index order
    primary: bool


def read_only_relation(path: Sequence[PathSegment], reason: str) -> EditTarget:
    """Shown read-only, with the reason (not an error, spec §3.2.1), and nothing else read: a view or another object
    that is not a table, or a table the connection cannot write exactly (PostgreSQL without standard strings, G3)."""
    return EditTarget(path=list(path), editable=False, reason=reason)


def build_target(
    path: Sequence[PathSegment], columns: Sequence[EditColumn], candidates: Sequence[KeyCandidate], *,
    transactional: bool = True, backslash_escapes: bool = True,
) -> EditTarget:
    """The primary key, else the unique key on NOT NULL columns with the fewest columns (then by name): the first whose
    every column can be matched exactly (compare not `none`, ruling E4). A primary key may hold NULL (SQLite): such a
    row is refused by the preview (`key_incomplete`). Not editable, with the reason, when no candidate qualifies."""
    by_name = {column.name: column for column in columns}
    ordered = sorted(candidates, key=lambda key: (not key.primary, len(key.columns), key.name))
    usable = [
        key for key in ordered
        if key.columns and all(name in by_name for name in key.columns)
        and (key.primary or not any(by_name[name].nullable for name in key.columns))
    ]
    common = {"path": list(path), "columns": list(columns), "transactional": transactional,
              "backslash_escapes": backslash_escapes}
    for key in usable:
        if all(by_name[name].compare != "none" for name in key.columns):
            source = "primary_key" if key.primary else "unique"
            return EditTarget(editable=True, key=list(key.columns), key_source=source, **common)
    if usable:
        column = next(by_name[name] for name in usable[0].columns if by_name[name].compare == "none")
        return EditTarget(editable=False, reason=KEY_NOT_EXACT.format(name=column.name, type=column.type_name), **common)
    return EditTarget(editable=False, reason=NO_KEY, **common)
