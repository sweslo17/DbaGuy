"""The SQL Server object tree and object details, over a connection already bound to the database `path[0]` names.

Tree: database > schema > folder > object, and table|view > folder (columns, indexes, foreign_keys, triggers) > leaf.
Every name reaches SQL through `q()`, `literal()` or `object_id_sql()` (catalog.py); a statement never takes a driver
parameter.

Round trips (over an SSH tunnel every one counts): every tree call is one query, because whether the schema or table a
call names still exists rides on the level's own count query (an `exists` row) or is asked only after an answer came
back empty. A detail that needs several catalog queries (a table's columns, keys, checks, foreign keys, indexes and
comment) sends them as one batch.

Limited accounts: without VIEW DEFINITION the catalog still lists an object's columns, constraints and indexes, but
hides their definitions (a computed column's expression, a default, a check, an index filter) and the user-defined types
that are not the account's own. A column then falls back to its base type and the names stay, but DDL that would
need a hidden definition is not written: such an object has `ddl=None` and keeps its columns, never a CREATE statement
that quietly differs from the real object.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Context, Decimal

from loguru import logger

from ...domain.errors import NotFound, ValidationFailed
from ...domain.metadata import ColumnInfo, Folder, NodeKind, ObjectDetail, ObjectNode, PathSegment, make_node
from . import driver
from .catalog import (
    SYSTEM_DATABASES, PerObject, batch, by_object, first_row, id_sources, is_system_schema, literal, name_literal,
    object_id_sql, q, result_sets, rows, type_sql)

HIDDEN = "?"  # what the tree shows for a name the account may not see
# Sequence and identity values go up to decimal(38, 0): their sums are exact (the default context keeps 28 digits)
EXACT = Context(prec=80)
# Their objects are system objects: listed from sys.all_views / sys.all_objects, not from the user-object views.
_CATALOG_SCHEMAS = frozenset({"sys", "INFORMATION_SCHEMA"})

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
_CATALOG_FOLDER_COUNTS = """
SELECT 'views', COUNT(*) FROM sys.all_views WHERE schema_id = SCHEMA_ID({s})
UNION ALL SELECT 'functions', COUNT(*) FROM sys.all_objects WHERE schema_id = SCHEMA_ID({s}) AND type IN ('FN', 'IF', 'TF', 'FS', 'FT')
UNION ALL SELECT 'procedures', COUNT(*) FROM sys.all_objects WHERE schema_id = SCHEMA_ID({s}) AND type IN ('P', 'PC', 'RF', 'X')
UNION ALL SELECT 'exists', CASE WHEN SCHEMA_ID({s}) IS NULL THEN 0 ELSE 1 END
"""
_FUNCTION_DETAIL = "CASE WHEN type IN ('FN', 'FS') THEN 'scalar' WHEN type = 'IF' THEN 'inline table' ELSE 'table' END"
_OBJECTS: dict[Folder, str] = {  # (name, detail)
    Folder.TABLES: "SELECT name, NULL FROM sys.tables WHERE schema_id = SCHEMA_ID({s}) ORDER BY name",
    Folder.VIEWS: "SELECT name, NULL FROM sys.views WHERE schema_id = SCHEMA_ID({s}) ORDER BY name",
    Folder.FUNCTIONS: f"SELECT name, {_FUNCTION_DETAIL} FROM sys.objects WHERE schema_id = SCHEMA_ID({{s}}) "
                      "AND type IN ('FN', 'IF', 'TF', 'FS', 'FT') ORDER BY name",
    Folder.PROCEDURES: "SELECT name, NULL FROM sys.procedures WHERE schema_id = SCHEMA_ID({s}) ORDER BY name",
    Folder.SEQUENCES: "SELECT name, TYPE_NAME(user_type_id) FROM sys.sequences WHERE schema_id = SCHEMA_ID({s}) ORDER BY name",
    Folder.SYNONYMS: "SELECT name, base_object_name FROM sys.synonyms WHERE schema_id = SCHEMA_ID({s}) ORDER BY name",
}
_CATALOG_OBJECTS: dict[Folder, str] = {  # what `sys` and `INFORMATION_SCHEMA` hold
    Folder.VIEWS: "SELECT name, NULL FROM sys.all_views WHERE schema_id = SCHEMA_ID({s}) ORDER BY name",
    Folder.FUNCTIONS: f"SELECT name, {_FUNCTION_DETAIL} FROM sys.all_objects WHERE schema_id = SCHEMA_ID({{s}}) "
                      "AND type IN ('FN', 'IF', 'TF', 'FS', 'FT') ORDER BY name",
    Folder.PROCEDURES: "SELECT name, NULL FROM sys.all_objects WHERE schema_id = SCHEMA_ID({s}) "
                       "AND type IN ('P', 'PC', 'RF', 'X') ORDER BY name",
}
_TYPE_COLUMNS = """
SELECT t.name, t.is_table_type, bt.name, t.max_length, t.precision, t.scale, t.is_nullable
FROM sys.types t LEFT JOIN sys.types bt ON bt.user_type_id = t.system_type_id
WHERE t.is_user_defined = 1 AND """
_TYPES = _TYPE_COLUMNS + "t.schema_id = SCHEMA_ID({s}) ORDER BY t.name"
_TYPE = _TYPE_COLUMNS + "t.user_type_id = TYPE_ID({ref})"
_TYPES_OF = """
SELECT t.user_type_id, t.name, t.is_table_type, bt.name, t.max_length, t.precision, t.scale, t.is_nullable
FROM sys.types t LEFT JOIN sys.types bt ON bt.user_type_id = t.system_type_id
WHERE t.is_user_defined = 1 AND t.user_type_id IN ({ids})
"""
_TABLE_TYPE_OBJECTS = "SELECT type_table_object_id FROM sys.table_types WHERE user_type_id IN ({ids})"
_TABLE_TYPE_OF = "SELECT type_table_object_id, user_type_id FROM sys.table_types WHERE user_type_id IN ({ids})"
_TABLE_FOLDER_COUNTS = """
SELECT 'indexes', COUNT(*) FROM sys.indexes WHERE object_id = {o} AND type IN (1, 2)
UNION ALL SELECT 'foreign_keys', COUNT(*) FROM sys.foreign_keys WHERE parent_object_id = {o}
UNION ALL SELECT 'triggers', COUNT(*) FROM sys.triggers WHERE parent_id = {o}
UNION ALL SELECT 'exists', COUNT(*) FROM sys.all_objects WHERE object_id = {o} AND type = {t}
"""
# sys.all_columns: a system view (`sys.objects`) has columns too. A user type the account may not see has no row in
# sys.types: the column falls back to its base type (system_type_id), and `type_hidden` says so.
_COLUMNS = PerObject("""
SELECT {key}c.name, COALESCE(t.name, st.name, N'?'), c.max_length, c.precision, c.scale, c.is_nullable, c.is_identity,
       CAST(ic.seed_value AS nvarchar(40)), CAST(ic.increment_value AS nvarchar(40)), cc.definition, cc.is_persisted,
       dc.name, dc.definition, CASE WHEN t.is_user_defined = 1 THEN SCHEMA_NAME(t.schema_id) END, c.collation_name,
       CONVERT(nvarchar(128), DATABASEPROPERTYEX(DB_NAME(), 'Collation')),
       CASE WHEN EXISTS (SELECT 1 FROM sys.indexes i JOIN sys.index_columns k
                         ON k.object_id = i.object_id AND k.index_id = i.index_id
                         WHERE i.is_primary_key = 1 AND k.object_id = c.object_id AND k.column_id = c.column_id)
            THEN 1 ELSE 0 END,
       CAST(ep.value AS nvarchar(4000)),
       c.is_computed, CASE WHEN c.default_object_id <> 0 THEN 1 ELSE 0 END, CASE WHEN t.name IS NULL THEN 1 ELSE 0 END,
       dc.is_system_named, c.is_sparse, c.is_column_set, c.is_rowguidcol, mc.masking_function, ic.is_not_for_replication
