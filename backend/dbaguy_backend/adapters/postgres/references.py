"""The foreign keys from or into some tables of one schema, with each one's ON DELETE action (a copy's order and what
its DELETE reaches, 06c Task 2 fix round 4): one catalog query. A partition's copy of its partitioned table's key
(conparentid) is the partitioned table's, never counted twice."""

import psycopg

from ..references import OnDelete, Reference

_REFERENCES = """
SELECT cn.nspname::text, c.relname::text, pn.nspname::text, p.relname::text, con.confdeltype::text
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace cn ON cn.oid = c.relnamespace
JOIN pg_class p ON p.oid = con.confrelid JOIN pg_namespace pn ON pn.oid = p.relnamespace
WHERE con.contype = 'f' AND con.conparentid = 0
  AND ((pn.nspname = %(schema)s AND p.relname::text = ANY(%(names)s))
       OR (cn.nspname = %(schema)s AND c.relname::text = ANY(%(names)s)))
ORDER BY cn.nspname, c.relname, con.conname
"""
_ON_DELETE: dict[str, OnDelete] = {"a": "no_action", "r": "restrict", "c": "cascade", "n": "set_null",
                                   "d": "set_default"}


def references(conn: psycopg.Connection, schema: str, names: list[str]) -> list[Reference]:
    if not names:
        return []
    return [Reference(child_schema, child, parent_schema, parent, _ON_DELETE.get(action, "no_action"))
            for child_schema, child, parent_schema, parent, action
            in conn.execute(_REFERENCES, {"schema": schema, "names": list(names)})]
