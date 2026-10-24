"""The foreign keys from or into some tables of one schema, with each one's ON DELETE action (a copy's order and what
its DELETE reaches, 06c Task 2 fix round 4): one catalog query. A disabled key (NOCHECK) neither checks nor cascades."""

from ..references import OnDelete, Reference
from . import driver
from .catalog import literal, rows

_REFERENCES = """
SELECT cs.name, c.name, ps.name, p.name, fk.delete_referential_action
FROM sys.foreign_keys fk
JOIN sys.objects c ON c.object_id = fk.parent_object_id JOIN sys.schemas cs ON cs.schema_id = c.schema_id
JOIN sys.objects p ON p.object_id = fk.referenced_object_id JOIN sys.schemas ps ON ps.schema_id = p.schema_id
WHERE fk.is_disabled = 0
  AND ((ps.name = {schema} AND p.name IN ({names})) OR (cs.name = {schema} AND c.name IN ({names})))
ORDER BY cs.name, c.name, fk.name
"""
_ON_DELETE: dict[int, OnDelete] = {0: "no_action", 1: "cascade", 2: "set_null", 3: "set_default"}


def references(conn: driver.Connection, schema: str, names: list[str]) -> list[Reference]:
    if not names:
        return []
    sql = _REFERENCES.format(schema=literal(schema), names=", ".join(literal(name) for name in names))
    return [Reference(child_schema, child, parent_schema, parent, _ON_DELETE.get(int(action), "no_action"))
            for child_schema, child, parent_schema, parent, action in rows(conn, sql)]
