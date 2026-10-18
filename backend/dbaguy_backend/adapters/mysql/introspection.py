"""The MySQL object tree and object details. MySQL databases are `schema` nodes, so paths start at a schema.

Tree: schema > folder > table|view > folder (columns, indexes, foreign_keys, triggers) > leaf.
Every name interpolated into SQL goes through `quote_ident_mysql`; every value goes through `%s` parameters.
"""

import mysql.connector
from loguru import logger
from mysql.connector.connection import MySQLConnection

from ...domain.errors import NotFound, ValidationFailed
from ...domain.metadata import ColumnInfo, Folder, NodeKind, ObjectDetail, ObjectNode, PathSegment, make_node
from ..sql_text import quote_ident_mysql as q
from . import catalog

_FOLDER_COUNTS = """
SELECT 'tables', COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_TYPE <> 'VIEW'
UNION ALL SELECT 'views', COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'VIEW'
UNION ALL SELECT 'functions', COUNT(*) FROM information_schema.ROUTINES
    WHERE ROUTINE_SCHEMA = %s AND ROUTINE_TYPE = 'FUNCTION'
UNION ALL SELECT 'procedures', COUNT(*) FROM information_schema.ROUTINES
    WHERE ROUTINE_SCHEMA = %s AND ROUTINE_TYPE = 'PROCEDURE'
UNION ALL SELECT 'triggers', COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s
UNION ALL SELECT 'events', COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA = %s
UNION ALL SELECT 'exists', COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s
"""
_TABLE_FOLDER_COUNTS = """
SELECT 'indexes', COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
UNION ALL SELECT 'foreign_keys', COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = %s AND TABLE_NAME = %s
UNION ALL SELECT 'triggers', COUNT(*) FROM information_schema.TRIGGERS
    WHERE EVENT_OBJECT_SCHEMA = %s AND EVENT_OBJECT_TABLE = %s
UNION ALL SELECT 'exists', COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
"""
_OBJECTS: dict[Folder, str] = {  # (name, detail); the order is the order of the schema's folders
    Folder.TABLES: "SELECT TABLE_NAME, NULL FROM information_schema.TABLES "
                   "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE <> 'VIEW' ORDER BY TABLE_NAME",
    Folder.VIEWS: "SELECT TABLE_NAME, NULL FROM information_schema.TABLES "
                  "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'VIEW' ORDER BY TABLE_NAME",
    Folder.FUNCTIONS: "SELECT ROUTINE_NAME, NULL FROM information_schema.ROUTINES "
                      "WHERE ROUTINE_SCHEMA = %s AND ROUTINE_TYPE = 'FUNCTION' ORDER BY ROUTINE_NAME",
    Folder.PROCEDURES: "SELECT ROUTINE_NAME, NULL FROM information_schema.ROUTINES "
                       "WHERE ROUTINE_SCHEMA = %s AND ROUTINE_TYPE = 'PROCEDURE' ORDER BY ROUTINE_NAME",
    Folder.TRIGGERS: "SELECT TRIGGER_NAME, CONCAT('on ', EVENT_OBJECT_TABLE) FROM information_schema.TRIGGERS "
                     "WHERE TRIGGER_SCHEMA = %s ORDER BY TRIGGER_NAME",
    Folder.EVENTS: "SELECT EVENT_NAME, LOWER(STATUS) FROM information_schema.EVENTS "
                   "WHERE EVENT_SCHEMA = %s ORDER BY EVENT_NAME",
}
_COLUMNS = """
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY, COLUMN_COMMENT
FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION
"""
_INDEXES = """
SELECT INDEX_NAME, MIN(NON_UNIQUE) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
GROUP BY INDEX_NAME ORDER BY INDEX_NAME = 'PRIMARY' DESC, INDEX_NAME
"""
_FOREIGN_KEYS = """
SELECT CONSTRAINT_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY ORDINAL_POSITION SEPARATOR ', '),
       REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME,
       GROUP_CONCAT(REFERENCED_COLUMN_NAME ORDER BY ORDINAL_POSITION SEPARATOR ', ')
FROM information_schema.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND REFERENCED_TABLE_NAME IS NOT NULL
GROUP BY CONSTRAINT_NAME, REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME ORDER BY CONSTRAINT_NAME
"""
_TABLE_TRIGGERS = """
SELECT TRIGGER_NAME, LOWER(CONCAT(ACTION_TIMING, ' ', EVENT_MANIPULATION)) FROM information_schema.TRIGGERS
WHERE EVENT_OBJECT_SCHEMA = %s AND EVENT_OBJECT_TABLE = %s ORDER BY TRIGGER_NAME
"""
_INDEX_PARTS = """
SELECT NON_UNIQUE, INDEX_TYPE, COLUMN_NAME, SUB_PART, COLLATION, EXPRESSION FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = %s ORDER BY SEQ_IN_INDEX
"""
_FOREIGN_KEY_PARTS = """
SELECT k.COLUMN_NAME, k.REFERENCED_TABLE_SCHEMA, k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME,
       r.UPDATE_RULE, r.DELETE_RULE
FROM information_schema.KEY_COLUMN_USAGE k
JOIN information_schema.REFERENTIAL_CONSTRAINTS r
  ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME AND r.TABLE_NAME = k.TABLE_NAME
WHERE k.TABLE_SCHEMA = %s AND k.TABLE_NAME = %s AND k.CONSTRAINT_NAME = %s ORDER BY k.ORDINAL_POSITION
"""
_TABLE_COMMENT = "SELECT TABLE_COMMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
_VIEW_DEFINITION = "SELECT VIEW_DEFINITION FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
# Run only when a query came back empty, to tell an empty answer from a schema or table that is gone.
_PRESENCE = (
    "SELECT (SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s), "
    "(SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s)"
)

