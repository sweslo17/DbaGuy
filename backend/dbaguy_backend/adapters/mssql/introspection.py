"""The SQL Server object tree and object details, over a connection already bound to the database `path[0]` names.

Tree: database > schema > folder > object, and table|view > folder (columns, indexes, foreign_keys, triggers) > leaf.
Every name reaches SQL through `q()`, `literal()` or `object_id_sql()` (catalog.py); a statement never takes a driver
parameter.

Whether the schema or table a call names still exists is not a query of its own: it rides on the count query of the
level (an `exists` row) or is asked only after an answer came back empty, so the common path of every call is one round
trip, however far the connection is (over an SSH tunnel every one counts).
"""

from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger

from ...domain.errors import NotFound, ValidationFailed
from ...domain.metadata import ColumnInfo, Folder, NodeKind, ObjectDetail, ObjectNode, PathSegment, make_node
from . import driver
from .catalog import SYSTEM_DATABASES, first_row, is_system_schema, literal, name_literal, object_id_sql, q, rows, type_sql

_DATABASES = (
    "SELECT name FROM sys.databases WHERE HAS_DBACCESS(name) = 1 AND state = 0 "
    "ORDER BY CASE WHEN database_id <= 4 THEN 1 ELSE 0 END, name"
)
_SCHEMAS = "SELECT name, schema_id FROM sys.schemas"
_FOLDER_COUNTS = """
SELECT 'tables', COUNT(*) FROM sys.tables WHERE schema_id = SCHEMA_ID({s})
UNION ALL SELECT 'views', COUNT(*) FROM sys.views WHERE schema_id = SCHEMA_ID({s})
UNION ALL SELECT 'functions', COUNT(*) FROM sys.objects WHERE schema_id = SCHEMA_ID({s}) AND type IN ('FN', 'IF', 'TF', 'FS', 'FT')
UNION ALL SELECT 'procedures', COUNT(*) FROM sys.procedures WHERE schema_id = SCHEMA_ID({s})
UNION ALL SELECT 'sequences', COUNT(*) FROM sys.sequences WHERE schema_id = SCHEMA_ID({s})
UNION ALL SELECT 'synonyms', COUNT(*) FROM sys.synonyms WHERE schema_id = SCHEMA_ID({s})
UNION ALL SELECT 'types', COUNT(*) FROM sys.types WHERE is_user_defined = 1 AND schema_id = SCHEMA_ID({s})
UNION ALL SELECT 'exists', CASE WHEN SCHEMA_ID({s}) IS NULL THEN 0 ELSE 1 END
"""
_OBJECTS: dict[Folder, str] = {  # (name, detail)
    Folder.TABLES: "SELECT name, NULL FROM sys.tables WHERE schema_id = SCHEMA_ID({s}) ORDER BY name",
    Folder.VIEWS: "SELECT name, NULL FROM sys.views WHERE schema_id = SCHEMA_ID({s}) ORDER BY name",
    Folder.FUNCTIONS: "SELECT name, CASE WHEN type IN ('FN', 'FS') THEN 'scalar' WHEN type = 'IF' THEN 'inline table' "
                      "ELSE 'table' END FROM sys.objects WHERE schema_id = SCHEMA_ID({s}) "
                      "AND type IN ('FN', 'IF', 'TF', 'FS', 'FT') ORDER BY name",
    Folder.PROCEDURES: "SELECT name, NULL FROM sys.procedures WHERE schema_id = SCHEMA_ID({s}) ORDER BY name",
    Folder.SEQUENCES: "SELECT name, TYPE_NAME(user_type_id) FROM sys.sequences WHERE schema_id = SCHEMA_ID({s}) ORDER BY name",
    Folder.SYNONYMS: "SELECT name, base_object_name FROM sys.synonyms WHERE schema_id = SCHEMA_ID({s}) ORDER BY name",
}
_TYPE_COLUMNS = """
SELECT t.name, t.is_table_type, bt.name, t.max_length, t.precision, t.scale, t.is_nullable
FROM sys.types t LEFT JOIN sys.types bt ON bt.user_type_id = t.system_type_id
WHERE t.is_user_defined = 1 AND """
_TYPES = _TYPE_COLUMNS + "t.schema_id = SCHEMA_ID({s}) ORDER BY t.name"
_TYPE = _TYPE_COLUMNS + "t.user_type_id = TYPE_ID({ref})"
_TABLE_FOLDER_COUNTS = """
SELECT 'indexes', COUNT(*) FROM sys.indexes WHERE object_id = {o} AND type IN (1, 2)
UNION ALL SELECT 'foreign_keys', COUNT(*) FROM sys.foreign_keys WHERE parent_object_id = {o}
UNION ALL SELECT 'triggers', COUNT(*) FROM sys.triggers WHERE parent_id = {o}
UNION ALL SELECT 'exists', COUNT(*) FROM sys.objects WHERE object_id = {o} AND type = {t}
"""
_COLUMNS = """
SELECT c.name, t.name, c.max_length, c.precision, c.scale, c.is_nullable, c.is_identity,
       CAST(ic.seed_value AS nvarchar(40)), CAST(ic.increment_value AS nvarchar(40)), cc.definition, cc.is_persisted,
       dc.name, dc.definition, CASE WHEN t.is_user_defined = 1 THEN SCHEMA_NAME(t.schema_id) END, c.collation_name,
       CONVERT(nvarchar(128), DATABASEPROPERTYEX(DB_NAME(), 'Collation')),
       CASE WHEN EXISTS (SELECT 1 FROM sys.indexes i JOIN sys.index_columns k
                         ON k.object_id = i.object_id AND k.index_id = i.index_id
                         WHERE i.is_primary_key = 1 AND k.object_id = c.object_id AND k.column_id = c.column_id)
            THEN 1 ELSE 0 END,
       CAST(ep.value AS nvarchar(4000))
FROM sys.columns c JOIN sys.types t ON t.user_type_id = c.user_type_id
LEFT JOIN sys.identity_columns ic ON ic.object_id = c.object_id AND ic.column_id = c.column_id
LEFT JOIN sys.computed_columns cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id
LEFT JOIN sys.default_constraints dc ON dc.parent_object_id = c.object_id AND dc.parent_column_id = c.column_id
LEFT JOIN sys.extended_properties ep
       ON ep.class = 1 AND ep.major_id = c.object_id AND ep.minor_id = c.column_id AND ep.name = N'MS_Description'
WHERE c.object_id = {o} ORDER BY c.column_id
"""
_KEYS = """
SELECT i.name, i.is_primary_key, i.type_desc, COL_NAME(k.object_id, k.column_id), k.is_descending_key
FROM sys.indexes i JOIN sys.index_columns k ON k.object_id = i.object_id AND k.index_id = i.index_id
WHERE i.object_id = {o} AND (i.is_primary_key = 1 OR i.is_unique_constraint = 1)
ORDER BY i.is_primary_key DESC, i.name, k.key_ordinal
"""
_CHECKS = "SELECT name, definition FROM sys.check_constraints WHERE parent_object_id = {o} ORDER BY name"
_FOREIGN_KEYS = """
SELECT f.name, COL_NAME(fc.parent_object_id, fc.parent_column_id), OBJECT_SCHEMA_NAME(f.referenced_object_id),
       OBJECT_NAME(f.referenced_object_id), COL_NAME(fc.referenced_object_id, fc.referenced_column_id),
       f.delete_referential_action_desc, f.update_referential_action_desc
FROM sys.foreign_keys f JOIN sys.foreign_key_columns fc ON fc.constraint_object_id = f.object_id
WHERE f.parent_object_id = {o} ORDER BY f.name, fc.constraint_column_id
"""
_INDEXES = """
SELECT i.name, i.is_unique, i.type_desc, i.filter_definition, COL_NAME(k.object_id, k.column_id), k.is_descending_key,
       k.is_included_column, i.is_primary_key, i.is_unique_constraint
FROM sys.indexes i JOIN sys.index_columns k ON k.object_id = i.object_id AND k.index_id = i.index_id
WHERE i.object_id = {o} AND i.type IN (1, 2)
ORDER BY i.is_primary_key DESC, i.name, k.is_included_column, k.key_ordinal, k.index_column_id
"""
_INDEX_NODES = (
    "SELECT name, is_primary_key, is_unique FROM sys.indexes WHERE object_id = {o} AND type IN (1, 2) "
    "ORDER BY is_primary_key DESC, name"
)
_TRIGGERS = """
SELECT t.name, t.is_instead_of_trigger, LOWER(e.type_desc) FROM sys.triggers t
JOIN sys.trigger_events e ON e.object_id = t.object_id WHERE t.parent_id = {o} ORDER BY t.name, e.type
"""
_SEQUENCE = """
SELECT TYPE_NAME(user_type_id), CAST(start_value AS nvarchar(40)), CAST(increment AS nvarchar(40)),
       CAST(minimum_value AS nvarchar(40)), CAST(maximum_value AS nvarchar(40)), is_cycling, is_cached, cache_size,
       precision, scale
FROM sys.sequences WHERE object_id = {o}
"""
_SYNONYM = "SELECT base_object_name FROM sys.synonyms WHERE object_id = {o}"
_DEFINITION = "SELECT OBJECT_ID({ref}), OBJECT_DEFINITION(OBJECT_ID({ref}))"
_TABLE_COMMENT = ("SELECT CAST(value AS nvarchar(4000)) FROM sys.extended_properties "
                  "WHERE class = 1 AND major_id = {o} AND minor_id = 0 AND name = N'MS_Description'")
