from datetime import UTC, datetime

import psycopg
from loguru import logger

from ...domain.schema import SchemaSnapshot, SnapshotColumn, SnapshotForeignKey, SnapshotRelation, SnapshotSchema

# Everything below runs without parameters, so `%` and `\_` are plain SQL text here.
_SCHEMAS_SQL = r"""
SELECT nspname, (nspname IN ('pg_catalog', 'information_schema') OR nspname LIKE 'pg\_%') AS is_system
FROM pg_namespace
WHERE nspname NOT LIKE 'pg\_toast%' AND nspname NOT LIKE 'pg\_temp\_%'
ORDER BY is_system, nspname
"""
# System schemas contribute no relations (spec §3.2): their catalogs are huge and completion never needs their columns.
_COLUMNS_SQL = r"""
SELECT n.nspname, c.relname, c.relkind::text, a.attname, format_type(a.atttypid, a.atttypmod)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.relkind IN ('r', 'p', 'f', 'v', 'm') AND NOT c.relispartition
  AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'
ORDER BY n.nspname, c.relname, a.attnum
"""
_PK_SQL = """
SELECT n.nspname, c.relname, array_agg(a.attname::text ORDER BY k.ord)
FROM pg_constraint co
JOIN pg_class c ON c.oid = co.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL unnest(co.conkey) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_attribute a ON a.attrelid = co.conrelid AND a.attnum = k.attnum
WHERE co.contype = 'p'
GROUP BY n.nspname, c.relname
"""
_FK_SQL = """
SELECT n.nspname, c.relname, co.conname, array_agg(a.attname::text ORDER BY k.ord),
       rn.nspname, rc.relname, array_agg(ra.attname::text ORDER BY k.ord)
FROM pg_constraint co
JOIN pg_class c ON c.oid = co.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_class rc ON rc.oid = co.confrelid
JOIN pg_namespace rn ON rn.oid = rc.relnamespace
CROSS JOIN LATERAL unnest(co.conkey, co.confkey) WITH ORDINALITY AS k(attnum, refnum, ord)
JOIN pg_attribute a ON a.attrelid = co.conrelid AND a.attnum = k.attnum
JOIN pg_attribute ra ON ra.attrelid = co.confrelid AND ra.attnum = k.refnum
WHERE co.contype = 'f' AND co.conparentid = 0  -- PG clones an FK to a partitioned table onto every partition
GROUP BY n.nspname, c.relname, co.conname, rn.nspname, rc.relname
ORDER BY n.nspname, c.relname, co.conname
"""
# Only pg_catalog's built-ins (count, now, …) are always callable; other system schemas are not listed at all.
_FUNCTIONS_SQL = r"""
SELECT DISTINCT n.nspname, p.proname
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.prokind IN ('f', 'a', 'w')
  AND (n.nspname = 'pg_catalog' OR (n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\_%'))
"""
_KIND = {"r": "table", "p": "table", "f": "table", "v": "view", "m": "materialized_view"}


def build_snapshot(conn: psycopg.Connection) -> SchemaSnapshot:
    """Schema snapshot of the connection's database in six queries, whatever the number of tables."""
    schemas = {name: SnapshotSchema(name=name, system=is_system) for name, is_system in conn.execute(_SCHEMAS_SQL)}
    relations: dict[tuple[str, str], SnapshotRelation] = {}
    for schema, relname, relkind, column, type_name in conn.execute(_COLUMNS_SQL):
        key = (schema, relname)
        if key not in relations:
            if schema not in schemas:  # created after the schema list was read
                continue
            relations[key] = SnapshotRelation(name=relname, kind=_KIND[relkind], columns=[])
            schemas[schema].relations.append(relations[key])
        relations[key].columns.append(SnapshotColumn(name=column, type_name=type_name))
    for schema, relname, columns in conn.execute(_PK_SQL):
        if (schema, relname) in relations:
            relations[(schema, relname)].primary_key = list(columns)
    for schema, relname, name, columns, ref_schema, ref_table, ref_columns in conn.execute(_FK_SQL):
        if (schema, relname) in relations:
            relations[(schema, relname)].foreign_keys.append(SnapshotForeignKey(
                name=name, columns=list(columns), ref_schema=ref_schema, ref_table=ref_table,
                ref_columns=list(ref_columns),
            ))
    for schema, proname in conn.execute(_FUNCTIONS_SQL):
        if schema in schemas:
            schemas[schema].functions.append(proname)
    for s in schemas.values():
        s.functions.sort()
    database, search_path = conn.execute("SELECT current_database(), current_schemas(false)").fetchone()
    logger.debug("PostgreSQL schema snapshot of {}: {} schemas, {} relations", database, len(schemas), len(relations))
    return SchemaSnapshot(
        database=database, default_schema=search_path[0] if search_path else None,
        search_path=list(search_path), schemas=list(schemas.values()), built_at=datetime.now(UTC),
    )
