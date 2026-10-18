"""The SQL Server schema snapshot behind completion: seven fixed queries, whatever the number of tables, sent as one
batch (one round trip).

A snapshot covers the database the connection is bound to (SQL Server catalogs are per database); other databases get
their own connection and snapshot. An account without VIEW DEFINITION sees fewer facts, never fewer columns: a column of
a user type it may not see keeps its base type, and a foreign key to a table it may not see is left out.
"""

from datetime import UTC, datetime

from loguru import logger

from ...domain.schema import SchemaSnapshot, SnapshotColumn, SnapshotForeignKey, SnapshotRelation, SnapshotSchema
from . import driver
from .catalog import batch, is_system_schema, result_sets, type_sql

_CURRENT = "SELECT DB_NAME(), SCHEMA_NAME()"
_SCHEMAS = "SELECT name, schema_id FROM sys.schemas"
_RELATIONS = (
    "SELECT object_id, SCHEMA_NAME(schema_id), name, type FROM sys.objects "
    "WHERE type IN ('U', 'V') AND is_ms_shipped = 0 ORDER BY 2, 3"
)
# LEFT JOIN: a column whose user type the account may not see keeps its base type (system_type_id) rather than vanish.
_COLUMNS = (
    "SELECT c.object_id, c.name, COALESCE(t.name, st.name, N'?'), c.max_length, c.precision, c.scale FROM sys.columns c "
    "JOIN sys.objects o ON o.object_id = c.object_id LEFT JOIN sys.types t ON t.user_type_id = c.user_type_id "
    "LEFT JOIN sys.types st ON st.user_type_id = c.system_type_id "
    "WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0 ORDER BY c.object_id, c.column_id"
)
_PRIMARY_KEYS = (
    "SELECT i.object_id, COL_NAME(k.object_id, k.column_id) FROM sys.indexes i "
    "JOIN sys.index_columns k ON k.object_id = i.object_id AND k.index_id = i.index_id "
    "JOIN sys.objects o ON o.object_id = i.object_id "
    "WHERE i.is_primary_key = 1 AND o.is_ms_shipped = 0 ORDER BY i.object_id, k.key_ordinal"
)
_FOREIGN_KEYS = """
SELECT f.parent_object_id, f.name, COL_NAME(fc.parent_object_id, fc.parent_column_id),
       OBJECT_SCHEMA_NAME(f.referenced_object_id), OBJECT_NAME(f.referenced_object_id),
       COL_NAME(fc.referenced_object_id, fc.referenced_column_id)
FROM sys.foreign_keys f JOIN sys.foreign_key_columns fc ON fc.constraint_object_id = f.object_id
ORDER BY f.parent_object_id, f.name, fc.constraint_column_id
"""
_FUNCTIONS = (
    "SELECT SCHEMA_NAME(schema_id), name FROM sys.objects "
    "WHERE type IN ('FN', 'IF', 'TF', 'FS', 'FT') AND is_ms_shipped = 0 ORDER BY 1, 2"
)
_QUERIES = (_CURRENT, _SCHEMAS, _RELATIONS, _COLUMNS, _PRIMARY_KEYS, _FOREIGN_KEYS, _FUNCTIONS)


def _relations(
    schemas: dict[str, SnapshotSchema], relation_rows: list[tuple], column_rows: list[tuple]
) -> dict[int, SnapshotRelation]:
    """Tables and views of the schemas that take any, with their columns, keyed by object id."""
    relations: dict[int, SnapshotRelation] = {}
    for object_id, schema, name, kind in relation_rows:
        if schema in schemas and not schemas[schema].system:  # system schemas get no relations
            relation = SnapshotRelation(name=name, kind="view" if kind.strip() == "V" else "table", columns=[])
            relations[object_id] = relation
            schemas[schema].relations.append(relation)
    for object_id, column, type_name, max_length, precision, scale in column_rows:
        if object_id in relations:  # a relation created after the list was read is skipped
            relations[object_id].columns.append(
                SnapshotColumn(name=column, type_name=type_sql(type_name, max_length, precision, scale))
            )
    return relations


def _fill_keys(relations: dict[int, SnapshotRelation], primary_key_rows: list[tuple], foreign_key_rows: list[tuple]) -> None:
    for object_id, column in primary_key_rows:
        if object_id in relations and column is not None:
            relations[object_id].primary_key.append(column)
    foreign_keys: dict[tuple, SnapshotForeignKey] = {}
    hidden: set[tuple] = set()  # a key that names something the account may not see (OBJECT_NAME is NULL): left out whole
    for parent_id, name, column, ref_schema, ref_table, ref_column in foreign_key_rows:
        relation = relations.get(parent_id)
        if relation is None:
            continue
        key = (parent_id, name)
        if None in (column, ref_schema, ref_table, ref_column):
            hidden.add(key)
            continue
        if key not in foreign_keys:
            foreign_keys[key] = SnapshotForeignKey(
                name=name, columns=[], ref_schema=ref_schema, ref_table=ref_table, ref_columns=[]
            )
            relation.foreign_keys.append(foreign_keys[key])
        foreign_keys[key].columns.append(column)
        foreign_keys[key].ref_columns.append(ref_column)
    for key in hidden:
        if key in foreign_keys:
            relations[key[0]].foreign_keys.remove(foreign_keys[key])


def build_snapshot(conn: driver.Connection) -> SchemaSnapshot:
    """Schema snapshot of the connection's database: seven queries in one batch, a fixed count however many tables."""
    current, schema_rows, relation_rows, column_rows, primary_key_rows, foreign_key_rows, function_rows = result_sets(
        conn, batch(*_QUERIES))
    database, default_schema = current[0]
    schemas = {name: SnapshotSchema(name=name, system=is_system_schema(name, schema_id)) for name, schema_id in schema_rows}
    relations = _relations(schemas, relation_rows, column_rows)
    _fill_keys(relations, primary_key_rows, foreign_key_rows)
    for schema, name in function_rows:
        if schema in schemas and not schemas[schema].system:
            schemas[schema].functions.append(name)
    search_path = [default_schema] if default_schema else []
    if default_schema != "dbo":
        search_path.append("dbo")  # an unqualified name that the user's own schema lacks still finds dbo
    logger.debug("SQL Server schema snapshot of {}: {} schemas, {} relations", database, len(schemas), len(relations))
    return SchemaSnapshot(
        database=database, default_schema=default_schema, search_path=search_path,
        schemas=list(schemas.values()), built_at=datetime.now(UTC),
    )
