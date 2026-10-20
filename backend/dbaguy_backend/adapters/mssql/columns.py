"""A SQL Server relation's columns (spec ⑥ D-06-1), from catalog-view joins (never COL_NAME / OBJECT_NAME). An alias
type is read through its base type; a CLR type (hierarchyid, geometry, geography) has no base row. Grid editing's kind
and compare rules (spec ⑤ §3.2.2) are made here, once, for editing and data tools alike."""

from ...domain.edits import edit_kind
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..columns import GENERATED, PERIOD, ROWVERSION, SQL_VARIANT, TableColumn, TypeFamily, not_copied
from . import driver
from .catalog import (
    CHARS, FRACTION, NCHARS, PerObject, by_object, char_length, id_sources, object_id_sql, rows, type_sql)
from .types import category_for_type

COLUMNS = PerObject("""
SELECT {key}c.name, t.name, COALESCE(st.name, t.name), t.is_assembly_type, c.max_length, c.precision, c.scale,
       c.is_nullable, c.is_identity, c.is_computed, CASE WHEN c.default_object_id <> 0 THEN 1 ELSE 0 END,
       c.generated_always_type, c.is_column_set
FROM sys.columns c
JOIN sys.types t ON t.user_type_id = c.user_type_id
LEFT JOIN sys.types st ON st.user_type_id = c.system_type_id AND st.is_user_defined = 0
WHERE {where} ORDER BY {order}c.column_id
""", "c.object_id")
# A table's column set (`xml COLUMN_SET FOR ALL_SPARSE_COLUMNS`) is its sparse columns' values seen as one XML: a copy
# writes the sparse columns themselves, and an INSERT may not name both (360)
COLUMN_SET = "column set: its sparse columns hold its values"
# 402, sql_variant: 0 rows; json and vector (2025) have no = (G9)
_NOT_EXACT = frozenset({"float", "real", "text", "ntext", "image", "xml", "sql_variant", "json", "vector"})
_NON_UNICODE = frozenset({"char", "varchar", "text"})
# Scale 7 as text (A S7: the driver only reads microseconds; the text keeps the 7th digit and writes back equal). Style
# 126 (ISO 8601): `2026-10-05T12:34:56.1234567+08:00`. CAST's own `… 12:34:56.1234567 +08:00` cannot be written back
# with a `T` (241, A15), and without it a datetime would read by the login's DATEFORMAT (F1)
_SEVENTH_DIGIT = {"datetime2": "CONVERT(varchar(27), {}, 126)", "datetimeoffset": "CONVERT(varchar(34), {}, 126)",
                  "time": "CONVERT(varchar(16), {}, 126)"}
_FAMILIES: dict[str, TypeFamily] = {
    "bit": "bool", "tinyint": "int16", "smallint": "int16", "int": "int32", "bigint": "int64", "decimal": "decimal",
    "numeric": "decimal", "money": "money", "smallmoney": "money", "real": "float32", "char": "char", "nchar": "char",
    "varchar": "varchar", "nvarchar": "varchar", "text": "text", "ntext": "text", "binary": "binary",
    "varbinary": "binary", "image": "binary", "date": "date", "time": "time", "datetime": "timestamp",
    "smalldatetime": "timestamp", "datetime2": "timestamp", "datetimeoffset": "timestamp_tz",
    "uniqueidentifier": "uuid", "xml": "xml", "json": "json", "timestamp": "rowversion", "sql_variant": "variant",
    "hierarchyid": "variant", "geometry": "spatial", "geography": "spatial",
}


def _family(base: str, max_length: int, precision: int) -> TypeFamily:
    if base == "float":
        return "float64" if precision > 24 else "float32"
    if base in ("varchar", "nvarchar") and max_length == -1:
        return "text"  # (max)
    return _FAMILIES.get(base, "other")


def _sizes(base: str, max_length: int, precision: int, scale: int) -> dict:
    if base in ("decimal", "numeric"):
        return {"precision": precision, "scale": scale}
    if base in CHARS or base in NCHARS:
        length = char_length(base, max_length)
        return {} if length is None else {"length": length}
    if base in FRACTION:
        return {"scale": scale}
    return {}


def column(
    name: str, type_name: str, base: str, assembly: bool, max_length: int, precision: int, scale: int,
    nullable: bool, identity: bool, computed: bool, default: int, generated_always: int, column_set: bool = False,
) -> TableColumn:
    """One row of COLUMNS."""
    if computed:
        reason: str | None = "computed column"
    elif identity:
        reason = "IDENTITY column"
    elif base == "timestamp":
        reason = ROWVERSION
    elif generated_always:
        reason = PERIOD
    elif base == "sql_variant" or assembly:
        reason = f"{type_name} values are not edited here"
    else:
        reason = None
    exact = not (base in _NOT_EXACT or assembly or (base in FRACTION and scale > 6))
    skipped = (GENERATED if computed else ROWVERSION if base == "timestamp" else PERIOD if generated_always
               else SQL_VARIANT if base == "sql_variant" else COLUMN_SET if column_set else None)
    if base in _SEVENTH_DIGIT and scale > 6:
        read_as, literal = _SEVENTH_DIGIT[base], "datetime"
    elif assembly:
        read_as, literal = "CAST({} AS varbinary(max))", "binary"  # A S6: written back as 0x… (A1: hierarchyid too)
    else:
        read_as, literal = None, None
    family = _family(base, max_length, precision)
    return TableColumn(
        name=name, type_name=type_sql(type_name, max_length, precision, scale), family=family, nullable=bool(nullable),
        has_default=bool(default or identity or computed or generated_always) or base == "timestamp",
        kind=edit_kind(category_for_type(base)), compare="equals" if exact else "none", writable=reason is None,
        read_only_reason=reason, unicode=base not in _NON_UNICODE, **_sizes(base, max_length, precision, scale),
        identity="always" if identity else None, generated=bool(computed or generated_always),
        **(not_copied(skipped) if skipped else {}), read_as=read_as, literal=literal,
    )


def read_columns(conn: driver.Connection, object_sql: str) -> list[TableColumn]:
    """`object_sql`: an expression for the object's id (`object_id_sql`)."""
    return [column(*row) for row in rows(conn, COLUMNS.one(object_sql))]


def columns_of(conn: driver.Connection, ids: list[int], within: str | None = None) -> dict[int, list[TableColumn]]:
    """The columns of many relations, by object id (a dump, Q1): one query per ID_CHUNK of them, or one over `within`
    (catalog.id_sources). A relation that is gone has no entry."""
    wanted = set(ids)
    found: dict[int, list[TableColumn]] = {}
    for source in id_sources(ids, within):
        for object_id, parts in by_object(rows(conn, COLUMNS.many(source))).items():
            if object_id in wanted:
                found[object_id] = [column(*row) for row in parts]
    return found


def table_columns(conn: driver.Connection, path: list[PathSegment]) -> list[TableColumn]:
    """The columns of the table or view at `path`; NotFound when it is gone."""
    schema, name = relation_from_path(path)
    found = read_columns(conn, object_id_sql(schema or "dbo", name))
    if not found:
        raise NotFound(f"Table {schema}.{name} not found")
    return found
