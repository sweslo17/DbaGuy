from dataclasses import dataclass

import psycopg

from ...domain.errors import NotFound, ValidationFailed
from ...domain.metadata import ColumnInfo, Folder, NodeKind, ObjectNode, PathSegment, make_node
from ..sql_text import qualified_pg

SCHEMA_OID = "(SELECT oid FROM pg_namespace WHERE nspname = %(schema)s)"

_SCHEMAS_SQL = """
SELECT n.nspname,
       (n.nspname IN ('pg_catalog', 'information_schema') OR n.nspname LIKE 'pg\\_%%') AS is_system
FROM pg_namespace n
WHERE n.nspname NOT LIKE 'pg\\_temp\\_%%' AND n.nspname NOT LIKE 'pg\\_toast%%'
ORDER BY is_system, n.nspname
"""

_FOLDER_COUNTS_SQL = """
SELECT
  (SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid AND c.relkind IN ('r', 'p') AND NOT c.relispartition),
  (SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid AND c.relkind = 'v'),
  (SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid AND c.relkind = 'm'),
  (SELECT count(*) FROM pg_proc p WHERE p.pronamespace = n.oid AND p.prokind IN ('f', 'w')),
  (SELECT count(*) FROM pg_proc p WHERE p.pronamespace = n.oid AND p.prokind = 'p'),
  (SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid AND c.relkind = 'S'),
  (SELECT count(*) FROM pg_type t LEFT JOIN pg_class c ON c.oid = t.typrelid
    WHERE t.typnamespace = n.oid AND (t.typtype IN ('e', 'd') OR (t.typtype = 'c' AND c.relkind = 'c')))
FROM pg_namespace n WHERE n.nspname = %(schema)s
"""
_FOLDER_ORDER = (
    Folder.TABLES, Folder.VIEWS, Folder.MATERIALIZED_VIEWS, Folder.FUNCTIONS,
    Folder.PROCEDURES, Folder.SEQUENCES, Folder.TYPES,
)

_RELKIND_BY_FOLDER = {Folder.VIEWS: ("v", NodeKind.VIEW), Folder.MATERIALIZED_VIEWS: ("m", NodeKind.MATERIALIZED_VIEW),
                      Folder.SEQUENCES: ("S", NodeKind.SEQUENCE)}
_TYPE_LABEL = {"e": "enum", "d": "domain", "c": "composite"}
_HAS_FOLDERS = (NodeKind.TABLE, NodeKind.PARTITION, NodeKind.VIEW, NodeKind.MATERIALIZED_VIEW)
_CAN_PARTITION = (NodeKind.TABLE, NodeKind.PARTITION)

# The collation only when it is not the type's own (COLLATE "C" on a text column); the storage only when it is not the
# type's; the statistics target and the compression (14+) only when set
_COLUMNS_SQL = """
SELECT a.attrelid, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, pg_get_expr(d.adbin, d.adrelid),
       a.attidentity::text, a.attgenerated::text, col_description(a.attrelid, a.attnum),
       EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = a.attrelid AND i.indisprimary AND a.attnum = ANY (i.indkey)),
       cn.nspname::text, co.collname::text,
       CASE WHEN a.attstattarget >= 0 THEN a.attstattarget END,
       CASE WHEN a.attstorage <> t.typstorage THEN a.attstorage::text END, {compression}
FROM pg_attribute a
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
LEFT JOIN pg_type t ON t.oid = a.atttypid
LEFT JOIN pg_collation co ON co.oid = a.attcollation AND a.attcollation <> t.typcollation
LEFT JOIN pg_namespace cn ON cn.oid = co.collnamespace
WHERE a.attrelid = ANY(%(oids)s) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attrelid, a.attnum
"""
_STORAGE = {"p": "PLAIN", "e": "EXTERNAL", "m": "MAIN", "x": "EXTENDED"}
_COMPRESSION = {"p": "pglz", "l": "lz4"}


