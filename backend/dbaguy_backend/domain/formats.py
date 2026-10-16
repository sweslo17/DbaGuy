from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from .column_category import ColumnCategory


class FormatKind(StrEnum):
    TSV = "tsv"
    CSV = "csv"
    JSON = "json"
    JSONL = "jsonl"
    MARKDOWN = "markdown"
    INSERT = "insert"
    IN_LIST = "in_list"


class FormatColumn(BaseModel):
    name: str
    category: ColumnCategory


class RowFormatOptions(BaseModel):
    """Shared by copy (`/sql/format-rows`) and export (WS `export`)."""

    model_config = ConfigDict(extra="forbid")

    header: bool = True
    null_text: str = ""
    table_schema: str | None = None  # INSERT target; the name falls back to a `table_name` placeholder
    table_name: str | None = None
