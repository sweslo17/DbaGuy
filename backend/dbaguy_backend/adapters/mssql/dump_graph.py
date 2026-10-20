"""How the objects of a SQL Server dump depend on each other (spec ⑥ §3.3.3), from sys.sql_expression_dependencies:
what must exist before what, what an object the dump leaves out takes out with it (review S1), and which defaults and
checks wait for the views. Pure: the reader (dump.py) gives it the catalog's rows.

When SQL Server needs what an object reads. A view, an inline function, a schema-bound module and a security policy
bind it when they are created (208, 4121 otherwise); a trigger runs it whenever it fires; a table needs the functions
of its computed columns, defaults and checks when it is created, and its checks and computed columns run them while
the rows go in. A procedure and a function that is neither inline nor schema-bound resolve names when they run
(deferred name resolution): they can be created before what they read, or without it."""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from ..dump import Ordering
from .catalog import q

FUNCTIONS = frozenset({"FN", "IF", "TF"})
RELATIONS = frozenset({"U", "V", "SN"})  # what a function may read rows from
_MODULES = FUNCTIONS | {"V", "P", "TR", "SN", "SP"}


@dataclass(frozen=True, slots=True)
class Object:
    object_id: int
    schema: str
    name: str
    type: str  # sys.objects.type, trimmed
    parent: int
    generated: bool = False  # a table with GENERATED ALWAYS columns (a period, a ledger's)
    schema_bound: bool = False

    @property
    def label(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def qualified(self) -> str:
        return f"{q(self.schema)}.{q(self.name)}"


@dataclass(frozen=True, slots=True)
class Use:
    """One row of sys.sql_expression_dependencies: `referencing` reads `referenced`. A default's or check's row is its
    table's, with the constraint's name and type; a computed column's is its table's, without one."""

    referencing: int
    referenced: int
    type: str  # the referenced object's
    schema: str | None
    name: str
    shipped: bool = False  # the referenced object is a system one
    constraint: str | None = None
    constraint_type: str | None = None  # C (check) or D (default)

    @property
    def label(self) -> str:
        return f"{self.schema}.{self.name}"


class Graph:
    def __init__(self, objects: dict[int, Object], uses: Iterable[Use], bases: dict[int, int] | None = None) -> None:
        """`bases`: synonym → the object it names, when that resolves."""
        self.objects = objects
        self.uses: dict[int, list[Use]] = {}
        self._referrers: dict[int, list[Use]] = {}
        self._labels: dict[int, str] = {object_id: obj.label for object_id, obj in objects.items()}
        for use in uses:
            self.uses.setdefault(use.referencing, []).append(use)
            self._referrers.setdefault(use.referenced, []).append(use)
            self._labels.setdefault(use.referenced, use.label)
        self._naming: dict[int, list[int]] = {}  # an object → the synonyms that name it
        for synonym, base in (bases or {}).items():
            self._naming.setdefault(base, []).append(synonym)
        self._reads: dict[int, bool] = {}

    def label(self, object_id: int) -> str:
        return self._labels.get(object_id, str(object_id))

    def binds(self, obj: Object) -> bool:
        """Whether `obj` needs what it reads when it is created (a view, an inline function, a schema-bound module, a
        security policy) or whenever it fires (a trigger)."""
        return obj.type in ("V", "IF", "SP", "TR") or (obj.type in ("FN", "TF") and obj.schema_bound)

    def spread(self, hidden: dict[int, str], tables: set[int]) -> dict[int, str]:
        """Review S1: what needs an object the dump leaves out (`hidden`: left out or unreadable) is left out with it,
        transitively — a view, an inline or schema-bound function that reads one; a synonym that names one; a trigger
        or a security policy that reaches one at all, through what it calls (it runs whenever its table is written or
        read); a table of `tables` whose computed column or check calls a function that reaches one (they run while
        the rows go in), or whose default calls one. `hidden` grows; returns the objects it gained → the label of what
        each needs."""
        found: dict[int, str] = {}
        reaches = set(hidden)  # what reaches a hidden object through any reference
        queue = deque(hidden)

        def hide(object_id: int, need: int) -> None:
            hidden[object_id] = "needs"
            found[object_id] = self.label(need)
            reaches.add(object_id)
            queue.append(object_id)

        while queue:
            current = queue.popleft()
            gone = current in hidden
            for use in self._referrers.get(current, ()):
                referencing = use.referencing
                obj = self.objects.get(referencing)
                if obj is None or referencing in hidden:
                    continue
                if obj.type == "U":
                    if referencing in tables and (gone or use.constraint_type != "D"):
                        hide(referencing, current)
                elif obj.type in ("TR", "SP") or (gone and self.binds(obj)):
                    hide(referencing, current)
                elif obj.type in _MODULES and referencing not in reaches:
                    reaches.add(referencing)
                    queue.append(referencing)
            if gone:
                for synonym in self._naming.get(current, ()):
                    if synonym not in hidden:
                        hide(synonym, current)
        return found

    def reads(self, function: int) -> bool:
        """Whether a function reads a table, view or synonym, itself or through the functions it calls."""
        if function in self._reads:
            return self._reads[function]
        self._reads[function] = False  # a cycle reads nothing more
        found = any(use.type in RELATIONS or (use.type in FUNCTIONS and self.reads(use.referenced))
                    for use in self.uses.get(function, ()))
        self._reads[function] = found
        return found

    def later(self, tables: Iterable[int]) -> dict[int, frozenset[str]]:
        """The defaults and checks of each table whose function reads a table, view or synonym (review S1): added
        after the views, with the rows in (WITH CHECK), not with the table — where its rows would be checked against
        lookups not yet loaded (547) or views not yet made (208)."""
        found = {}
        for table in tables:  # a constraint's row on its own table is a column it names, not a read
            names = {use.constraint for use in self.uses.get(table, ()) if use.constraint is not None
                     and use.referenced != table
                     and (use.type in RELATIONS or (use.type in FUNCTIONS and self.reads(use.referenced)))}
            if names:
                found[table] = frozenset(names)
        return found

    def early(self, tables: set[int], later: dict[int, frozenset[str]]) -> set[int]:
        """What must exist before the tables (S7): the functions their computed columns and their defaults and checks
        written with them call, the functions those call, and the views and synonyms an inline or schema-bound one (or
        a view among them) binds (review S1)."""
        todo = [use.referenced for table in tables for use in self.uses.get(table, ())
                if use.type in FUNCTIONS and (use.constraint is None or use.constraint not in later.get(table, ()))]
        found: set[int] = set()
        while todo:
            current = todo.pop()
            if current in found or current not in self.objects:
                continue
            found.add(current)
            obj = self.objects[current]
            for use in self.uses.get(current, ()):
                if use.type in FUNCTIONS or (use.type in ("V", "SN") and self.binds(obj)):
                    todo.append(use.referenced)
        return found

    def closure(self, roots: set[int], kinds: frozenset[str], skip: set[int]) -> set[int]:
        """The scope's objects of `kinds` that `roots` read, and what those read, transitively; `roots` and `skip`
        excluded (and not gone through)."""
        found: set[int] = set()
        todo = list(roots)
        while todo:
            for use in self.uses.get(todo.pop(), ()):
                referenced = use.referenced
                if (use.type in kinds and referenced in self.objects and referenced not in found
                        and referenced not in roots and referenced not in skip):
                    found.add(referenced)
                    todo.append(referenced)
        return found

    def used_by(self, referencing: Iterable[int]) -> set[int]:
        return {use.referenced for object_id in referencing for use in self.uses.get(object_id, ())}

    def order(self, keys: list[int]) -> list[int]:
        """`keys` (creation order) by their dependencies on each other."""
        wanted = set(keys)
        ordering = Ordering()
        for key in keys:
            ordering.depends[str(key)] = {str(use.referenced) for use in self.uses.get(key, ())
                                          if use.referenced in wanted}
        return [int(key) for key in ordering.order([str(key) for key in keys])]

    def needs(self, written: set[int], hidden: dict[int, str], position: dict[int, int]) -> list[str]:
        """`a needs b` for what the written objects read but the dump does not write: what they bind (from other
        schemas, not chosen), and anything the dump leaves out — the file needs it where it is run."""
        found = []
        for referencing in sorted(written, key=position.__getitem__):
            obj = self.objects[referencing]
            for use in self.uses.get(referencing, ()):
                binds = use.constraint is not None or obj.type == "U" or self.binds(obj)
                if not use.shipped and use.referenced not in written and (binds or use.referenced in hidden):
                    found.append(f"{obj.label} needs {use.label}")
        return found