FROM sys.all_columns c LEFT JOIN sys.types t ON t.user_type_id = c.user_type_id
LEFT JOIN sys.types st ON st.user_type_id = c.system_type_id
LEFT JOIN sys.identity_columns ic ON ic.object_id = c.object_id AND ic.column_id = c.column_id
LEFT JOIN sys.computed_columns cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id
LEFT JOIN sys.default_constraints dc ON dc.parent_object_id = c.object_id AND dc.parent_column_id = c.column_id
LEFT JOIN sys.extended_properties ep
       ON ep.class = 1 AND ep.major_id = c.object_id AND ep.minor_id = c.column_id AND ep.name = N'MS_Description'
LEFT JOIN sys.masked_columns mc ON mc.object_id = c.object_id AND mc.column_id = c.column_id
WHERE {where} ORDER BY {order}c.column_id
""", "c.object_id")
# A constraint's index has the constraint's name: sys.key_constraints is joined through sys.objects by it, a seek, where
# joining it by (parent_object_id, unique_index_id) costs ~1.3 ms per table (a dump of 1,000 tables: 1.3 s, twice)
_KEYS = PerObject("""
SELECT {key}i.name, i.is_primary_key, i.type_desc, COL_NAME(k.object_id, k.column_id), k.is_descending_key,
       kc.is_system_named
FROM sys.indexes i JOIN sys.index_columns k ON k.object_id = i.object_id AND k.index_id = i.index_id
LEFT JOIN sys.objects ko ON ko.parent_object_id = i.object_id AND ko.name = i.name AND ko.type IN ('PK', 'UQ')
LEFT JOIN sys.key_constraints kc ON kc.object_id = ko.object_id
WHERE {where} AND (i.is_primary_key = 1 OR i.is_unique_constraint = 1)
ORDER BY {order}i.is_primary_key DESC, i.name, k.key_ordinal
""", "i.object_id")
_CHECKS = PerObject("""
SELECT {key}name, definition, is_system_named, is_disabled, is_not_trusted, is_not_for_replication
FROM sys.check_constraints WHERE {where} ORDER BY {order}name
""", "parent_object_id")
_FOREIGN_KEYS = PerObject("""
SELECT {key}f.name, COL_NAME(fc.parent_object_id, fc.parent_column_id), OBJECT_SCHEMA_NAME(f.referenced_object_id),
       OBJECT_NAME(f.referenced_object_id), COL_NAME(fc.referenced_object_id, fc.referenced_column_id),
       f.delete_referential_action_desc, f.update_referential_action_desc, f.is_system_named, f.is_disabled,
       f.is_not_trusted, f.is_not_for_replication
FROM sys.foreign_keys f JOIN sys.foreign_key_columns fc ON fc.constraint_object_id = f.object_id
WHERE {where} ORDER BY {order}f.name, fc.constraint_column_id
""", "f.parent_object_id")
_INDEXES = PerObject("""
SELECT {key}i.name, i.is_unique, i.type_desc, i.filter_definition, COL_NAME(k.object_id, k.column_id),
       k.is_descending_key, k.is_included_column, i.is_primary_key, i.is_unique_constraint, kc.is_system_named,
       i.has_filter