_TABLE_TYPE_OBJECT = "(SELECT type_table_object_id FROM sys.table_types WHERE user_type_id = TYPE_ID({ref}))"
# Run only when an answer came back empty, to tell an empty answer from a schema or table that is gone.
_PRESENCE = "SELECT SCHEMA_ID({s}), (SELECT COUNT(*) FROM sys.objects WHERE object_id = {o} AND type = {t})"

_SCHEMA_FOLDERS = (
    Folder.TABLES, Folder.VIEWS, Folder.FUNCTIONS, Folder.PROCEDURES, Folder.SEQUENCES, Folder.SYNONYMS, Folder.TYPES,
)
_KIND_OF_FOLDER: dict[Folder, NodeKind] = {
    Folder.TABLES: NodeKind.TABLE,
    Folder.VIEWS: NodeKind.VIEW,
    Folder.FUNCTIONS: NodeKind.FUNCTION,
    Folder.PROCEDURES: NodeKind.PROCEDURE,
    Folder.SEQUENCES: NodeKind.SEQUENCE,
    Folder.SYNONYMS: NodeKind.SYNONYM,
    Folder.TYPES: NodeKind.TYPE,
}
_HAS_FOLDERS = (NodeKind.TABLE, NodeKind.VIEW)
_RELATION_TYPE = {NodeKind.TABLE: "U", NodeKind.VIEW: "V"}  # sys.objects.type
_ACTIONS = {"NO_ACTION": "NO ACTION", "CASCADE": "CASCADE", "SET_NULL": "SET NULL", "SET_DEFAULT": "SET DEFAULT"}