_KIND_OF_FOLDER: dict[Folder, NodeKind] = {
    Folder.TABLES: NodeKind.TABLE,
    Folder.VIEWS: NodeKind.VIEW,
    Folder.FUNCTIONS: NodeKind.FUNCTION,
    Folder.PROCEDURES: NodeKind.PROCEDURE,
    Folder.TRIGGERS: NodeKind.TRIGGER,
    Folder.EVENTS: NodeKind.EVENT,
}
_HAS_FOLDERS = (NodeKind.TABLE, NodeKind.VIEW)
# kind -> (word after SHOW CREATE, index of the DDL in the result row)
_SHOW_CREATE: dict[NodeKind, tuple[str, int]] = {
    NodeKind.TABLE: ("TABLE", 1),
    NodeKind.VIEW: ("VIEW", 1),
    NodeKind.PROCEDURE: ("PROCEDURE", 2),
    NodeKind.FUNCTION: ("FUNCTION", 2),
    NodeKind.TRIGGER: ("TRIGGER", 2),
    NodeKind.EVENT: ("EVENT", 3),
}
_GENERATED = (NodeKind.INDEX, NodeKind.FOREIGN_KEY, NodeKind.COLUMN)  # leaves of a table: the path holds the table


# Whether the schema (and table) a call names still exists is not a query of its own: it rides on the count query of
# the level (an `exists` row) or is only asked once an answer came back empty or failed, so the common path of every
# call is one round trip, however far the connection is.