FROM sys.indexes i JOIN sys.index_columns k ON k.object_id = i.object_id AND k.index_id = i.index_id
LEFT JOIN sys.objects ko ON ko.parent_object_id = i.object_id AND ko.name = i.name AND ko.type IN ('PK', 'UQ')
LEFT JOIN sys.key_constraints kc ON kc.object_id = ko.object_id
WHERE {where} AND i.type IN (1, 2)
ORDER BY {order}i.is_primary_key DESC, i.name, k.is_included_column, k.key_ordinal, k.index_column_id
""", "i.object_id")
_INDEX_NODES = (
    "SELECT name, is_primary_key, is_unique FROM sys.indexes WHERE object_id = {o} AND type IN (1, 2) "
    "ORDER BY is_primary_key DESC, name"
)
_TRIGGERS = """
SELECT t.name, t.is_instead_of_trigger, LOWER(e.type_desc) FROM sys.triggers t
JOIN sys.trigger_events e ON e.object_id = t.object_id WHERE t.parent_id = {o} ORDER BY t.name, e.type
"""
_SEQUENCE_FIELDS = """TYPE_NAME(user_type_id), CAST(start_value AS nvarchar(40)), CAST(increment AS nvarchar(40)),
       CAST(minimum_value AS nvarchar(40)), CAST(maximum_value AS nvarchar(40)), is_cycling, is_cached, cache_size,
       precision, scale"""
_SEQUENCE = f"SELECT {_SEQUENCE_FIELDS} FROM sys.sequences WHERE object_id = {{o}}"
_SYNONYM = "SELECT base_object_name FROM sys.synonyms WHERE object_id = {o}"
_SYNONYMS_OF = "SELECT object_id, base_object_name FROM sys.synonyms WHERE object_id IN ({ids})"
_DEFINITION = "SELECT OBJECT_ID({ref}), OBJECT_DEFINITION(OBJECT_ID({ref}))"
_TABLE_COMMENT = ("SELECT CAST(value AS nvarchar(4000)) FROM sys.extended_properties "
                  "WHERE class = 1 AND major_id = {o} AND minor_id = 0 AND name = N'MS_Description'")
_TABLE_TYPE_OBJECT = "(SELECT type_table_object_id FROM sys.table_types WHERE user_type_id = TYPE_ID({ref}))"
# Run only when an answer came back empty, to tell an empty answer from a schema or table that is gone.
_PRESENCE = "SELECT SCHEMA_ID({s}), (SELECT COUNT(*) FROM sys.all_objects WHERE object_id = {o} AND type = {t})"

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


class _Withheld(Exception):
    """The catalog hides a definition the generated DDL needs: there is no DDL to write (it is not an error)."""


def _need[T](value: T | None, what: str) -> T:
    if value is None:
        raise _Withheld(what)
    return value


def _quote(name: str | None) -> str:
    return q(_need(name, "a name"))


def _written(kind: NodeKind | str, schema: str, name: str, build: Callable[[], str | None]) -> str | None:
    """build(), or None when it finds a definition the account may not see."""
    try:
        return build()
    except _Withheld as exc:
        logger.debug("SQL Server DDL of {} {}.{} withheld: the catalog hides {}", kind, schema, name, exc)
        return None


@dataclass(frozen=True, slots=True)
class _Column:
    """One row of _COLUMNS."""

    name: str
    type_name: str  # the alias name, else the built-in type (the base type of an alias the account may not see)
    max_length: int
    precision: int
    scale: int
    nullable: bool
    identity: bool
    seed: str | None
    increment: str | None
    computed: str | None  # the expression; NULL for a plain column, and for a computed one whose text is hidden
    persisted: bool | None
    default_name: str | None
    default: str | None  # likewise: NULL when there is none, and when it is hidden
    user_schema: str | None  # set for an alias type: the type is then written schema-qualified
    collation: str | None
    database_collation: str | None
    primary_key: bool
    comment: str | None
    is_computed: bool
    has_default: bool
    type_hidden: bool  # a user-defined type that sys.types does not show to this account
    default_system_named: bool | None
    sparse: bool = False
    column_set: bool = False  # `xml COLUMN_SET FOR ALL_SPARSE_COLUMNS`: the table's sparse columns as one XML value
    rowguidcol: bool = False
    masking: str | None = None  # dynamic data masking's function, `partial(1, "X", 0)`
    identity_not_for_replication: bool | None = None

    def info(self) -> ColumnInfo:
        """An alias type keeps its name; a built-in type is written as T-SQL writes it."""
        type_name = self.type_name if self.user_schema else type_sql(self.type_name, self.max_length, self.precision, self.scale)
        return ColumnInfo(
            name=self.name, type_name=type_name, nullable=bool(self.nullable), default=self.default,
            is_primary_key=bool(self.primary_key), comment=self.comment or None,
        )


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
    """Every schema, user schemas first, each group by name. Of the system schemas only `sys` and INFORMATION_SCHEMA
    hold anything (system views, functions, procedures); `guest` and the database roles' schemas own nothing."""
    found = [(name, is_system_schema(name, schema_id)) for name, schema_id in rows(conn, _SCHEMAS)]
    found.sort(key=lambda schema: (schema[1], schema[0].casefold(), schema[0]))
    return [
        make_node(path, NodeKind.SCHEMA, name, has_children=not is_system or name in _CATALOG_SCHEMAS, system=is_system)
        for name, is_system in found
    ]


