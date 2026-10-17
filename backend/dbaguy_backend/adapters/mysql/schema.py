"""The MySQL schema snapshot behind completion: seven fixed queries, whatever the number of tables."""

from datetime import UTC, datetime

from loguru import logger
from mysql.connector.connection import MySQLConnection

from ...domain.schema import SchemaSnapshot, SnapshotColumn, SnapshotForeignKey, SnapshotRelation, SnapshotSchema
from ...sql.dialects import Dialect, quote_literal
from . import catalog

# Over this many tables and views on the server, only the current database's relations are loaded (information_schema
# gets slow on a server with tens of thousands of tables).
RELATION_LIMIT = 10_000

_SYSTEM_LIST = "(" + ", ".join(quote_literal(Dialect.MYSQL, name) for name in sorted(catalog.SYSTEM_SCHEMAS)) + ")"
_COUNT = "SELECT COUNT(*) FROM information_schema.TABLES WHERE {scope}"
_TABLES = (
    "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE {scope} "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME"
)
_COLUMNS = (
    "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS WHERE {scope} "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
)
_KEYS = (
    "SELECT TABLE_SCHEMA, TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME, "
    "REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
    "WHERE {scope} AND (CONSTRAINT_NAME = 'PRIMARY' OR REFERENCED_TABLE_NAME IS NOT NULL) "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION"
)
_FUNCTIONS = (
    "SELECT ROUTINE_SCHEMA, ROUTINE_NAME FROM information_schema.ROUTINES "
    "WHERE {scope} AND ROUTINE_TYPE = 'FUNCTION'"
)


def _scope(column: str, current_only: bool) -> str:
    """Which databases a snapshot query reads: every non-system one, or (over RELATION_LIMIT) the current one."""
    return f"{column} = DATABASE()" if current_only else f"{column} NOT IN {_SYSTEM_LIST}"


def _relations(
    conn: MySQLConnection, current_only: bool, schemas: dict[str, SnapshotSchema]
) -> dict[tuple, SnapshotRelation]:
    """Relations (and their columns) of the schemas that take any, keyed by (schema, name)."""
    relations: dict[tuple, SnapshotRelation] = {}
    for schema, name, table_type in catalog.query(conn, _TABLES.format(scope=_scope("TABLE_SCHEMA", current_only))):
        if schema in schemas and not schemas[schema].system:  # system schemas get no relations
            relation = SnapshotRelation(name=name, kind="view" if table_type == "VIEW" else "table", columns=[])
            relations[(schema, name)] = relation
            schemas[schema].relations.append(relation)
    for schema, table, column, type_name in catalog.query(
        conn, _COLUMNS.format(scope=_scope("TABLE_SCHEMA", current_only))
    ):
        if (schema, table) in relations:  # a relation created after the table list was read is skipped
            relations[(schema, table)].columns.append(SnapshotColumn(name=column, type_name=type_name))
    return relations


def _fill_keys(conn: MySQLConnection, current_only: bool, relations: dict[tuple, SnapshotRelation]) -> None:
    foreign_keys: dict[tuple, SnapshotForeignKey] = {}
    for schema, table, constraint, column, ref_schema, ref_table, ref_column in catalog.query(
        conn, _KEYS.format(scope=_scope("TABLE_SCHEMA", current_only))
    ):
        relation = relations.get((schema, table))
        if relation is None:
            continue
        if ref_table is None:  # a PRIMARY row
            relation.primary_key.append(column)
            continue
        key = (schema, table, constraint)
        if key not in foreign_keys:
            foreign_keys[key] = SnapshotForeignKey(
                name=constraint, columns=[], ref_schema=ref_schema, ref_table=ref_table, ref_columns=[]
            )
            relation.foreign_keys.append(foreign_keys[key])
        foreign_keys[key].columns.append(column)
        foreign_keys[key].ref_columns.append(ref_column)


def build_snapshot(conn: MySQLConnection) -> SchemaSnapshot:
    """Schema snapshot of the server: every non-system database, or the current one over RELATION_LIMIT relations."""
    schemas = {name: SnapshotSchema(name=name, system=is_system) for name, is_system in catalog.list_schemas(conn)}
    (count,) = catalog.query(conn, _COUNT.format(scope=f"TABLE_SCHEMA NOT IN {_SYSTEM_LIST}"))[0]
    current_only = count > RELATION_LIMIT
    (current,) = catalog.query(conn, "SELECT DATABASE()")[0]
    if current_only:
        logger.info(
            "MySQL schema snapshot limited to the current database {}: {} relations on the server (limit {})",
            current, count, RELATION_LIMIT,
        )
    relations = _relations(conn, current_only, schemas)
    _fill_keys(conn, current_only, relations)
    for schema, name in catalog.query(conn, _FUNCTIONS.format(scope=_scope("ROUTINE_SCHEMA", current_only))):
        if schema in schemas and not schemas[schema].system:
            schemas[schema].functions.append(name)
    for schema in schemas.values():
        schema.functions.sort()
    logger.debug("MySQL schema snapshot: {} schemas, {} relations", len(schemas), len(relations))
    return SchemaSnapshot(
        database=None, default_schema=current, search_path=[current] if current else [],
        schemas=list(schemas.values()), built_at=datetime.now(UTC),
    )
