"""What a dump reads from an engine's catalog (spec ⑥ §3.3, D-06-1), in one engine-neutral shape: the objects in the
order they must be created — before the data and after it — and the tables whose rows are written. Each engine's
`dump.py` knows its own catalog and ordering rules; `sql/dump_script.py` writes the file. Pure."""

import heapq
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from .columns import TableColumn

Consistency = Literal["snapshot", "per_table"]
Section = Literal["schema", "data"]
OWNERS_AND_PRIVILEGES = "owners and privileges"  # never dumped (R-06-2)


def kept_foreign_key(table: str, target: str, key: str | None = None) -> str:
    """The warning for a foreign key its table's DDL keeps (MySQL, SQLite: the checks are off while the file runs)
    although the table it references is not in the dump."""
    which = f"Foreign key {key} of {table}" if key else f"A foreign key of {table}"
    return (f"{which} references {target}, which is not in this dump: the file keeps it, and needs that table where "
            "it is run")


@dataclass(frozen=True)
class DumpObject:
    """One object's DDL: its statements in order (no terminators), the DROP … IF EXISTS a `drop_first` dump writes
    for it, and which part of the dump it belongs to — `schema` objects need the structure, `data` ones (PostgreSQL's
    setval) the rows."""

    kind: str  # schema, extension, type, sequence, function, procedure, table, view, materialized_view, index, …
    name: str  # as the file names it: schema.name (MySQL, SQLite: name)
    statements: tuple[str, ...]
    drop: str | None = None
    section: Section = "schema"
    compound: bool = False  # MySQL: a stored program, written between DELIMITER lines


@dataclass(frozen=True)
class DumpTable:
    """A table whose rows the dump writes."""

    schema: str | None
    table: str
    name: str  # as the file names it
    columns: tuple[TableColumn, ...]
    rows_estimate: int | None = None  # the catalog's estimate (≈): progress only
    bytes_estimate: int | None = None  # its size on disk: the free-space check (CR-4)
    only: bool = False  # PostgreSQL: FROM ONLY (an inheritance parent's own rows)
    # The `name`s of the other tables of this dump that its foreign keys reference: a data-only dump writes those
    # first (pre-flight M9), since its file restores into a schema whose foreign keys are already there
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class DumpCatalog:
    server: str  # `PostgreSQL 17.11`, as ServerInfo writes it
    scope: str  # `database dbaguy · schema public`: the header's text
    consistency: Consistency
    pre: tuple[DumpObject, ...] = ()  # before the data, in creation order
    tables: tuple[DumpTable, ...] = ()  # whose rows are written, in this order
    post: tuple[DumpObject, ...] = ()  # after the data, in creation order
    not_included: tuple[str, ...] = (OWNERS_AND_PRIVILEGES,)  # what the header lists as left out
    warnings: tuple[str, ...] = ()
    snapshot_text: str = "one consistent snapshot"  # the header's Snapshot line


@dataclass(frozen=True)
class DumpSession:
    """The statements a dump's own session sends before it reads (output settings, the snapshot) and after."""

    setup: tuple[str, ...]
    teardown: tuple[str, ...]
    consistency: Consistency


@dataclass
class Ordering:
    """Objects in dependency order: those whose dependencies are placed first, the rest in the catalog's own creation
    order (`key`) after them — a cycle, or a dependency the catalog does not record (dynamic SQL), never stops a dump
    (§3.3.3: "逐句＋跳過並繼續" completes the rest)."""

    depends: dict[str, set[str]] = field(default_factory=dict)

    def order(self, keys: list[str]) -> list[str]:
        """`keys` in creation order, each moved only as far down as one of its dependencies forces: the first key
        whose dependencies are all placed goes next; when none is ready (a cycle), the first one does. A dependency on
        a key not in `keys`, or on itself, is no dependency. O((keys + edges) log keys) (pre-flight L15)."""
        index = {key: i for i, key in enumerate(keys)}
        waiting = {key: {d for d in self.depends.get(key, ()) if d in index and d != key} for key in keys}
        dependents: dict[str, list[str]] = defaultdict(list)
        for key, deps in waiting.items():
            for dep in deps:
                dependents[dep].append(key)
        ready = [index[key] for key in keys if not waiting[key]]
        heapq.heapify(ready)
        placed: list[str] = []
        done: set[str] = set()
        first = 0  # the first key not yet placed: the one a cycle lets through
        while len(placed) < len(keys):
            if ready:
                key = keys[heapq.heappop(ready)]
                if key in done:
                    continue
            else:
                while keys[first] in done:
                    first += 1
                key = keys[first]
            done.add(key)
            placed.append(key)
            for dependent in dependents[key]:
                deps = waiting[dependent]
                deps.discard(key)
                if not deps and dependent not in done:
                    heapq.heappush(ready, index[dependent])
        return placed