def _schema_folders(conn: driver.Connection, path: list[PathSegment], schema: str) -> list[ObjectNode]:
    sql = _CATALOG_FOLDER_COUNTS if schema in _CATALOG_SCHEMAS else _FOLDER_COUNTS
    counts = dict(rows(conn, sql.format(s=literal(schema))))
    if not counts["exists"]:
        raise NotFound(f"Schema '{schema}' not found")
    return [make_node(path, NodeKind.FOLDER, folder, has_children=True) for folder in _SCHEMA_FOLDERS if counts.get(folder)]


def _object_rows(conn: driver.Connection, schema: str, folder: Folder) -> list[tuple]:
    """(name, detail) of the schema's objects in a folder."""
    if schema in _CATALOG_SCHEMAS:
        sql = _CATALOG_OBJECTS.get(folder)
        return rows(conn, sql.format(s=literal(schema))) if sql else []
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


def _columns(conn: driver.Connection, object_sql: str) -> list[_Column]:
    return [_Column(*row) for row in rows(conn, _COLUMNS.one(object_sql))]


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
    """A key to a table the account may not see shows `?` for what it cannot name."""
    nodes = []
    for name, parts in _grouped(rows(conn, _FOREIGN_KEYS.one(o))).items():
        columns = ", ".join(p[1] or HIDDEN for p in parts)
        ref_columns = ", ".join(p[4] or HIDDEN for p in parts)
        detail = f"({columns}) → {parts[0][2] or HIDDEN}.{parts[0][3] or HIDDEN} ({ref_columns})"
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


def _constraint(name: str, system_named: bool | None, named: bool) -> str:
    """`CONSTRAINT [name] ` for a constraint someone named. Nothing for a table type's (it may not) nor for a name the
    server made up (`PK__t__3213E83F…`): without it the DDL reads like the DDL a person would write."""
    return f"CONSTRAINT {q(name)} " if named and not system_named else ""


def _column_line(c: _Column, named: bool, later: frozenset[str] = frozenset()) -> str:
    """`later`: constraint names the dump adds after the data (a default among them is left out of the line)."""
    if c.is_computed:
        line = f"{_quote(c.name)} AS {_need(c.computed, 'a computed column definition')}"
        if c.persisted:
            line += " PERSISTED" + ("" if c.nullable else " NOT NULL")
        return line
    if c.type_hidden:
        raise _Withheld(f"the type of column {c.name}")
    if c.column_set:
        return f"{q(c.name)} xml COLUMN_SET FOR ALL_SPARSE_COLUMNS"
    type_text = f"{q(c.user_schema)}.{q(c.type_name)}" if c.user_schema else type_sql(c.type_name, c.max_length, c.precision, c.scale)
    parts = [q(c.name), type_text]
    if c.collation and c.collation != c.database_collation:
        parts.append(f"COLLATE {c.collation}")
    if c.sparse:
        parts.append("SPARSE")
    if c.masking:
        parts.append(f"MASKED WITH (FUNCTION = {literal(c.masking)})")
    if c.identity:
        parts.append(f"IDENTITY({_need(c.seed, 'an identity seed')}, {_need(c.increment, 'an identity increment')})"
                     + (" NOT FOR REPLICATION" if c.identity_not_for_replication else ""))
    parts.append("NULL" if c.nullable else "NOT NULL")
    if c.rowguidcol:
        parts.append("ROWGUIDCOL")
    if c.has_default and c.default_name not in later:
        default = _need(c.default, "a default definition")
        parts.append(f"{_constraint(c.default_name, c.default_system_named, named)}DEFAULT {default}")
    return " ".join(parts)


def _foreign_key_clause(name: str, parts: list[tuple], *, keep_name: bool = False) -> str:
    """parts: _FOREIGN_KEYS rows of one key, in key order. `keep_name`: also a name the server made up (a dump: its
    `drop_first` DROP names the key, pre-flight H7)."""
    _, _, ref_schema, ref_table, _, on_delete, on_update, system_named, _, _, not_for_replication = parts[0]
    columns = ", ".join(_quote(p[1]) for p in parts)
    ref_columns = ", ".join(_quote(p[4]) for p in parts)
    named = f"CONSTRAINT {q(name)} " if keep_name else _constraint(name, system_named, True)
    return (f"{named}FOREIGN KEY ({columns}) "
            f"REFERENCES {_quote(ref_schema)}.{_quote(ref_table)} ({ref_columns}) "
            f"ON DELETE {_ACTIONS[_need(on_delete, 'an action')]} ON UPDATE {_ACTIONS[_need(on_update, 'an action')]}"
            + (" NOT FOR REPLICATION" if not_for_replication else ""))


def _index_statement(schema: str, table: str, name: str, parts: list[tuple]) -> str:
    """parts: _INDEXES rows of one index. A primary key or unique constraint becomes ALTER TABLE … ADD CONSTRAINT."""
    keys = ", ".join(_quote(p[4]) + (" DESC" if p[5] else "") for p in parts if not p[6])
    target = f"{q(schema)}.{q(table)}"
    is_unique, type_desc, filter_definition, is_primary, is_constraint = parts[0][1], parts[0][2], parts[0][3], parts[0][7], parts[0][8]
    system_named, has_filter = parts[0][9], parts[0][10]
    if is_primary or is_constraint:
        return f"ALTER TABLE {target} ADD {_constraint(name, system_named, True)}{'PRIMARY KEY' if is_primary else 'UNIQUE'} {type_desc} ({keys});"
    included = [_quote(p[4]) for p in parts if p[6]]
    sql = f"CREATE {'UNIQUE ' if is_unique else ''}{type_desc} INDEX {q(name)} ON {target} ({keys})"
    if included:
        sql += f" INCLUDE ({', '.join(included)})"
    if has_filter:
        sql += f" WHERE {_need(filter_definition, 'an index filter')}"
    return sql + ";"