@dataclass(frozen=True)
class PgColumn:
    name: str
    type: str
    not_null: bool
    default: str | None
    identity: str  # '' | 'a' (ALWAYS) | 'd' (BY DEFAULT)
    generated: str  # '' | 's' (stored) | 'v' (virtual, PG 18)
    comment: str | None
    is_pk: bool
    collation: str | None = None  # qualified, when it is not the type's own
    statistics: int | None = None  # SET STATISTICS, when set
    storage: str | None = None  # SET STORAGE (PLAIN, EXTERNAL…), when it is not the type's
    compression: str | None = None  # SET COMPRESSION (pglz, lz4), when set

    def info(self) -> ColumnInfo:
        return ColumnInfo(
            name=self.name, type_name=self.type, nullable=not self.not_null,
            default=self.default, is_primary_key=self.is_pk, comment=self.comment,
        )

    def detail(self) -> str:
        parts = [self.type]
        if self.not_null:
            parts.append("NOT NULL")
        if self.is_pk:
            parts.append("PK")
        return " ".join(parts)


def relation_oid(conn: psycopg.Connection, qname: str) -> int:
    row = conn.execute("SELECT to_regclass(%s)::oid", (qname,)).fetchone()
    if row is None or row[0] is None:
        raise NotFound(f"Relation {qname} not found")
    return int(row[0])


def columns_of(conn: psycopg.Connection, oids: list[int]) -> dict[int, list[PgColumn]]:
    """The columns of every relation in `oids`, in one query (a dump reads thousands)."""
    found: dict[int, list[PgColumn]] = {oid: [] for oid in oids}
    if not oids:
        return found
    compression = "a.attcompression::text" if conn.info.server_version >= 140000 else "''"
    for oid, *row in conn.execute(_COLUMNS_SQL.format(compression=compression), {"oids": list(oids)}):
        collation = qualified_pg(row[8], row[9]) if row[9] is not None else None
        found[oid].append(PgColumn(*row[:8], collation=collation, statistics=row[10],
                                   storage=_STORAGE.get(row[11]) if row[11] else None,
                                   compression=_COMPRESSION.get(row[12]) if row[12] else None))
    return found


def table_columns(conn: psycopg.Connection, oid: int) -> list[PgColumn]:
    return columns_of(conn, [oid])[oid]


def _folder(segment: PathSegment) -> Folder:
    try:
        return Folder(segment.name)
    except ValueError:
        raise ValidationFailed(f"Unknown folder '{segment.name}'") from None


def list_children(conn: psycopg.Connection, path: list[PathSegment]) -> list[ObjectNode]:
    if not path:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY datname"
        )
        return [make_node(path, NodeKind.DATABASE, name, has_children=True) for (name,) in rows]
    rel = path[1:]  # path[0] is the database this connection is bound to
    if not rel:
        return [
            make_node(path, NodeKind.SCHEMA, name, has_children=True, system=is_system)
            for name, is_system in conn.execute(_SCHEMAS_SQL)
        ]
    schema = rel[0].name
    if len(rel) == 1:
        row = conn.execute(_FOLDER_COUNTS_SQL, {"schema": schema}).fetchone()
        if row is None:
            raise NotFound(f"Schema {schema} not found")
        return [make_node(path, NodeKind.FOLDER, f, has_children=True) for f, n in zip(_FOLDER_ORDER, row) if n]
    if len(rel) == 2:
        return _schema_objects(conn, path, schema, _folder(rel[1]))
    # Below the schema folders the path alternates object, folder, object, ... (partitions nest to any depth).
    last = rel[-1]
    if last.kind == NodeKind.FOLDER:
        return _object_children(conn, path, schema, rel[-2], _folder(last))
    return _object_folders(conn, path, schema, last)