@dataclass(frozen=True, slots=True)
class _Column:
    """One row of _COLUMNS."""

    name: str
    type_name: str
    max_length: int
    precision: int
    scale: int
    nullable: bool
    identity: bool
    seed: str | None
    increment: str | None
    computed: str | None
    persisted: bool | None
    default_name: str | None
    default: str | None
    user_schema: str | None  # set for an alias type: the type is then written schema-qualified
    collation: str | None
    database_collation: str | None
    primary_key: bool
    comment: str | None

    def info(self) -> ColumnInfo:
        """An alias type keeps its name; a built-in type is written as T-SQL writes it."""
        type_name = self.type_name if self.user_schema else type_sql(self.type_name, self.max_length, self.precision, self.scale)
        return ColumnInfo(
            name=self.name, type_name=type_name, nullable=bool(self.nullable), default=self.default,
            is_primary_key=bool(self.primary_key), comment=self.comment or None,
        )


def _columns(conn: driver.Connection, object_sql: str) -> list[_Column]:
    return [_Column(*row) for row in rows(conn, _COLUMNS.format(o=object_sql))]


def _grouped(found: list[tuple]) -> dict[str, list[tuple]]:
    """Rows grouped by their first field, in the order the fields first appear."""
    groups: dict[str, list[tuple]] = {}
    for row in found:
        groups.setdefault(row[0], []).append(row)
    return groups