def _body_lines(columns: list[_Column], keys: list[tuple], checks: list[tuple], named: bool,
                later: frozenset[str] = frozenset()) -> list[str]:
    """Columns, then PRIMARY KEY / UNIQUE, then CHECK constraints. `named`: a table names its constraints; a table type
    may not. keys: _KEYS rows; checks: _CHECKS rows; `later`: the defaults and checks a dump adds after the data."""
    lines = [_column_line(c, named, later) for c in columns]
    for name, parts in _grouped(keys).items():
        kind = "PRIMARY KEY" if parts[0][1] else "UNIQUE"
        key_columns = ", ".join(_quote(p[3]) + (" DESC" if p[4] else "") for p in parts)
        lines.append(f"{_constraint(name, parts[0][5], named)}{kind} {parts[0][2]} ({key_columns})")
    for name, definition, system_named, _, _, not_for_replication in checks:
        if name not in later:
            lines.append(f"{_constraint(name, system_named, named)}{_check(definition, not_for_replication)}")
    return lines


def _inline_index(name: str, parts: list[tuple]) -> str:
    """A table type's own index, in its body (`INDEX [ix] NONCLUSTERED ([b])`): _INDEXES rows of one index. A table
    type's index has neither INCLUDE nor a filter."""
    if any(p[6] for p in parts) or parts[0][10]:
        raise _Withheld("an index a table type cannot have")
    keys = ", ".join(_quote(p[4]) + (" DESC" if p[5] else "") for p in parts)
    return f"INDEX {q(name)} {'UNIQUE ' if parts[0][1] else ''}{parts[0][2]} ({keys})"


def _check(definition: str | None, not_for_replication: bool) -> str:
    return f"CHECK {'NOT FOR REPLICATION ' if not_for_replication else ''}{_need(definition, 'a check definition')}"


def _table_ddl(schema: str, table: str, columns: list[_Column], keys: list[tuple], checks: list[tuple],
               foreign_keys: list[tuple], indexes: list[tuple]) -> str:
    lines = _body_lines(columns, keys, checks, named=True)
    lines += [_foreign_key_clause(name, parts) for name, parts in _grouped(foreign_keys).items()]
    statements = ["CREATE TABLE {} (\n    {}\n);".format(f"{q(schema)}.{q(table)}", ",\n    ".join(lines))]
    for name, parts in _grouped(indexes).items():
        if not (parts[0][7] or parts[0][8]):  # constraint indexes are already in the body
            statements.append(_index_statement(schema, table, name, parts))
    return "\n".join(statements)


@dataclass(frozen=True, slots=True)
class AddedConstraint:
    """A constraint a dump adds with ALTER TABLE after the data: a foreign key; a CHECK the server does not enforce or
    does not trust (created with its table it would check the rows the dump writes, which need not pass: 547); a
    CHECK or a DEFAULT whose function reads a table, view or synonym (review S1: those exist, with their rows, only
    then). Its name is always kept, so that NOCHECK and a `drop_first` DROP can name it (pre-flight H7, L13)."""

    name: str
    statements: tuple[str, ...]  # ADD …, then NOCHECK CONSTRAINT for one the server does not enforce
    references: tuple[str | None, str | None] | None = None  # a foreign key's table: (schema, name)


@dataclass(frozen=True, slots=True)
class TableStatements:
    """A table's DDL as a dump writes it (spec ⑥ §3.3.3): CREATE TABLE without its foreign keys, the CHECKs it does not
    enforce and the defaults and checks added later, then — after the data — its other indexes and the constraints it
    adds. No `;` at the ends."""

    create: str | None  # None: a definition it needs is hidden from the account (VIEW DEFINITION)
    indexes: list[str]
    foreign_keys: list[AddedConstraint]
    checks: list[AddedConstraint]  # not enforced or not trusted, or added after the views (`later`)
    defaults: list[AddedConstraint]  # added after the views (`later`)
    hidden: int  # indexes, foreign keys, checks and defaults not written: their definition is hidden from the account