def _require_schema(conn: MySQLConnection, name: str) -> None:
    if catalog.first_row(conn, "SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (name,)) is None:
        raise NotFound(f"Schema '{name}' not found")


def _require_table(conn: MySQLConnection, schema: str, table: str, kind: str) -> None:
    """After an empty answer about a table's children: NotFound when the schema or the table is gone."""
    schema_count, table_count = catalog.first_row(conn, _PRESENCE, (schema, schema, table))
    if not schema_count:
        raise NotFound(f"Schema '{schema}' not found")
    if not table_count:
        raise NotFound(f"{kind} '{table}' not found")


def _schema_name(path: list[PathSegment]) -> str:
    """The schema a path starts at (it is not looked up here: the level's own query does that)."""
    if path[0].kind != NodeKind.SCHEMA:
        raise ValidationFailed(f"A path starts at a schema, not at a {path[0].kind}")
    return path[0].name


def _folder(name: str) -> Folder:
    try:
        return Folder(name)
    except ValueError:
        raise ValidationFailed(f"Unknown folder '{name}'") from None


def columns(conn: MySQLConnection, schema: str, table: str) -> list[ColumnInfo]:
    return [
        ColumnInfo(
            name=name, type_name=type_name, nullable=nullable == "YES", default=default,
            is_primary_key=key == "PRI", comment=comment or None,
        )
        for name, type_name, nullable, default, key, comment in catalog.query(conn, _COLUMNS, (schema, table))
    ]


def _column_detail(col: ColumnInfo) -> str:
    parts = [col.type_name]
    if not col.nullable:
        parts.append("NOT NULL")
    if col.is_primary_key:
        parts.append("PK")
    return " ".join(parts)


def _index_detail(name: str, non_unique: int) -> str | None:
    if name == "PRIMARY":
        return "primary"
    return None if non_unique else "unique"


# ---- tree ----------------------------------------------------------------------------------------------------------


def list_children(conn: MySQLConnection, path: list[PathSegment]) -> list[ObjectNode]:
    if not path:
        return [
            make_node(path, NodeKind.SCHEMA, name, has_children=True, system=is_system)
            for name, is_system in catalog.list_schemas(conn)
        ]
    schema = _schema_name(path)
    last = path[-1]
    folder = _folder(last.name) if last.kind == NodeKind.FOLDER else None  # unknown folder names fail at any depth
    depth = len(path)
    if depth == 1:
        return _schema_folders(conn, path, schema)
    if depth == 2 and folder is not None:
        return _schema_objects(conn, path, schema, folder)
    if depth == 3 and last.kind in _HAS_FOLDERS:
        return _table_folders(conn, path, schema, last)
    if depth == 4 and folder is not None:
        return _table_children(conn, path, schema, folder)
    return []


def _counts(conn: MySQLConnection, sql: str, *names: str) -> dict[str, int]:
    """A UNION ALL of ('label', COUNT(*)) rows; every `%s` of the statement takes the same `names`."""
    return dict(catalog.query(conn, sql, list(names) * (sql.count("%s") // len(names))))


def _schema_folders(conn: MySQLConnection, path: list[PathSegment], schema: str) -> list[ObjectNode]:
    counts = _counts(conn, _FOLDER_COUNTS, schema)
    if not counts["exists"]:
        raise NotFound(f"Schema '{schema}' not found")
    return [make_node(path, NodeKind.FOLDER, folder, has_children=True) for folder in _OBJECTS if counts[folder]]


def _schema_objects(conn: MySQLConnection, path: list[PathSegment], schema: str, folder: Folder) -> list[ObjectNode]:
    if folder not in _OBJECTS:
        raise ValidationFailed(f"Folder '{folder}' is not listed at schema level")
    rows = catalog.query(conn, _OBJECTS[folder], (schema,))
    if not rows:
        _require_schema(conn, schema)  # a folder is listed only when it has objects: empty means stale or gone
    kind = _KIND_OF_FOLDER[folder]
    return [make_node(path, kind, name, has_children=kind in _HAS_FOLDERS, detail=detail) for name, detail in rows]


def _table_folders(conn: MySQLConnection, path: list[PathSegment], schema: str, table: PathSegment) -> list[ObjectNode]:
    counts = _counts(conn, _TABLE_FOLDER_COUNTS, schema, table.name)
    if not counts["exists"]:
        _require_table(conn, schema, table.name, table.kind)  # tells a dropped schema from a dropped table
        raise NotFound(f"{table.kind} '{table.name}' not found")
    folders = [Folder.COLUMNS]
    if table.kind == NodeKind.TABLE:  # a view has no indexes, foreign keys or triggers
        folders += [folder for folder in (Folder.INDEXES, Folder.FOREIGN_KEYS, Folder.TRIGGERS) if counts[folder]]
    return [make_node(path, NodeKind.FOLDER, folder, has_children=True) for folder in folders]


def _column_nodes(conn: MySQLConnection, path: list[PathSegment], schema: str, table: str) -> list[ObjectNode]:
    return [
        make_node(path, NodeKind.COLUMN, col.name, has_children=False, detail=_column_detail(col))
        for col in columns(conn, schema, table)
    ]


def _index_nodes(conn: MySQLConnection, path: list[PathSegment], schema: str, table: str) -> list[ObjectNode]:
    return [
        make_node(path, NodeKind.INDEX, name, has_children=False, detail=_index_detail(name, non_unique))
        for name, non_unique in catalog.query(conn, _INDEXES, (schema, table))
    ]


def _foreign_key_nodes(conn: MySQLConnection, path: list[PathSegment], schema: str, table: str) -> list[ObjectNode]:
    return [
        make_node(
            path, NodeKind.FOREIGN_KEY, name, has_children=False,
            detail=f"({cols}) → {ref_schema}.{ref_table} ({ref_cols})",
        )
        for name, cols, ref_schema, ref_table, ref_cols in catalog.query(conn, _FOREIGN_KEYS, (schema, table))
    ]


def _trigger_nodes(conn: MySQLConnection, path: list[PathSegment], schema: str, table: str) -> list[ObjectNode]:
    return [
        make_node(path, NodeKind.TRIGGER, name, has_children=False, detail=timing)
        for name, timing in catalog.query(conn, _TABLE_TRIGGERS, (schema, table))
    ]


_TABLE_LEAVES = {
    Folder.COLUMNS: _column_nodes,
    Folder.INDEXES: _index_nodes,
    Folder.FOREIGN_KEYS: _foreign_key_nodes,
    Folder.TRIGGERS: _trigger_nodes,
}


def _table_children(conn: MySQLConnection, path: list[PathSegment], schema: str, folder: Folder) -> list[ObjectNode]:
    if folder not in _TABLE_LEAVES:
        raise ValidationFailed(f"Folder '{folder}' is not listed under a table")
    table = path[2]
    nodes = _TABLE_LEAVES[folder](conn, path, schema, table.name)
    if not nodes:  # a table always has columns and a folder is listed only when it has leaves: stale, or gone
        _require_table(conn, schema, table.name, table.kind)
    return nodes


# ---- details -------------------------------------------------------------------------------------------------------


def _index_ddl(schema: str, table: str, name: str, parts: list[tuple]) -> str:
    """parts: (NON_UNIQUE, INDEX_TYPE, COLUMN_NAME, SUB_PART, COLLATION, EXPRESSION) in index order."""
    key_columns = ", ".join(
        (f"({expression})" if expression else q(column) + (f"({sub_part})" if sub_part else ""))
        + (" DESC" if collation == "D" else "")
        for _, _, column, sub_part, collation, expression in parts
    )
    target = f"{q(schema)}.{q(table)}"
    if name == "PRIMARY":
        return f"ALTER TABLE {target} ADD PRIMARY KEY ({key_columns});"
    non_unique, index_type = parts[0][0], parts[0][1]
    prefix = {"FULLTEXT": "FULLTEXT ", "SPATIAL": "SPATIAL "}.get(index_type, "" if non_unique else "UNIQUE ")
    return f"CREATE {prefix}INDEX {q(name)} ON {target} ({key_columns});"


def _foreign_key_ddl(schema: str, table: str, name: str, parts: list[tuple]) -> str:
    """parts: (COLUMN_NAME, REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME, UPDATE_RULE,
    DELETE_RULE) in key order."""
    _, ref_schema, ref_table, _, on_update, on_delete = parts[0]
    key_columns = ", ".join(q(p[0]) for p in parts)
    ref_columns = ", ".join(q(p[3]) for p in parts)
    return (
        f"ALTER TABLE {q(schema)}.{q(table)} ADD CONSTRAINT {q(name)} FOREIGN KEY ({key_columns}) "
        f"REFERENCES {q(ref_schema)}.{q(ref_table)} ({ref_columns}) ON UPDATE {on_update} ON DELETE {on_delete};"
    )


def _view_definition_ddl(conn: MySQLConnection, schema: str, name: str) -> str | None:
    """The best DDL of a view whose SHOW CREATE is refused: its definition, when information_schema shows it."""
    row = catalog.first_row(conn, _VIEW_DEFINITION, (schema, name))
    return f"CREATE VIEW {q(schema)}.{q(name)} AS {row[0]};" if row and row[0] else None


def _server_ddl(conn: MySQLConnection, schema: str, kind: NodeKind, name: str) -> tuple[str | None, bool]:
    """(ddl, is_generated): the server's own text; None when the account may not read it."""
    word, ddl_index = _SHOW_CREATE[kind]
    try:
        row = catalog.first_row(conn, f"SHOW CREATE {word} {q(schema)}.{q(name)}")
    except mysql.connector.Error as exc:
        if exc.errno not in catalog.UNREADABLE_DEFINITION_ERRNOS:
            raise
        logger.debug(
            "MySQL error {} on SHOW CREATE {} {}.{}: no server DDL ({})", exc.errno, word, schema, name, exc.msg
        )
        # A dropped schema answers 1142 as well, and so does a vanished table of a schema the account has no grant
        # on: those are NotFound, not an object without readable DDL.
        if kind in _HAS_FOLDERS:
            _require_table(conn, schema, name, kind)
        else:
            _require_schema(conn, schema)
        if kind == NodeKind.VIEW:
            return _view_definition_ddl(conn, schema, name), True
        return None, False
    if row is None:
        raise NotFound(f"{kind} '{name}' not found")
    if not row[ddl_index]:  # NULL: the account may not see the definition
        logger.debug("MySQL SHOW CREATE {} {}.{} returned no definition", word, schema, name)
        return None, False
    return f"{row[ddl_index]};", False


def _generated_ddl(conn: MySQLConnection, schema: str, table: str, kind: NodeKind, name: str) -> str:
    if kind == NodeKind.INDEX:
        parts = catalog.query(conn, _INDEX_PARTS, (schema, table, name))
        build = _index_ddl
    else:
        parts = catalog.query(conn, _FOREIGN_KEY_PARTS, (schema, table, name))
        build = _foreign_key_ddl
    if not parts:
        _require_table(conn, schema, table, NodeKind.TABLE)
        raise NotFound(f"{kind} '{name}' not found")
    return build(schema, table, name, parts)


def _detail(conn: MySQLConnection, path: list[PathSegment], schema: str) -> ObjectDetail:
    last = path[-1]
    kind = NodeKind(last.kind) if last.kind in NodeKind else None
    if kind in _SHOW_CREATE:
        ddl, is_generated = _server_ddl(conn, schema, kind, last.name)
        comment = None
        if kind == NodeKind.TABLE:
            row = catalog.first_row(conn, _TABLE_COMMENT, (schema, last.name))
            comment = (row[0] or None) if row else None
        return ObjectDetail(
            path=path, kind=last.kind, name=last.name, ddl=ddl, ddl_is_generated=is_generated and ddl is not None,
            columns=columns(conn, schema, last.name) if kind in _HAS_FOLDERS else None, comment=comment,
        )
    if kind in _GENERATED:
        if len(path) < 3:
            raise ValidationFailed(f"A {kind} belongs to a table: expected a path inside one")
        table = path[2].name
        if kind == NodeKind.COLUMN:
            match = [col for col in columns(conn, schema, table) if col.name == last.name]
            if not match:
                _require_table(conn, schema, table, NodeKind.TABLE)
                raise NotFound(f"Column '{last.name}' not found")
            return ObjectDetail(path=path, kind=last.kind, name=last.name, columns=match)
        ddl = _generated_ddl(conn, schema, table, kind, last.name)
        return ObjectDetail(path=path, kind=last.kind, name=last.name, ddl=ddl, ddl_is_generated=True)
    raise ValidationFailed(f"No details available for {last.kind}")


def object_detail(conn: MySQLConnection, path: list[PathSegment]) -> ObjectDetail:
    if not path:
        raise ValidationFailed("Empty path")
    schema = _schema_name(path)
    try:
        return _detail(conn, path, schema)
    except mysql.connector.Error as exc:
        missing = exc.errno in catalog.MISSING_OBJECT_ERRNOS
        kind, name = path[-1].kind, path[-1].name
        if missing:
            logger.debug("MySQL error {} on {} {}.{} mapped to NotFound ({})", exc.errno, kind, schema, name, exc.msg)
        if missing or exc.errno in catalog.ACCESS_DENIED_ERRNOS:
            _require_schema(conn, schema)  # the server words a dropped schema in several ways: say so plainly
        if missing:
            raise NotFound(f"{kind} '{name}' not found") from exc
        raise  # MetadataService retries once, then reports QueryFailed