def _check_head(path: list[PathSegment]) -> None:
    if path[0].kind != NodeKind.DATABASE:
        raise ValidationFailed(f"A path starts at a database, not at a {path[0].kind}")
    if len(path) > 1 and path[1].kind != NodeKind.SCHEMA:
        raise ValidationFailed(f"A database holds schemas, not a {path[1].kind}")


def _folder(name: str) -> Folder:
    try:
        return Folder(name)
    except ValueError:
        raise ValidationFailed(f"Unknown folder '{name}'") from None


def _require_relation(conn: driver.Connection, schema: str, name: str, kind: NodeKind | str) -> None:
    """After an empty answer about a table's or view's children: NotFound when the schema or the relation is gone."""
    relation_type = _RELATION_TYPE[NodeKind(kind)]
    schema_id, count = first_row(conn, _PRESENCE.format(s=literal(schema), o=object_id_sql(schema, name), t=literal(relation_type)))
    if schema_id is None:
        raise NotFound(f"Schema '{schema}' not found")
    if not count:
        raise NotFound(f"{kind} '{name}' not found")


def _column_detail(col: ColumnInfo) -> str:
    parts = [col.type_name]
    if not col.nullable:
        parts.append("NOT NULL")
    if col.is_primary_key:
        parts.append("PK")
    return " ".join(parts)


def _type_detail(is_table_type: bool, base: str | None, max_length: int, precision: int, scale: int) -> str:
    if is_table_type:
        return "table type"
    if base is None:  # a CLR type: no built-in type under it
        return "CLR type"
    return type_sql(base, max_length, precision, scale)


# ---- tree ----------------------------------------------------------------------------------------------------------


def list_children(conn: driver.Connection, path: list[PathSegment]) -> list[ObjectNode]:
    if not path:
        return _databases(conn)
    _check_head(path)
    last = path[-1]
    folder = _folder(last.name) if last.kind == NodeKind.FOLDER else None  # unknown folder names fail at any depth
    depth = len(path)
    if depth == 1:
        return _schemas(conn, path)
    schema = path[1].name
    if depth == 2:
        return _schema_folders(conn, path, schema)
    if depth == 3 and folder is not None:
        return _schema_objects(conn, path, schema, folder)
    if depth == 4 and last.kind in _HAS_FOLDERS:
        return _table_folders(conn, path, schema, last)
    if depth == 5 and folder is not None:
        return _table_children(conn, path, schema, folder)
    return []


def _databases(conn: driver.Connection) -> list[ObjectNode]:
    return [
        make_node([], NodeKind.DATABASE, name, has_children=True, system=name in SYSTEM_DATABASES)
        for (name,) in rows(conn, _DATABASES)
    ]


def _schemas(conn: driver.Connection, path: list[PathSegment]) -> list[ObjectNode]:
    """Every schema, user schemas first, each group by name."""
    found = [(name, is_system_schema(name, schema_id)) for name, schema_id in rows(conn, _SCHEMAS)]
    found.sort(key=lambda schema: (schema[1], schema[0].casefold(), schema[0]))
    return [make_node(path, NodeKind.SCHEMA, name, has_children=True, system=is_system) for name, is_system in found]


def _schema_folders(conn: driver.Connection, path: list[PathSegment], schema: str) -> list[ObjectNode]:
    counts = dict(rows(conn, _FOLDER_COUNTS.format(s=literal(schema))))
    if not counts["exists"]:
        raise NotFound(f"Schema '{schema}' not found")
    return [make_node(path, NodeKind.FOLDER, folder, has_children=True) for folder in _SCHEMA_FOLDERS if counts[folder]]


def _object_rows(conn: driver.Connection, schema: str, folder: Folder) -> list[tuple]:
    """(name, detail) of the schema's objects in a folder."""
    if folder == Folder.TYPES:
        return [
            (name, _type_detail(is_table_type, base, max_length, precision, scale))
            for name, is_table_type, base, max_length, precision, scale, _ in rows(conn, _TYPES.format(s=literal(schema)))
        ]
    return rows(conn, _OBJECTS[folder].format(s=literal(schema)))


