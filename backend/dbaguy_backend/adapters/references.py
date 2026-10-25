"""Foreign keys between a target's tables (spec ⑥ R-06-9; 06c Task 2 fix round 4): what a copy that appends or replaces
rows orders its tables by, parents first, what a table's DELETE reaches in another table's rows (ON DELETE CASCADE,
SET NULL, SET DEFAULT), and what refuses that DELETE (NO ACTION, RESTRICT) while the referencing table holds rows (06c
final review I-2). PostgreSQL and SQL Server read them live (`base.ReferenceReading`, one catalog query); MySQL's and
SQLite's come from the schema snapshot, since the copy turns their checks, and with them their cascades, off."""

from dataclasses import dataclass
from typing import Literal

OnDelete = Literal["no_action", "restrict", "cascade", "set_null", "set_default"]
# What a parent row's DELETE does to the rows that reference it, besides refusing it
CHANGES_CHILDREN: frozenset[OnDelete] = frozenset({"cascade", "set_null", "set_default"})


@dataclass(frozen=True, slots=True)
class Reference:
    """One foreign key: the referencing (child) table and the referenced (parent) one, each by schema and name."""

    schema: str
    table: str
    ref_schema: str
    ref_table: str
    on_delete: OnDelete = "no_action"
    deferred: bool = False  # PostgreSQL's INITIALLY DEFERRED: checked at COMMIT, not as the DELETE runs
    # Whether the referencing table holds rows (read when a copy replaces rows, I-2); None: not read, or it may
    child_rows: bool | None = None

    @property
    def changes_children(self) -> bool:
        """A DELETE of a parent row deletes or changes the rows that reference it."""
        return self.on_delete in CHANGES_CHILDREN

    @property
    def refuses_delete(self) -> bool:
        """A DELETE of every row of the parent fails as it runs (I-2): the key only refuses (NO ACTION, RESTRICT), it is
        checked at once, and another table's rows (or may: not read) reference it. A table's key to itself does not:
        its own DELETE takes its referencing rows with it."""
        return (not self.changes_children and not self.deferred and self.child_rows is not False
                and (self.schema, self.table) != (self.ref_schema, self.ref_table))
