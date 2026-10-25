"""The foreign keys from or into some tables of one schema, with each one's ON DELETE action (a copy's order and what
its DELETE reaches, 06c Task 2 fix round 4): one catalog query. A disabled key (NOCHECK) neither checks nor cascades.
With `rows`, whether each table that references one of them through a key that refuses the DELETE (NO ACTION) holds
rows (06c final review I-2): from `sys.partitions`' row counts (the heap's or the clustered index's), a catalog read
with no lock on the table."""

from ..references import OnDelete, Reference
from . import driver
from .catalog import literal, rows as catalog_rows

_REFERENCES = """
SELECT cs.name, c.name, ps.name, p.name, fk.delete_referential_action,
       CASE WHEN {rows} = 1 AND fk.delete_referential_action = 0 AND fk.parent_object_id <> fk.referenced_object_id
                 AND ps.name = {schema} AND p.name IN ({names})
            THEN CASE WHEN EXISTS (SELECT 1 FROM sys.partitions pt WHERE pt.object_id = c.object_id
                                   AND pt.index_id IN (0, 1) AND pt.rows > 0) THEN 1 ELSE 0 END
       END
FROM sys.foreign_keys fk
JOIN sys.objects c ON c.object_id = fk.parent_object_id JOIN sys.schemas cs ON cs.schema_id = c.schema_id
JOIN sys.objects p ON p.object_id = fk.referenced_object_id JOIN sys.schemas ps ON ps.schema_id = p.schema_id
WHERE fk.is_disabled = 0
  AND ((ps.name = {schema} AND p.name IN ({names})) OR (cs.name = {schema} AND c.name IN ({names})))
ORDER BY cs.name, c.name, fk.name
"""
_ON_DELETE: dict[int, OnDelete] = {0: "no_action", 1: "cascade", 2: "set_null", 3: "set_default"}


def references(conn: driver.Connection, schema: str, names: list[str], *, rows: bool = False) -> list[Reference]:
    if not names:
        return []
    sql = _REFERENCES.format(schema=literal(schema), names=", ".join(literal(name) for name in names),
                             rows=1 if rows else 0)
    return [Reference(child_schema, child, parent_schema, parent, _ON_DELETE.get(int(action), "no_action"),
                      child_rows=None if holds is None else bool(holds))
            for child_schema, child, parent_schema, parent, action, holds in catalog_rows(conn, sql)]