@dataclass(frozen=True, slots=True)
class TableRows:
    """A table's catalog rows, the five queries its detail reads; its statements come from them."""

    schema: str
    name: str
    columns: list[_Column]
    keys: list[tuple]
    checks: list[tuple]
    foreign_keys: list[tuple]
    indexes: list[tuple]

    def user_types(self) -> set[tuple[str, str]]:
        """(schema, name) of the user types its columns use."""
        return {(c.user_schema, c.type_name) for c in self.columns if c.user_schema}

    def statements(self, later: frozenset[str] = frozenset()) -> TableStatements:
        """`later`: the names of its defaults and trusted checks a dump adds after the views."""
        schema, name = self.schema, self.name
        target = f"{q(schema)}.{q(name)}"
        hidden = 0

        def written(build: Callable[[], str]) -> str | None:
            nonlocal hidden
            text = _written(NodeKind.TABLE, schema, name, build)
            hidden += text is None
            return text

        def added(constraint: str, clause: str, trusted: bool, enabled: bool) -> tuple[str, ...]:
            statements = (f"ALTER TABLE {target} WITH {'CHECK' if trusted else 'NOCHECK'} ADD {clause}",)
            return statements + (() if enabled else (f"ALTER TABLE {target} NOCHECK CONSTRAINT {q(constraint)}",))

        enforced = [check for check in self.checks if not (check[3] or check[4])]  # neither disabled nor untrusted
        create = _written(NodeKind.TABLE, schema, name, lambda: "CREATE TABLE {} (\n    {}\n)".format(
            target, ",\n    ".join(_body_lines(self.columns, self.keys, enforced, named=True, later=later))))
        index_statements = []
        for index, parts in _grouped(self.indexes).items():
            if parts[0][7] or parts[0][8]:  # constraint indexes are in the body
                continue
            if (statement := written(lambda p=parts, i=index: _index_statement(schema, name, i, p))) is not None:
                index_statements.append(statement.rstrip(";"))
        keys_out = []
        for key, parts in _grouped(self.foreign_keys).items():
            clause = written(lambda p=parts, k=key: _foreign_key_clause(k, p, keep_name=True))
            if clause is not None:
                keys_out.append(AddedConstraint(key, added(key, clause, not parts[0][9], not parts[0][8]),
                                                (parts[0][2], parts[0][3])))
        checks_out = []
        for check, definition, _, disabled, untrusted, not_for_replication in self.checks:
            if not (disabled or untrusted or check in later):
                continue
            clause = written(lambda c=check, d=definition, r=not_for_replication: f"CONSTRAINT {q(c)} {_check(d, r)}")
            if clause is not None:
                checks_out.append(AddedConstraint(check, added(check, clause, not untrusted, not disabled)))
        defaults_out = []
        for column in self.columns:
            if column.has_default and column.default_name in later:
                clause = written(lambda c=column: f"CONSTRAINT {q(c.default_name)} DEFAULT "
                                                  f"{_need(c.default, 'a default definition')} FOR {q(c.name)}")
                if clause is not None:
                    defaults_out.append(AddedConstraint(column.default_name, (f"ALTER TABLE {target} ADD {clause}",)))
        return TableStatements(create, index_statements, keys_out, checks_out, defaults_out, hidden)


def table_rows_of(conn: driver.Connection, tables: dict[int, tuple[str, str]],
                  within: str | None = None) -> dict[int, TableRows]:
    """Many tables' rows (`tables`: object id → (schema, name)), one batch per ID_CHUNK of them or one over `within`
    (catalog.id_sources) — the five queries the table's detail reads (Q1). A table that is gone has no entry."""
    found: dict[int, TableRows] = {}
    for source in id_sources(tables, within):
        column_rows, keys, checks, foreign_keys, indexes = (by_object(part) for part in result_sets(conn, batch(
            _COLUMNS.many(source), _KEYS.many(source), _CHECKS.many(source), _FOREIGN_KEYS.many(source),
            _INDEXES.many(source))))
        for object_id, columns in column_rows.items():
            if object_id in tables:
                found[object_id] = TableRows(
                    *tables[object_id], [_Column(*row) for row in columns], keys.get(object_id, []),
                    checks.get(object_id, []), foreign_keys.get(object_id, []), indexes.get(object_id, []))
    return found


def view_indexes_of(conn: driver.Connection, views: dict[int, tuple[str, str]],
                    within: str | None = None) -> dict[int, list[str]]:
    """The indexes of indexed views (object id → CREATE … INDEX, the unique clustered one first: the others need it;
    no `;`), one query per ID_CHUNK of them or one over `within` (review S3)."""
    found: dict[int, list[str]] = {}
    for source in id_sources(views, within):
        for object_id, rows_ in by_object(rows(conn, _INDEXES.many(source))).items():
            if object_id not in views:
                continue
            schema, name = views[object_id]
            groups = sorted(_grouped(rows_).items(), key=lambda item: item[1][0][2] != "CLUSTERED")
            texts = [_written(NodeKind.VIEW, schema, name, lambda i=index, p=parts: _index_statement(schema, name, i, p))
                     for index, parts in groups]
            found[object_id] = [text.rstrip(";") for text in texts if text is not None]
    return found


def _sequence_statement(schema: str, name: str, row: tuple, start: str | None = None) -> str:
    """One _SEQUENCE_FIELDS row as CREATE SEQUENCE (no `;`), starting at `start` when given."""
    type_name, first, increment, minimum, maximum, is_cycling, is_cached, cache_size, precision, scale = row
    cache = f"CACHE {cache_size}" if is_cached and cache_size is not None else "CACHE" if is_cached else "NO CACHE"
    return (f"CREATE SEQUENCE {q(schema)}.{q(name)} AS {type_sql(type_name, 0, precision, scale)} START WITH "
            f"{start if start is not None else first} INCREMENT BY {increment} MINVALUE {minimum} MAXVALUE {maximum} "
            f"{'CYCLE' if is_cycling else 'NO CYCLE'} {cache}")


def sequence_ddl(conn: driver.Connection, schema: str, name: str) -> str:
    row = first_row(conn, _SEQUENCE.format(o=object_id_sql(schema, name)))
    if row is None:
        raise _not_found(NodeKind.SEQUENCE, schema, name)
    return _sequence_statement(schema, name, row) + ";"


@dataclass(frozen=True, slots=True)
class SequenceAtNext:
    statement: str  # CREATE SEQUENCE … START WITH the value it gives next (no `;`)
    exhausted: bool  # NO CYCLE, and its last value given: the one written gives that value once more


