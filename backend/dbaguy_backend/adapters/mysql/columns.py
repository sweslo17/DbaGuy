"""A MySQL relation's columns (spec ⑥ D-06-1): one information_schema query. Grid editing's kind and compare rules
(spec ⑤ §3.2.2) are made here, once, for editing and data tools alike."""

from mysql.connector.connection import MySQLConnection

from ...domain.edits import Compare, edit_kind
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..columns import GENERATED, TableColumn, not_copied
from ..families import SPATIAL_TYPES, mysql_family
from . import catalog
from .types import category_for_data_type

_FIELDS = """COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA, NUMERIC_PRECISION, NUMERIC_SCALE,
       CHARACTER_MAXIMUM_LENGTH, DATETIME_PRECISION"""  # column()'s arguments
_COLUMNS = f"""
SELECT {_FIELDS}
FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION
"""
_SCHEMA_COLUMNS = f"""
SELECT TABLE_NAME, {_FIELDS}
FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, ORDINAL_POSITION
"""
_GENERATED = ("VIRTUAL GENERATED", "STORED GENERATED")  # not DEFAULT_GENERATED: an expression default (ruling E15)
_NOT_EXACT = frozenset({"float", "double", "real", "vector", *SPATIAL_TYPES})
# The text protocol sends a FLOAT with 6 significant digits (0.1234567 arrives as 0.123457, another float32: pre-flight
# M7). Times 1E0 it is a DOUBLE, sent with every digit, and written back it is the same float32 again. Not `+ 0E0`, the
# pre-flight's text: -0 + 0 is 0, while -0 * 1 keeps the sign (probed, 8.4.11)
_FLOAT_READ = "({} * 1E0)"


def column(
    name: str, column_type: str, data_type: str, is_nullable: str, default: str | None, extra: str | None,
    precision: int | None, scale: int | None, length: int | None, fraction: int | None,
) -> TableColumn:
    """One row of _COLUMNS."""
    data_type, extra, lowered = data_type.lower(), (extra or "").upper(), column_type.lower()
    generated = any(word in extra for word in _GENERATED)
    if generated:
        reason: str | None = GENERATED
    elif data_type in SPATIAL_TYPES:
        reason = "geometry values are not edited here"
    elif data_type == "vector":
        reason = "vector values are not edited here"
    else:
        reason = None
    compare: Compare = "none" if data_type in _NOT_EXACT else "equals"
    sizes: dict = {}
    if data_type in ("decimal", "numeric"):
        sizes = {"precision": precision, "scale": scale}
    elif data_type in ("char", "varchar", "binary", "varbinary"):
        sizes = {"length": length}
    elif data_type in ("time", "datetime", "timestamp"):
        sizes = {"scale": fraction}
    return TableColumn(
        name=name, type_name=column_type, family=mysql_family(data_type, lowered, precision), nullable=is_nullable == "YES",
        has_default=default is not None or generated or "AUTO_INCREMENT" in extra or "DEFAULT_GENERATED" in extra,
        kind=edit_kind(category_for_data_type(data_type)), compare=compare, writable=reason is None,
        read_only_reason=reason, **sizes, identity="by_default" if "AUTO_INCREMENT" in extra else None,
        generated=generated, **(not_copied(GENERATED) if generated else {}), hidden="INVISIBLE" in extra,
        read_as=_FLOAT_READ if data_type == "float" else None,
    )


def read_columns(conn: MySQLConnection, schema: str, name: str) -> list[TableColumn]:
    return [column(*row) for row in catalog.query(conn, _COLUMNS, (schema, name))]


def schema_columns(conn: MySQLConnection, schema: str) -> dict[str, list[TableColumn]]:
    """Every relation's columns in `schema`, in one query (a dump's catalog: never one query per table)."""
    found: dict[str, list[TableColumn]] = {}
    for table, *row in catalog.query(conn, _SCHEMA_COLUMNS, (schema,)):
        found.setdefault(table, []).append(column(*row))
    return found


def table_columns(conn: MySQLConnection, path: list[PathSegment]) -> list[TableColumn]:
    """The columns of the table or view at `path`, INVISIBLE ones included (`hidden`); NotFound when it is gone."""
    schema, name = relation_from_path(path)
    found = read_columns(conn, schema or "", name)
    if not found:
        raise NotFound(f"Table {schema}.{name} not found")
    return found
