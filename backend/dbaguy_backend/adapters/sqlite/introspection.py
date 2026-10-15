import sqlite3

from ...domain.errors import NotFound, ValidationFailed
from ...domain.metadata import ColumnInfo, Folder, NodeKind, ObjectDetail, ObjectNode, PathSegment, make_node
from ..sql_text import quote_ident_sqlite as q

_TYPE_FOLDERS: dict[Folder, str] = {
    Folder.TABLES: "table",
    Folder.VIEWS: "view",
    Folder.INDEXES: "index",
    Folder.TRIGGERS: "trigger",
}
_KIND_OF_TYPE = {
    "table": NodeKind.TABLE,
    "view": NodeKind.VIEW,
    "index": NodeKind.INDEX,
    "trigger": NodeKind.TRIGGER,
}
_TYPE_OF_KIND = {kind: obj_type for obj_type, kind in _KIND_OF_TYPE.items()}


def _master(schema: str) -> str:
    return f"{q(schema)}.sqlite_master"


def _schemas(conn: sqlite3.Connection) -> list[str]:
    return [row[1] for row in conn.execute("PRAGMA database_list") if row[1] != "temp"]


def _require_schema(conn: sqlite3.Connection, name: str) -> None:
    if name not in _schemas(conn):
        raise NotFound(f"Schema '{name}' not found")


def _folder(name: str) -> Folder:
    try:
        return Folder(name)
    except ValueError:
        raise ValidationFailed(f"Unknown folder '{name}'") from None


def columns(conn: sqlite3.Connection, schema: str, table: str) -> list[ColumnInfo]:
    rows = conn.execute(f"PRAGMA {q(schema)}.table_xinfo({q(table)})").fetchall()
    return [
        ColumnInfo(name=name, type_name=type_ or "", nullable=not notnull, default=default, is_primary_key=pk > 0)
        for _cid, name, type_, notnull, default, pk, _hidden in rows
    ]


def _table_indexes(conn: sqlite3.Connection, schema: str, table: str) -> list[tuple[str, bool]]:
    rows = conn.execute(f"PRAGMA {q(schema)}.index_list({q(table)})").fetchall()
    return sorted((row[1], bool(row[2])) for row in rows)


def _table_triggers(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    rows = conn.execute(
        f"SELECT name FROM {_master(schema)} WHERE type = 'trigger' AND tbl_name = ? ORDER BY name", (table,)
    ).fetchall()
    return [row[0] for row in rows]


def _column_detail(col: ColumnInfo) -> str:
    parts = [col.type_name or "ANY"]
    if not col.nullable:
        parts.append("NOT NULL")
    if col.is_primary_key:
        parts.append("PK")
    return " ".join(parts)


def list_children(conn: sqlite3.Connection, path: list[PathSegment]) -> list[ObjectNode]:
    depth = len(path)
    if depth == 0:
        return [make_node(path, NodeKind.SCHEMA, s, has_children=True) for s in _schemas(conn)]
    schema = path[0].name
    last = path[-1]
    _require_schema(conn, schema)
    if last.kind == NodeKind.FOLDER:
        _folder(last.name)  # unknown folder names are rejected at any depth
    if depth == 1:
        counts = dict(conn.execute(f"SELECT type, count(*) FROM {_master(schema)} GROUP BY type").fetchall())
        return [
            make_node(path, NodeKind.FOLDER, folder, has_children=True)
            for folder, obj_type in _TYPE_FOLDERS.items()
            if counts.get(obj_type)
        ]
    if depth == 2 and last.kind == NodeKind.FOLDER:
        folder = Folder(last.name)  # validated above
        if folder not in _TYPE_FOLDERS:
            raise ValidationFailed(f"Folder '{folder}' is not listed at schema level")
        obj_type = _TYPE_FOLDERS[folder]
        kind = _KIND_OF_TYPE[obj_type]
        rows = conn.execute(
            f"SELECT name, tbl_name FROM {_master(schema)} WHERE type = ? ORDER BY name", (obj_type,)
        ).fetchall()
        return [
            make_node(
                path, kind, name,
                has_children=kind in (NodeKind.TABLE, NodeKind.VIEW),
                system=name.startswith("sqlite_"),
                detail=f"on {tbl}" if kind in (NodeKind.INDEX, NodeKind.TRIGGER) else None,
            )
            for name, tbl in rows
        ]
    if depth == 3 and last.kind in (NodeKind.TABLE, NodeKind.VIEW):
        folders = [Folder.COLUMNS]
        if last.kind == NodeKind.TABLE:
            if _table_indexes(conn, schema, last.name):
                folders.append(Folder.INDEXES)
            if _table_triggers(conn, schema, last.name):
                folders.append(Folder.TRIGGERS)
        return [make_node(path, NodeKind.FOLDER, f, has_children=True) for f in folders]
    if depth == 4 and last.kind == NodeKind.FOLDER:
        table = path[2].name
        if last.name == Folder.COLUMNS:
            return [
                make_node(path, NodeKind.COLUMN, c.name, has_children=False, detail=_column_detail(c))
                for c in columns(conn, schema, table)
            ]
        if last.name == Folder.INDEXES:
            return [
                make_node(
                    path, NodeKind.INDEX, name, has_children=False,
                    system=name.startswith("sqlite_autoindex"), detail="unique" if unique else None,
                )
                for name, unique in _table_indexes(conn, schema, table)
            ]
        if last.name == Folder.TRIGGERS:
            return [make_node(path, NodeKind.TRIGGER, n, has_children=False) for n in _table_triggers(conn, schema, table)]
    return []


def object_detail(conn: sqlite3.Connection, path: list[PathSegment]) -> ObjectDetail:
    if not path:
        raise ValidationFailed("Empty path")
    schema, last = path[0].name, path[-1]
    _require_schema(conn, schema)
    if last.kind in (NodeKind.TABLE, NodeKind.VIEW, NodeKind.INDEX, NodeKind.TRIGGER):
        row = conn.execute(
            f"SELECT sql FROM {_master(schema)} WHERE type = ? AND name = ?",
            (_TYPE_OF_KIND[last.kind], last.name),
        ).fetchone()
        if row is None:
            raise NotFound(f"{last.kind} '{last.name}' not found")
        ddl = f"{row[0]};" if row[0] else None
        cols = columns(conn, schema, last.name) if last.kind in (NodeKind.TABLE, NodeKind.VIEW) else None
        return ObjectDetail(path=path, kind=last.kind, name=last.name, ddl=ddl, columns=cols)
    if last.kind == NodeKind.COLUMN and len(path) >= 3:
        table = path[2].name
        match = [c for c in columns(conn, schema, table) if c.name == last.name]
        if not match:
            raise NotFound(f"Column '{last.name}' not found")
        return ObjectDetail(path=path, kind=last.kind, name=last.name, columns=match)
    raise ValidationFailed(f"No details available for {last.kind}")
