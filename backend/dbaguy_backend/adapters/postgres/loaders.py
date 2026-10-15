from psycopg import postgres
from psycopg.adapt import AdaptersMap
from psycopg.types.string import TextLoader

from ...domain.execution import ColumnCategory

# Decoded natively by psycopg; every other type is loaded as the server's text representation,
# which keeps big numbers exact, keeps JSON text intact and survives values Python cannot
# represent (infinity, BC dates).
_NATIVE = frozenset({"bool", "int2", "int4", "float4", "float8", "bytea", "text", "varchar", "bpchar", "name", "char"})

_NAME_CATEGORY: dict[str, ColumnCategory] = {
    "bool": ColumnCategory.BOOL,
    **dict.fromkeys(("int2", "int4", "int8", "float4", "float8", "numeric", "oid", "money"), ColumnCategory.NUMBER),
    **dict.fromkeys(("date", "time", "timetz", "timestamp", "timestamptz", "interval"), ColumnCategory.DATETIME),
    "bytea": ColumnCategory.BINARY,
    **dict.fromkeys(("json", "jsonb"), ColumnCategory.JSON),
    **dict.fromkeys(("text", "varchar", "bpchar", "name", "char", "uuid", "xml"), ColumnCategory.TEXT),
}

_OID_CATEGORY: dict[int, ColumnCategory] = {
    info.oid: _NAME_CATEGORY[info.name] for info in postgres.types if info.name in _NAME_CATEGORY
}


def configure_user_loaders(adapters: AdaptersMap) -> None:
    """Register text loaders on a user-query cursor's adapters."""
    for info in postgres.types:
        if info.name not in _NATIVE:
            adapters.register_loader(info.oid, TextLoader)
        if info.array_oid:
            adapters.register_loader(info.array_oid, TextLoader)


def category_for_oid(oid: int) -> ColumnCategory:
    return _OID_CATEGORY.get(oid, ColumnCategory.OTHER)
