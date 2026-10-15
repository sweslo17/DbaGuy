from enum import StrEnum

# A leaf module (no domain imports) so that `execution` and `formats` can both use it without an import cycle.


class ColumnCategory(StrEnum):
    NUMBER = "number"
    TEXT = "text"
    BOOL = "bool"
    DATETIME = "datetime"
    BINARY = "binary"
    JSON = "json"
    OTHER = "other"
