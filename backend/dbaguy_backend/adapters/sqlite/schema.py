import sqlite3
from datetime import UTC, datetime

from loguru import logger

from ...domain.schema import SchemaSnapshot, SnapshotColumn, SnapshotForeignKey, SnapshotRelation, SnapshotSchema
from ..sql_text import quote_ident_sqlite as q

_MAIN = "main"


def _relations(conn: sqlite3.Connection, master: str) -> dict[str, SnapshotRelation]:
    rows = conn.execute(
        f"SELECT name, type FROM {master} WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
        f"ORDER BY name"
    )
    return {name: SnapshotRelation(name=name, kind="table" if type_ == "table" else "view", columns=[])
            for name, type_ in rows}


def _pragma_rows(
    conn: sqlite3.Connection, what: str, batched_sql: str, one_sql: str, schema: str, names: list[str]
) -> list[tuple]:
    """Rows of a `pragma_*` table-valued function for many relations, as (relation name, ...) tuples.

    One batched query when it works. A single broken relation (e.g. a virtual table whose module is not loaded) makes
    SQLite fail the whole batch, so then each relation is read on its own and the broken ones are skipped.
    """
    try:
        return conn.execute(batched_sql, (schema,)).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("Reading {} of SQLite schema {} in one query failed ({}); reading relation by relation",
                       what, schema, exc)
    rows: list[tuple] = []
    for name in names:
        try:
            rows.extend(conn.execute(one_sql, (name, name, schema)).fetchall())
        except sqlite3.OperationalError as exc:
            logger.warning("Skipping {} of SQLite relation {}.{}: {}", what, schema, name, exc)
    return rows


def _fill_columns(conn: sqlite3.Connection, master: str, schema: str, relations: dict[str, SnapshotRelation]) -> None:
    rows = _pragma_rows(
        conn, "columns",
        f"SELECT m.name, p.name, p.type, p.pk FROM {master} m JOIN pragma_table_xinfo(m.name, ?) p "
        f"WHERE m.type IN ('table', 'view') ORDER BY m.name, p.cid",
        "SELECT ?, p.name, p.type, p.pk FROM pragma_table_xinfo(?, ?) p ORDER BY p.cid",
        schema, list(relations),
    )
    primary_keys: dict[str, list[tuple[int, str]]] = {}
    for rel_name, column, type_name, pk in rows:
        if rel_name in relations:
            relations[rel_name].columns.append(SnapshotColumn(name=column, type_name=type_name or ""))
            if pk:
                primary_keys.setdefault(rel_name, []).append((pk, column))
    for rel_name, columns in primary_keys.items():
        relations[rel_name].primary_key = [column for _, column in sorted(columns)]


def _fill_foreign_keys(
    conn: sqlite3.Connection, master: str, schema: str, relations: dict[str, SnapshotRelation]
) -> None:
    rows = _pragma_rows(
        conn, "foreign keys",
        f'SELECT m.name, f.id, f."table", f."from", f."to" FROM {master} m '
        f"JOIN pragma_foreign_key_list(m.name, ?) f WHERE m.type = 'table' ORDER BY m.name, f.id, f.seq",
        'SELECT ?, f.id, f."table", f."from", f."to" FROM pragma_foreign_key_list(?, ?) f ORDER BY f.id, f.seq',
        schema, [name for name, relation in relations.items() if relation.kind == "table"],
    )
    by_lower_name = {name.lower(): relation for name, relation in relations.items()}  # names are case-insensitive
    foreign_keys: dict[tuple[str, int], SnapshotForeignKey] = {}
    implicit: set[tuple[str, int]] = set()  # reference the parent's primary key without naming its columns
    for rel_name, fk_id, ref_table, column, ref_column in rows:
        if rel_name not in relations:
            continue
        key = (rel_name, fk_id)
        if key not in foreign_keys:
            parent = by_lower_name.get(ref_table.lower())
            foreign_keys[key] = SnapshotForeignKey(
                name=f"fk_{rel_name}_{fk_id}", columns=[], ref_schema=schema,
                ref_table=parent.name if parent else ref_table, ref_columns=[],
            )
            relations[rel_name].foreign_keys.append(foreign_keys[key])
        foreign_keys[key].columns.append(column)
        if ref_column is None:
            implicit.add(key)
        else:
            foreign_keys[key].ref_columns.append(ref_column)
    for key in implicit:
        fk = foreign_keys[key]
        parent = by_lower_name.get(fk.ref_table.lower())
        primary_key = parent.primary_key if parent else []
        fk.ref_columns = list(primary_key) if len(primary_key) == len(fk.columns) else [""] * len(fk.columns)


def build_snapshot(conn: sqlite3.Connection) -> SchemaSnapshot:
    """Schema snapshot of every attached database: three queries per schema, whatever the number of tables.

    (Plus one query per relation, only for a schema where a broken relation made the batched queries fail.)
    """
    schemas: list[SnapshotSchema] = []
    for _, schema, _ in conn.execute("PRAGMA database_list").fetchall():
        if schema == "temp":
            continue
        master = f"{q(schema)}.sqlite_master"
        relations = _relations(conn, master)
        _fill_columns(conn, master, schema, relations)
        _fill_foreign_keys(conn, master, schema, relations)
        schemas.append(SnapshotSchema(name=schema, relations=list(relations.values())))
    logger.debug(
        "SQLite schema snapshot: {} schemas, {} relations", len(schemas), sum(len(s.relations) for s in schemas)
    )
    return SchemaSnapshot(
        database=None, default_schema=_MAIN, search_path=[_MAIN], schemas=schemas, built_at=datetime.now(UTC)
    )