def _sequences_of(ids: str) -> str:
    """_SEQUENCE_FIELDS, the current value and whether one was ever given, of many sequences. last_used_value is
    SQL Server 2017's: read through dynamic SQL, 2016 answers "given", which at worst skips the start value."""
    return ("DECLARE @q nvarchar(max) = N'SELECT object_id, " + _SEQUENCE_FIELDS
            + ", CAST(current_value AS nvarchar(40)), ' + CASE WHEN COL_LENGTH(N'sys.sequences', N'last_used_value') "
            "IS NULL THEN N'1' ELSE N'CASE WHEN last_used_value IS NULL THEN 0 ELSE 1 END' END "
            f"+ N' FROM sys.sequences WHERE object_id IN ({ids})';\nEXEC (@q)")


def _next_value(first: str, increment: str, minimum: str, maximum: str, cycling: bool, current: str,
                used: bool) -> tuple[str, bool]:
    """(the value a sequence gives next, whether it has none left): its start value until one was given (appendix A
    S11), then the current value plus the increment — past MAXVALUE (MINVALUE going down) a CYCLE sequence starts
    over at MINVALUE (MAXVALUE), and a NO CYCLE one has no value left."""
    if not used:
        return first, False
    step = Decimal(increment)
    following = EXACT.add(Decimal(current), step)
    if Decimal(minimum) <= following <= Decimal(maximum):
        return str(following), False
    if cycling:
        return (minimum if step > 0 else maximum), False
    return current, True


def sequences_at_next_value(conn: driver.Connection, sequences: dict[int, tuple[str, str]],
                            within: str | None = None) -> dict[int, SequenceAtNext]:
    """A dump's sequences (object id → (schema, name)), each starting at the value it gives next: one query per
    ID_CHUNK of them, or one over `within` (catalog.id_sources; no quote in it: it goes into dynamic SQL)."""
    found: dict[int, SequenceAtNext] = {}
    for source in id_sources(sequences, within):
        for object_id, *fields, current, used in rows(conn, _sequences_of(source)):
            if object_id not in sequences:
                continue
            schema, name = sequences[object_id]
            start, exhausted = _next_value(fields[1], fields[2], fields[3], fields[4], bool(fields[5]), current,
                                           bool(used))
            found[object_id] = SequenceAtNext(_sequence_statement(schema, name, tuple(fields), start), exhausted)
    return found


def _synonym_statement(schema: str, name: str, base: str) -> str:
    return f"CREATE SYNONYM {q(schema)}.{q(name)} FOR {base}"


def synonym_ddl(conn: driver.Connection, schema: str, name: str) -> str:
    row = first_row(conn, _SYNONYM.format(o=object_id_sql(schema, name)))
    if row is None:
        raise _not_found(NodeKind.SYNONYM, schema, name)
    return _synonym_statement(schema, name, row[0]) + ";"


def synonyms_of(conn: driver.Connection, synonyms: dict[int, tuple[str, str]],
                within: str | None = None) -> dict[int, str]:
    """CREATE SYNONYM (no `;`) of many synonyms (object id → (schema, name)): one query per ID_CHUNK of them, or one
    over `within` (catalog.id_sources)."""
    return {object_id: _synonym_statement(*synonyms[object_id], base)
            for source in id_sources(synonyms, within) for object_id, base in rows(conn, _SYNONYMS_OF.format(ids=source))
            if object_id in synonyms}


def _type_statement(schema: str, name: str, type_row: tuple, column_rows: list[tuple], key_rows: list[tuple],
                    check_rows: list[tuple], index_rows: list[tuple]) -> str | None:
    """`type_row`: (is_table_type, base, max_length, precision, scale, nullable). A table type's own indexes go in its
    body (review S3). None for a CLR type: its assembly is not something a catalog query can write back."""
    is_table_type, base, max_length, precision, scale, nullable = type_row
    target = f"{q(schema)}.{q(name)}"
    if is_table_type:
        lines = _body_lines([_Column(*row) for row in column_rows], key_rows, check_rows, named=False)
        lines += [_inline_index(index, parts) for index, parts in _grouped(index_rows).items()
                  if not (parts[0][7] or parts[0][8])]
        return "CREATE TYPE {} AS TABLE (\n    {}\n);".format(target, ",\n    ".join(lines))
    if base is None:
        logger.debug("SQL Server type {}.{} has no base type (a CLR type): no DDL", schema, name)
        return None
    return f"CREATE TYPE {target} FROM {type_sql(base, max_length, precision, scale)} {'NULL' if nullable else 'NOT NULL'};"


def type_ddl(conn: driver.Connection, schema: str, name: str) -> str | None:
    """One batch: the type, and what a table type's body needs (empty for an alias type). None for a CLR type."""
    ref = name_literal(schema, name)
    body = _TABLE_TYPE_OBJECT.format(ref=ref)
    type_rows, column_rows, key_rows, check_rows, index_rows = result_sets(conn, batch(
        _TYPE.format(ref=ref), _COLUMNS.one(body), _KEYS.one(body), _CHECKS.one(body), _INDEXES.one(body)))
    if not type_rows:
        raise _not_found(NodeKind.TYPE, schema, name)
    return _type_statement(schema, name, type_rows[0][1:], column_rows, key_rows, check_rows, index_rows)