def _schema_objects(conn: psycopg.Connection, path: list[PathSegment], schema: str, folder: Folder) -> list[ObjectNode]:
    params = {"schema": schema}
    if folder == Folder.TABLES:
        rows = conn.execute(
            f"SELECT c.relname, c.relkind::text, c.reltuples::float8 FROM pg_class c "
            f"WHERE c.relnamespace = {SCHEMA_OID} AND c.relkind IN ('r', 'p') AND NOT c.relispartition ORDER BY c.relname",
            params,
        )
        return [
            make_node(
                path, NodeKind.TABLE, name, has_children=True,
                detail="partitioned" if relkind == "p" else (f"~{int(est):,} rows" if est >= 0 else None),
            )
            for name, relkind, est in rows
        ]
    if folder in _RELKIND_BY_FOLDER:
        relkind, kind = _RELKIND_BY_FOLDER[folder]
        rows = conn.execute(
            f"SELECT c.relname FROM pg_class c WHERE c.relnamespace = {SCHEMA_OID} AND c.relkind = %(relkind)s "
            f"ORDER BY c.relname",
            {**params, "relkind": relkind},
        )
        return [make_node(path, kind, name, has_children=kind != NodeKind.SEQUENCE) for (name,) in rows]
    if folder in (Folder.FUNCTIONS, Folder.PROCEDURES):
        kinds = ["f", "w"] if folder == Folder.FUNCTIONS else ["p"]
        kind = NodeKind.FUNCTION if folder == Folder.FUNCTIONS else NodeKind.PROCEDURE
        rows = conn.execute(
            f"SELECT p.proname || '(' || oidvectortypes(p.proargtypes) || ')', pg_get_function_result(p.oid) "
            f"FROM pg_proc p WHERE p.pronamespace = {SCHEMA_OID} AND p.prokind::text = ANY(%(kinds)s) ORDER BY 1",
            {**params, "kinds": kinds},
        )
        return [make_node(path, kind, sig, has_children=False, detail=result) for sig, result in rows]
    if folder == Folder.TYPES:
        rows = conn.execute(
            f"SELECT t.typname, t.typtype::text FROM pg_type t LEFT JOIN pg_class c ON c.oid = t.typrelid "
            f"WHERE t.typnamespace = {SCHEMA_OID} AND (t.typtype IN ('e', 'd') OR (t.typtype = 'c' AND c.relkind = 'c')) "
            f"ORDER BY t.typname",
            params,
        )
        return [make_node(path, NodeKind.TYPE, name, has_children=False, detail=_TYPE_LABEL.get(t)) for name, t in rows]
    return []


def _object_folders(conn: psycopg.Connection, path: list[PathSegment], schema: str, obj: PathSegment) -> list[ObjectNode]:
    if obj.kind not in _HAS_FOLDERS:
        return []
    oid = relation_oid(conn, qualified_pg(schema, obj.name))
    indexes, triggers, partitions = conn.execute(
        "SELECT (SELECT count(*) FROM pg_index WHERE indrelid = %(oid)s), "
        "(SELECT count(*) FROM pg_trigger WHERE tgrelid = %(oid)s AND NOT tgisinternal), "
        "(SELECT count(*) FROM pg_inherits WHERE inhparent = %(oid)s)",
        {"oid": oid},
    ).fetchone()
    folders = [Folder.COLUMNS]
    if indexes:
        folders.append(Folder.INDEXES)
    if triggers:
        folders.append(Folder.TRIGGERS)
    if partitions and obj.kind in _CAN_PARTITION:
        folders.append(Folder.PARTITIONS)
    return [make_node(path, NodeKind.FOLDER, f, has_children=True) for f in folders]


def _object_children(
    conn: psycopg.Connection, path: list[PathSegment], schema: str, obj: PathSegment, folder: Folder
) -> list[ObjectNode]:
    oid = relation_oid(conn, qualified_pg(schema, obj.name))
    if folder == Folder.COLUMNS:
        return [make_node(path, NodeKind.COLUMN, c.name, has_children=False, detail=c.detail()) for c in table_columns(conn, oid)]
    if folder == Folder.INDEXES:
        rows = conn.execute(
            "SELECT ic.relname, i.indisprimary, i.indisunique FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
            "WHERE i.indrelid = %(oid)s ORDER BY ic.relname",
            {"oid": oid},
        )
        return [
            make_node(path, NodeKind.INDEX, name, has_children=False,
                      detail="primary" if primary else ("unique" if unique else None))
            for name, primary, unique in rows
        ]
    if folder == Folder.TRIGGERS:
        rows = conn.execute(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = %(oid)s AND NOT tgisinternal ORDER BY tgname", {"oid": oid}
        )
        return [make_node(path, NodeKind.TRIGGER, name, has_children=False) for (name,) in rows]
    if folder == Folder.PARTITIONS:
        rows = conn.execute(
            "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid), c.relkind = 'p' "
            "FROM pg_inherits h JOIN pg_class c ON c.oid = h.inhrelid WHERE h.inhparent = %(oid)s ORDER BY c.relname",
            {"oid": oid},
        )
        # a partition that is itself partitioned can be expanded further
        return [
            make_node(path, NodeKind.PARTITION, name, has_children=is_partitioned, detail=bound)
            for name, bound, is_partitioned in rows
        ]
    return []
