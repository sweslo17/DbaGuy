"""A previewed copy as its job runs it (spec ⑥ D-06-8): what the preview showed — each table's SELECT, the target's
statements and which source column goes where — kept once, for 15 minutes, in a one-time plan store. The copy job takes
it by id on the target's session; nothing it sends is chosen after the preview, except its INSERTs' literals, which
follow the target session's own settings (§3.5.5 step 1)."""

from dataclasses import dataclass
from uuid import UUID

from ..adapters.columns import TableColumn
from ..domain.edits import MAX_PLAN_CHARS
from ..domain.metadata import PathSegment
from ..domain.transfer import COPY_PLAN_EXPIRED
from .plans import OneTimePlanStore

PLAN_TOO_LARGE = (f"The copy's statements are too large to keep for it: at most {MAX_PLAN_CHARS // 1_048_576} MiB of "
                  "text. Copy fewer tables at once")


@dataclass(frozen=True)
class PlannedColumn:
    """A column the copy writes: the source column (its place in the SELECT's columns) and the target's by name."""

    source: int
    target: str


@dataclass(frozen=True)
class PlannedTable:
    source_name: str  # as messages name it: schema.name, or the query's title
    target_name: str  # schema.name on the target
    target_schema: str | None
    target_table: str
    target_path: tuple[PathSegment, ...]  # the target relation's tree path (its columns are read again after CREATE)
    select: str  # what the source runs (a query: the tab's statement, as it is)
    columns: tuple[TableColumn, ...]  # what `select` reads, in its order
    written: tuple[PlannedColumn, ...]
    before: tuple[str, ...]  # inside the table's transaction: CREATE TABLE (but MySQL's), DELETE, IDENTITY_INSERT ON
    after: tuple[str, ...]  # before its COMMIT: setval, IDENTITY_INSERT OFF
    created: bool  # the copy creates the table
    create_first: str | None = None  # MySQL: its CREATE TABLE, sent before the transaction (it commits at once)
    transactional: bool = True  # False: a MySQL table that cannot roll back (MyISAM, ⑤ G10)
    filtered: bool = False  # the SELECT has the table-data tab's filter: it is checked to be a plain read first

    @property
    def statements(self) -> tuple[str, ...]:
        """The statements the copy adds around the rows, in order (the History record's)."""
        return ((self.create_first,) if self.create_first else ()) + self.before + self.after


@dataclass(frozen=True)
class StoredTransfer:
    plan_id: str
    profile_id: UUID  # the target's: the plan store hands it to that profile's session only
    database: str | None  # the target's database where the engine connects per database (PostgreSQL)
    source_profile_id: UUID
    source_database: str | None
    query: bool  # the source is a query tab's result: the copy reads it on that tab's session
    tables: tuple[PlannedTable, ...]
    summary: str  # the History record's text
    title: str  # the job list's: `Copy public.orders from Local PG to Dev MySQL`


def stored_chars(plan: StoredTransfer) -> int:
    """What a stored copy holds that grows with it: its SELECTs and statements."""
    return len(plan.summary) + sum(len(table.select) + sum(map(len, table.statements)) for table in plan.tables)


class TransferPlanStore(OneTimePlanStore[StoredTransfer]):
    def __init__(self, **options: object) -> None:
        super().__init__(stored_chars, PLAN_TOO_LARGE, what="copy plan", expired=COPY_PLAN_EXPIRED,
                         **options)  # type: ignore[arg-type]