def _schema_objects(conn: driver.Connection, path: list[PathSegment], schema: str, folder: Folder) -> list[ObjectNode]:
    if folder not in _SCHEMA_FOLDERS:
        raise ValidationFailed(f"Folder '{folder}' is not listed at schema level")
    found = _object_rows(conn, schema, folder)
    if not found:  # a folder is listed only when it has objects: empty means stale, or the schema is gone
        (schema_id,) = first_row(conn, f"SELECT SCHEMA_ID({literal(schema)})")
        if schema_id is None:
            raise NotFound(f"Schema '{schema}' not found")
    kind = _KIND_OF_FOLDER[folder]
    return [make_node(path, kind, name, has_children=kind in _HAS_FOLDERS, detail=detail) for name, detail in found]


def _table_folders(conn: driver.Connection, path: list[PathSegment], schema: str, table: PathSegment) -> list[ObjectNode]:
    kind = NodeKind(table.kind)
    counts = dict(rows(conn, _TABLE_FOLDER_COUNTS.format(
        o=object_id_sql(schema, table.name), t=literal(_RELATION_TYPE[kind]))))
    if not counts["exists"]:
        _require_relation(conn, schema, table.name, kind)  # tells a dropped schema from a dropped table
        raise NotFound(f"{kind} '{table.name}' not found")
    folders = [Folder.COLUMNS]
    if kind == NodeKind.TABLE:  # a view has no foreign keys or triggers, and no indexes the tree shows
        folders += [folder for folder in (Folder.INDEXES, Folder.FOREIGN_KEYS, Folder.TRIGGERS) if counts[folder]]
    return [make_node(path, NodeKind.FOLDER, folder, has_children=True) for folder in folders]


def _column_nodes(conn: driver.Connection, path: list[PathSegment], o: str) -> list[ObjectNode]:
    infos = (col.info() for col in _columns(conn, o))
    return [make_node(path, NodeKind.COLUMN, info.name, has_children=False, detail=_column_detail(info)) for info in infos]


def _index_nodes(conn: driver.Connection, path: list[PathSegment], o: str) -> list[ObjectNode]:
    return [
        make_node(path, NodeKind.INDEX, name, has_children=False,
                  detail="primary" if is_primary else "unique" if is_unique else None)
        for name, is_primary, is_unique in rows(conn, _INDEX_NODES.format(o=o))
    ]


def _foreign_key_nodes(conn: driver.Connection, path: list[PathSegment], o: str) -> list[ObjectNode]:
    nodes = []
    for name, parts in _grouped(rows(conn, _FOREIGN_KEYS.format(o=o))).items():
        columns = ", ".join(p[1] for p in parts)
        ref_columns = ", ".join(p[4] for p in parts)
        detail = f"({columns}) → {parts[0][2]}.{parts[0][3]} ({ref_columns})"
        nodes.append(make_node(path, NodeKind.FOREIGN_KEY, name, has_children=False, detail=detail))
    return nodes


def _trigger_nodes(conn: driver.Connection, path: list[PathSegment], o: str) -> list[ObjectNode]:
    """Events are joined here: STRING_AGG needs SQL Server 2017, and 2016 is supported."""
    return [
        make_node(path, NodeKind.TRIGGER, name, has_children=False,
                  detail=f"{'instead of' if parts[0][1] else 'after'} {', '.join(p[2] for p in parts)}")
        for name, parts in _grouped(rows(conn, _TRIGGERS.format(o=o))).items()
    ]


_TABLE_LEAVES = {
    Folder.COLUMNS: _column_nodes,
    Folder.INDEXES: _index_nodes,
    Folder.FOREIGN_KEYS: _foreign_key_nodes,
    Folder.TRIGGERS: _trigger_nodes,
}


def _table_children(conn: driver.Connection, path: list[PathSegment], schema: str, folder: Folder) -> list[ObjectNode]:
    if folder not in _TABLE_LEAVES:
        raise ValidationFailed(f"Folder '{folder}' is not listed under a table")
    table = path[3]
    nodes = _TABLE_LEAVES[folder](conn, path, object_id_sql(schema, table.name))
    if not nodes:  # a table always has columns and a folder is listed only when it has leaves: stale, or gone
        _require_relation(conn, schema, table.name, table.kind)
    return nodes