def types_of(conn: driver.Connection, types: dict[int, tuple[str, str]],
             within: str | None = None) -> dict[int, str | None]:
    """The DDL of many types (user_type_id → (schema, name); with `;`, as type_ddl), one batch per ID_CHUNK of them or
    one over `within` (catalog.id_sources); None for a CLR type. A table type's body is read with the queries a
    table's are, by its type_table_object_id."""
    found: dict[int, str | None] = {}
    for source in id_sources(types, within):
        bodies = _TABLE_TYPE_OBJECTS.format(ids=source)
        type_rows, table_objects, column_rows, keys, checks, indexes = result_sets(conn, batch(
            _TYPES_OF.format(ids=source), _TABLE_TYPE_OF.format(ids=source), _COLUMNS.many(bodies),
            _KEYS.many(bodies), _CHECKS.many(bodies), _INDEXES.many(bodies)))
        body_of = {int(type_id): int(object_id) for object_id, type_id in table_objects}
        column_rows, keys, checks, indexes = (by_object(part) for part in (column_rows, keys, checks, indexes))
        for type_id, _, *type_row in type_rows:
            if int(type_id) not in types:
                continue
            body = body_of.get(int(type_id))
            schema, name = types[int(type_id)]
            found[int(type_id)] = _written(NodeKind.TYPE, schema, name, lambda b=body, r=type_row, s=schema, n=name:
                                           _type_statement(s, n, tuple(r), column_rows.get(b, []), keys.get(b, []),
                                                           checks.get(b, []), indexes.get(b, [])))
    return found


# ---- details -------------------------------------------------------------------------------------------------------


def _not_found(kind: NodeKind | str, schema: str, name: str) -> NotFound:
    logger.debug("SQL Server {} {}.{} not found: the catalog has no such object", kind, schema, name)
    return NotFound(f"{kind} '{name}' not found")


def _definition_row(kind: NodeKind | str, schema: str, name: str, row: tuple) -> str | None:
    """OBJECT_DEFINITION: the server's own text. NULL (an encrypted module, or an account without VIEW DEFINITION) is
    no DDL, not an error: the object is there and keeps its columns."""
    object_id, definition = row
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
    """One batch: columns, keys, checks, foreign keys, indexes and the table's comment."""
    name = path[-1].name
    o = object_id_sql(schema, name)
    column_rows, keys, checks, foreign_keys, indexes, comment = result_sets(conn, batch(
        _COLUMNS.one(o), _KEYS.one(o), _CHECKS.one(o), _FOREIGN_KEYS.one(o), _INDEXES.one(o),
        _TABLE_COMMENT.format(o=o)))
    if not column_rows:  # a table always has a column
        raise _not_found(path[-1].kind, schema, name)
    columns = [_Column(*row) for row in column_rows]
    ddl = _written(path[-1].kind, schema, name, lambda: _table_ddl(schema, name, columns, keys, checks, foreign_keys, indexes))
    return _Found(ddl, ddl is not None, [c.info() for c in columns], (comment[0][0] or None) if comment else None)


def _view_detail(conn: driver.Connection, path: list[PathSegment], schema: str) -> _Found:
    """One batch: the definition and the columns."""
    name = path[-1].name
    definition_rows, column_rows = result_sets(conn, batch(
        _DEFINITION.format(ref=name_literal(schema, name)), _COLUMNS.one(object_id_sql(schema, name))))
    ddl = _definition_row(path[-1].kind, schema, name, definition_rows[0])
    return _Found(ddl, False, [_Column(*row).info() for row in column_rows])


def _module_detail(conn: driver.Connection, path: list[PathSegment], schema: str) -> _Found:
    name = path[-1].name
    return _Found(_definition_row(path[-1].kind, schema, name, first_row(conn, _DEFINITION.format(ref=name_literal(schema, name)))))


def _generated_detail(build: Callable[[driver.Connection, str, str], str | None]) -> _Detail:
    """A detail whose DDL a catalog query writes: build(conn, schema, name), None when there is nothing to write."""

    def detail(conn: driver.Connection, path: list[PathSegment], schema: str) -> _Found:
        name = path[-1].name
        ddl = _written(path[-1].kind, schema, name, lambda: build(conn, schema, name))
        return _Found(ddl, ddl is not None)
    return detail


def _table_leaf(path: list[PathSegment]) -> tuple[str, str]:
    """(schema, table) of an index, foreign key or column: the table sits at path[3]."""
    if len(path) < 5:
        raise ValidationFailed(f"A {path[-1].kind} belongs to a table: expected a path inside one")
    return path[1].name, path[3].name


def _index_detail(conn: driver.Connection, path: list[PathSegment], _schema: str) -> _Found:
    schema, table = _table_leaf(path)
    name = path[-1].name
    parts = _grouped(rows(conn, _INDEXES.one(object_id_sql(schema, table)))).get(name)
    if parts is None:
        raise _not_found(path[-1].kind, schema, name)
    ddl = _written(path[-1].kind, schema, name, lambda: _index_statement(schema, table, name, parts))
    return _Found(ddl, ddl is not None)


def _foreign_key_detail(conn: driver.Connection, path: list[PathSegment], _schema: str) -> _Found:
    schema, table = _table_leaf(path)
    name = path[-1].name
    parts = _grouped(rows(conn, _FOREIGN_KEYS.one(object_id_sql(schema, table)))).get(name)
    if parts is None:
        raise _not_found(path[-1].kind, schema, name)
    ddl = _written(path[-1].kind, schema, name,
                   lambda: f"ALTER TABLE {q(schema)}.{q(table)} ADD {_foreign_key_clause(name, parts)};")
    return _Found(ddl, ddl is not None)


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
    NodeKind.SEQUENCE: _generated_detail(sequence_ddl),
    NodeKind.SYNONYM: _generated_detail(synonym_ddl),
    NodeKind.TYPE: _generated_detail(type_ddl),
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
