"""A relation's columns as data tools and grid editing read them (spec ⑥ D-06-1): what each engine's catalog says,
in one engine-neutral shape. Grid editing projects a column to its EditColumn (⑤'s rules, unchanged); a dump or a copy
reads which columns it writes, how the SELECT reads each one, and its type family (`TypeFamily`, the key of the
cross-engine type map). Pure."""

from dataclasses import dataclass
from typing import Literal

from ..domain.edits import Compare, EditColumn, EditKind

# The families of spec ⑥ §3.5.2: what a column holds, whatever its engine calls it. `composite` is a PostgreSQL row
# type; `variant` SQL Server's sql_variant and hierarchyid; `other` anything a copy writes as the engine's text.
TypeFamily = Literal[
    "bool", "int16", "int32", "int64", "uint64", "decimal", "money", "float32", "float64", "char", "varchar", "text",
    "binary", "date", "time", "duration", "year", "timestamp", "timestamp_tz", "time_tz", "interval", "uuid", "json",
    "xml", "enum", "set", "composite", "spatial", "variant", "rowversion", "other",
]
Identity = Literal["always", "by_default"]

GENERATED = "generated column"
ROWVERSION = "rowversion column"
SQL_VARIANT = "sql_variant column: only its raw bytes can be read"
PERIOD = "period column (system-versioned)"


@dataclass(frozen=True, slots=True)
class TableColumn:
    name: str
    type_name: str  # as declared: numeric(10,2), varchar(20), datetime2(7)
    family: TypeFamily
    nullable: bool
    has_default: bool  # a default, identity, auto_increment or generated value: an INSERT may leave it out
    kind: EditKind  # how a typed value is read (grid editing), and by default how a read value is written
    compare: Compare  # how grid editing checks an original value
    writable: bool  # grid editing may set it
    read_only_reason: str | None = None
    unicode: bool = True  # SQL Server: False for char / varchar / text (ASCII text then goes without N)
    precision: int | None = None  # decimal(p, s)
    scale: int | None = None  # decimal(p, s); the fractional digits of a time type
    length: int | None = None  # char / varchar / binary (n) in the engine's unit; None: no limit (text, max)
    identity: Identity | None = None  # always: written only with OVERRIDING SYSTEM VALUE / IDENTITY_INSERT
    generated: bool = False  # the server computes it (never written)
    copied: bool = True  # a dump or a copy writes it
    not_copied_reason: str | None = None  # why not: generated, rowversion, sql_variant, period column
    hidden: bool = False  # MySQL INVISIBLE: not in SELECT *, but a dump keeps its values
    # The expression a dump or copy SELECT reads it with, `{}` standing for the quoted column (PG money: CAST({} AS
    # numeric); SQL Server time(7): CONVERT(varchar(16), {}, 126)); None: the column itself
    read_as: str | None = None
    literal: EditKind | None = None  # how a value read that way is written; None: as `kind`
    # The column's type is the database's own (PostgreSQL: a domain, enum, row or extension type; SQL Server: an alias
    # type): a copy to another database cannot count on it, and maps it by its family (spec ⑥ R-06-10, 06c)
    custom_type: bool = False

    @property
    def value_kind(self) -> EditKind:
        """How a value the dump or copy SELECT read is written as a literal."""
        return self.literal or self.kind

    def edit_column(self) -> EditColumn:
        """The column as grid editing knows it (spec ⑤ §3.2.2)."""
        return EditColumn(
            name=self.name, type_name=self.type_name, kind=self.kind, nullable=self.nullable,
            has_default=self.has_default, writable=self.writable, read_only_reason=self.read_only_reason,
            compare=self.compare, unicode=self.unicode,
        )


def not_copied(reason: str) -> dict:
    """The fields of a column a dump or a copy leaves out (the server fills it, or it cannot be read back)."""
    return {"copied": False, "not_copied_reason": reason}