# ---- generated DDL -------------------------------------------------------------------------------------------------


def _column_line(c: _Column, named: bool) -> str:
    if c.computed is not None:
        return f"{q(c.name)} AS {c.computed}{' PERSISTED' if c.persisted else ''}"
    type_text = f"{q(c.user_schema)}.{q(c.type_name)}" if c.user_schema else type_sql(c.type_name, c.max_length, c.precision, c.scale)
    parts = [q(c.name), type_text]
    if c.collation and c.collation != c.database_collation:
        parts.append(f"COLLATE {c.collation}")
    if c.identity:
        parts.append(f"IDENTITY({c.seed}, {c.increment})")
    parts.append("NULL" if c.nullable else "NOT NULL")
    if c.default is not None:
        parts.append(f"CONSTRAINT {q(c.default_name)} DEFAULT {c.default}" if named else f"DEFAULT {c.default}")
    return " ".join(parts)


def _foreign_key_clause(name: str, parts: list[tuple]) -> str:
    """parts: _FOREIGN_KEYS rows of one key, in key order."""
    _, _, ref_schema, ref_table, _, on_delete, on_update = parts[0]
    columns = ", ".join(q(p[1]) for p in parts)
    ref_columns = ", ".join(q(p[4]) for p in parts)
    return (f"CONSTRAINT {q(name)} FOREIGN KEY ({columns}) REFERENCES {q(ref_schema)}.{q(ref_table)} ({ref_columns}) "
            f"ON DELETE {_ACTIONS[on_delete]} ON UPDATE {_ACTIONS[on_update]}")


def _index_statement(schema: str, table: str, name: str, parts: list[tuple]) -> str:
    """parts: _INDEXES rows of one index. A primary key or unique constraint becomes ALTER TABLE … ADD CONSTRAINT."""
    keys = ", ".join(q(p[4]) + (" DESC" if p[5] else "") for p in parts if not p[6])
    target = f"{q(schema)}.{q(table)}"
    is_unique, type_desc, filter_definition, is_primary, is_constraint = parts[0][1], parts[0][2], parts[0][3], parts[0][7], parts[0][8]
    if is_primary or is_constraint:
        return f"ALTER TABLE {target} ADD CONSTRAINT {q(name)} {'PRIMARY KEY' if is_primary else 'UNIQUE'} {type_desc} ({keys});"
    included = [q(p[4]) for p in parts if p[6]]
    sql = f"CREATE {'UNIQUE ' if is_unique else ''}{type_desc} INDEX {q(name)} ON {target} ({keys})"
    if included:
        sql += f" INCLUDE ({', '.join(included)})"
    if filter_definition:
        sql += f" WHERE {filter_definition}"
    return sql + ";"


def _body_lines(conn: driver.Connection, object_sql: str, columns: list[_Column], named: bool) -> list[str]:
    """Columns, then PRIMARY KEY / UNIQUE, then CHECK constraints. `named`: a table names its constraints; a table type
    may not."""
    lines = [_column_line(c, named) for c in columns]
    for name, parts in _grouped(rows(conn, _KEYS.format(o=object_sql))).items():
        kind = "PRIMARY KEY" if parts[0][1] else "UNIQUE"
        key_columns = ", ".join(q(p[3]) + (" DESC" if p[4] else "") for p in parts)
        lines.append(f"{f'CONSTRAINT {q(name)} ' if named else ''}{kind} {parts[0][2]} ({key_columns})")
    for name, definition in rows(conn, _CHECKS.format(o=object_sql)):
        lines.append(f"{f'CONSTRAINT {q(name)} ' if named else ''}CHECK {definition}")
    return lines


def _table_ddl(conn: driver.Connection, schema: str, table: str, columns: list[_Column]) -> str:
    o = object_id_sql(schema, table)
    lines = _body_lines(conn, o, columns, named=True)
    lines += [_foreign_key_clause(name, parts) for name, parts in _grouped(rows(conn, _FOREIGN_KEYS.format(o=o))).items()]
    statements = ["CREATE TABLE {} (\n    {}\n);".format(f"{q(schema)}.{q(table)}", ",\n    ".join(lines))]
    for name, parts in _grouped(rows(conn, _INDEXES.format(o=o))).items():
        if not (parts[0][7] or parts[0][8]):  # constraint indexes are already in the body
            statements.append(_index_statement(schema, table, name, parts))
    return "\n".join(statements)


