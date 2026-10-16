from enum import StrEnum


class Dialect(StrEnum):
    """The SQL dialect of an engine: picks the splitting, classification, completion and quoting rules in `sql/`."""

    POSTGRES = "postgres"
    SQLITE = "sqlite"