def _sequence_ddl(conn: driver.Connection, schema: str, name: str) -> str:
    row = first_row(conn, _SEQUENCE.format(o=object_id_sql(schema, name)))
    if row is None:
        raise _not_found(NodeKind.SEQUENCE, schema, name)
    type_name, start, increment, minimum, maximum, is_cycling, is_cached, cache_size, precision, scale = row
    cache = f"CACHE {cache_size}" if is_cached and cache_size is not None else "CACHE" if is_cached else "NO CACHE"
    return (f"CREATE SEQUENCE {q(schema)}.{q(name)} AS {type_sql(type_name, 0, precision, scale)} START WITH {start} "
            f"INCREMENT BY {increment} MINVALUE {minimum} MAXVALUE {maximum} {'CYCLE' if is_cycling else 'NO CYCLE'} {cache};")


def _synonym_ddl(conn: driver.Connection, schema: str, name: str) -> str:
    row = first_row(conn, _SYNONYM.format(o=object_id_sql(schema, name)))
    if row is None:
        raise _not_found(NodeKind.SYNONYM, schema, name)
    return f"CREATE SYNONYM {q(schema)}.{q(name)} FOR {row[0]};"


def _type_ddl(conn: driver.Connection, schema: str, name: str) -> str | None:
    """None for a CLR type: its assembly is not something a catalog query can write back."""
    ref = name_literal(schema, name)
    row = first_row(conn, _TYPE.format(ref=ref))
    if row is None:
        raise _not_found(NodeKind.TYPE, schema, name)
    _, is_table_type, base, max_length, precision, scale, nullable = row
    target = f"{q(schema)}.{q(name)}"
    if is_table_type:
        body_object = _TABLE_TYPE_OBJECT.format(ref=ref)
        lines = _body_lines(conn, body_object, _columns(conn, body_object), named=False)
        return "CREATE TYPE {} AS TABLE (\n    {}\n);".format(target, ",\n    ".join(lines))
    if base is None:
        logger.debug("SQL Server type {}.{} has no base type (a CLR type): no DDL", schema, name)
        return None
    return f"CREATE TYPE {target} FROM {type_sql(base, max_length, precision, scale)} {'NULL' if nullable else 'NOT NULL'};"


# ---- details -------------------------------------------------------------------------------------------------------


def _not_found(kind: NodeKind | str, schema: str, name: str) -> NotFound:
    logger.debug("SQL Server {} {}.{} not found: the catalog has no such object", kind, schema, name)
    return NotFound(f"{kind} '{name}' not found")


def _definition(conn: driver.Connection, kind: NodeKind | str, schema: str, name: str) -> str | None:
    """OBJECT_DEFINITION: the server's own text. NULL (an encrypted module, or an account without VIEW DEFINITION) is
    no DDL, not an error: the object is there and keeps its columns."""
    object_id, definition = first_row(conn, _DEFINITION.format(ref=name_literal(schema, name)))
    if object_id is None:
        raise _not_found(kind, schema, name)
    if definition is None:
        logger.debug("SQL Server OBJECT_DEFINITION of {} {}.{} is NULL: encrypted, or no VIEW DEFINITION", kind, schema, name)
    return definition


@dataclass(frozen=True, slots=True)
class _Found:
    """What a detail builder returns; the dispatcher turns it into an ObjectDetail."""

    ddl: str | None = None
    generated: bool = False
    columns: list[ColumnInfo] | None = None
    comment: str | None = None


_Detail = Callable[[driver.Connection, list[PathSegment], str], _Found]  # (conn, path, path[1].name)


def _table_detail(conn: driver.Connection, path: list[PathSegment], schema: str) -> _Found:
    name = path[-1].name
    columns = _columns(conn, object_id_sql(schema, name))
    if not columns:  # a table always has a column
        raise _not_found(path[-1].kind, schema, name)
    ddl = _table_ddl(conn, schema, name, columns)
    row = first_row(conn, _TABLE_COMMENT.format(o=object_id_sql(schema, name)))
    return _Found(ddl, True, [c.info() for c in columns], (row[0] or None) if row else None)


def _view_detail(conn: driver.Connection, path: list[PathSegment], schema: str) -> _Found:
    name = path[-1].name
    ddl = _definition(conn, path[-1].kind, schema, name)
    return _Found(ddl, False, [c.info() for c in _columns(conn, object_id_sql(schema, name))])


def _module_detail(conn: driver.Connection, path: list[PathSegment], schema: str) -> _Found:
    return _Found(_definition(conn, path[-1].kind, schema, path[-1].name))


def _generated_detail(build: Callable[[driver.Connection, str, str], str | None]) -> _Detail:
    """A detail whose DDL a catalog query writes: build(conn, schema, name), None when there is nothing to write."""

    def detail(conn: driver.Connection, path: list[PathSegment], schema: str) -> _Found:
        ddl = build(conn, schema, path[-1].name)
        return _Found(ddl, ddl is not None)
    return detail


def _table_leaf(path: list[PathSegment]) -> tuple[str, str]:
    """(schema, table) of an index, foreign key or column: the table sits at path[3]."""
    if len(path) < 5:
        raise ValidationFailed(f"A {path[-1].kind} belongs to a table: expected a path inside one")
    return path[1].name, path[3].name


def _index_detail(conn: driver.Connection, path: list[PathSegment], _schema: str) -> _Found:
    schema, table = _table_leaf(path)
    parts = _grouped(rows(conn, _INDEXES.format(o=object_id_sql(schema, table)))).get(path[-1].name)
    if parts is None:
        raise _not_found(path[-1].kind, schema, path[-1].name)
    return _Found(_index_statement(schema, table, path[-1].name, parts), True)


def _foreign_key_detail(conn: driver.Connection, path: list[PathSegment], _schema: str) -> _Found:
    schema, table = _table_leaf(path)
    name = path[-1].name
    parts = _grouped(rows(conn, _FOREIGN_KEYS.format(o=object_id_sql(schema, table)))).get(name)
    if parts is None:
        raise _not_found(path[-1].kind, schema, name)
    return _Found(f"ALTER TABLE {q(schema)}.{q(table)} ADD {_foreign_key_clause(name, parts)};", True)


def _column_leaf_detail(conn: driver.Connection, path: list[PathSegment], _schema: str) -> _Found:
    schema, table = _table_leaf(path)
    match = [c.info() for c in _columns(conn, object_id_sql(schema, table)) if c.name == path[-1].name]
    if not match:
        raise _not_found(path[-1].kind, schema, path[-1].name)
    return _Found(columns=match)


_DETAILS: dict[NodeKind, _Detail] = {
    NodeKind.TABLE: _table_detail,
    NodeKind.VIEW: _view_detail,
    NodeKind.PROCEDURE: _module_detail,
    NodeKind.FUNCTION: _module_detail,
    NodeKind.TRIGGER: _module_detail,  # a trigger's schema is its table's: path[1]
    NodeKind.SEQUENCE: _generated_detail(_sequence_ddl),
    NodeKind.SYNONYM: _generated_detail(_synonym_ddl),
    NodeKind.TYPE: _generated_detail(_type_ddl),
    NodeKind.INDEX: _index_detail,
    NodeKind.FOREIGN_KEY: _foreign_key_detail,
    NodeKind.COLUMN: _column_leaf_detail,
}


def object_detail(conn: driver.Connection, path: list[PathSegment]) -> ObjectDetail:
    if not path:
        raise ValidationFailed("Empty path")
    _check_head(path)
    last = path[-1]
    kind = NodeKind(last.kind) if last.kind in NodeKind else None
    if kind not in _DETAILS:
        raise ValidationFailed(f"No details available for {last.kind}")
    found = _DETAILS[kind](conn, path, path[1].name)
    return ObjectDetail(
        path=path, kind=last.kind, name=last.name, ddl=found.ddl, ddl_is_generated=found.generated and found.ddl is not None,
        columns=found.columns, comment=found.comment,
    )
