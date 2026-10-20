"""MySQL dump tests' independent check (06a Task 6 review, ruling T10), adopted from the reviewer's harness: a
database's objects as root reads them in information_schema (with every table's SHOW CREATE TABLE as root), and a
digest of every table's rows (every column, invisible ones too, as the server's raw bytes). A restore is compared
through it, never through the dump's own reader, which cannot see what it never reads (a MERGE table's rows written
twice, a function's collation)."""

import hashlib

import mysql.connector

from tests.support import mysql_driver_kwargs

INVENTORY = {
    "schema": "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
              "WHERE SCHEMA_NAME = %s",
    "tables": "SELECT TABLE_NAME, TABLE_TYPE, ENGINE, ROW_FORMAT, TABLE_COLLATION, CREATE_OPTIONS, TABLE_COMMENT, "
              "AUTO_INCREMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
    "columns": "SELECT TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, COLUMN_DEFAULT, IS_NULLABLE, COLUMN_TYPE, "
               "CHARACTER_SET_NAME, COLLATION_NAME, COLUMN_KEY, EXTRA, COLUMN_COMMENT, GENERATION_EXPRESSION, SRS_ID "
               "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, ORDINAL_POSITION",
    "indexes": "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, COLLATION, SUB_PART, NULLABLE, "
               "INDEX_TYPE, COMMENT, INDEX_COMMENT, IS_VISIBLE, EXPRESSION FROM information_schema.STATISTICS "
               "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX",
    "constraints": "SELECT TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE, ENFORCED FROM "
                   "information_schema.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, CONSTRAINT_NAME",
    "checks": "SELECT CONSTRAINT_NAME, CHECK_CLAUSE FROM information_schema.CHECK_CONSTRAINTS "
              "WHERE CONSTRAINT_SCHEMA = %s ORDER BY CONSTRAINT_NAME",
    "fks": "SELECT TABLE_NAME, CONSTRAINT_NAME, UNIQUE_CONSTRAINT_NAME, MATCH_OPTION, UPDATE_RULE, DELETE_RULE, "
           "REFERENCED_TABLE_NAME FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = %s "
           "ORDER BY TABLE_NAME, CONSTRAINT_NAME",
    "key_columns": "SELECT TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME, ORDINAL_POSITION, POSITION_IN_UNIQUE_CONSTRAINT, "
                   "REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME, REFERENCED_TABLE_SCHEMA = TABLE_SCHEMA "
                   "FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = %s "
                   "ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION",
    "partitions": "SELECT TABLE_NAME, PARTITION_NAME, SUBPARTITION_NAME, PARTITION_ORDINAL_POSITION, "
                  "SUBPARTITION_ORDINAL_POSITION, PARTITION_METHOD, SUBPARTITION_METHOD, PARTITION_EXPRESSION, "
                  "SUBPARTITION_EXPRESSION, PARTITION_DESCRIPTION, PARTITION_COMMENT, TABLESPACE_NAME "
                  "FROM information_schema.PARTITIONS WHERE TABLE_SCHEMA = %s AND PARTITION_NAME IS NOT NULL "
                  "ORDER BY TABLE_NAME, PARTITION_ORDINAL_POSITION, SUBPARTITION_ORDINAL_POSITION",
    "views": "SELECT TABLE_NAME, REPLACE(VIEW_DEFINITION, CONCAT('`', TABLE_SCHEMA, '`.'), '<db>.'), CHECK_OPTION, "
             "IS_UPDATABLE, DEFINER, SECURITY_TYPE, CHARACTER_SET_CLIENT, COLLATION_CONNECTION "
             "FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
    "routines": "SELECT ROUTINE_NAME, ROUTINE_TYPE, DATA_TYPE, CHARACTER_SET_NAME, COLLATION_NAME, DTD_IDENTIFIER, "
                "ROUTINE_BODY, ROUTINE_DEFINITION, IS_DETERMINISTIC, SQL_DATA_ACCESS, SECURITY_TYPE, SQL_MODE, "
                "ROUTINE_COMMENT, DEFINER, CHARACTER_SET_CLIENT, COLLATION_CONNECTION, DATABASE_COLLATION "
                "FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s ORDER BY ROUTINE_NAME, ROUTINE_TYPE",
    "parameters": "SELECT SPECIFIC_NAME, ROUTINE_TYPE, ORDINAL_POSITION, PARAMETER_MODE, PARAMETER_NAME, "
                  "DTD_IDENTIFIER, CHARACTER_SET_NAME, COLLATION_NAME FROM information_schema.PARAMETERS "
                  "WHERE SPECIFIC_SCHEMA = %s ORDER BY SPECIFIC_NAME, ROUTINE_TYPE, ORDINAL_POSITION",
    "triggers": "SELECT TRIGGER_NAME, EVENT_MANIPULATION, EVENT_OBJECT_TABLE, ACTION_ORDER, ACTION_STATEMENT, "
                "ACTION_TIMING, SQL_MODE, DEFINER, CHARACTER_SET_CLIENT, COLLATION_CONNECTION, DATABASE_COLLATION "
                "FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s "
                "ORDER BY EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_ORDER",
    "events": "SELECT EVENT_NAME, DEFINER, TIME_ZONE, EVENT_BODY, EVENT_DEFINITION, EVENT_TYPE, EXECUTE_AT, "
              "INTERVAL_VALUE, INTERVAL_FIELD, SQL_MODE, STARTS, ENDS, STATUS, ON_COMPLETION, EVENT_COMMENT, "
              "CHARACTER_SET_CLIENT, COLLATION_CONNECTION, DATABASE_COLLATION FROM information_schema.EVENTS "
              "WHERE EVENT_SCHEMA = %s ORDER BY EVENT_NAME",
    "histograms": "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMN_STATISTICS WHERE SCHEMA_NAME = %s "
                  "ORDER BY 1, 2",
}


def root(database: str = "mysql", **extra) -> mysql.connector.MySQLConnection:
    return mysql.connector.connect(**mysql_driver_kwargs(user="root", password="dbaguy", autocommit=True,
                                                         database=database, **extra))


def run(conn, *statements: str, raw: bool = False) -> list[tuple] | None:
    found = None
    for statement in statements:
        cursor = conn.cursor(raw=raw)
        cursor.execute(statement)
        found = cursor.fetchall() if cursor.with_rows else None
        cursor.close()
    return found


def _q(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def inventory(database: str) -> dict[str, list[tuple]]:
    """information_schema's rows about `database` as root (statistics read fresh), each table's SHOW CREATE TABLE,
    and its rows' digest: (count, sha256 of the sorted raw rows)."""
    conn = root(database)
    try:
        run(conn, "SET SESSION information_schema_stats_expiry = 0")
        found: dict[str, list[tuple]] = {}
        for key, sql in INVENTORY.items():
            cursor = conn.cursor()
            cursor.execute(sql, (database,))
            found[key] = [tuple(row) for row in cursor.fetchall()]
            cursor.close()
        tables = [row[0] for row in found["tables"] if row[1] == "BASE TABLE"]
        found["show"] = [(table, run(conn, f"SHOW CREATE TABLE {_q(table)}")[0][1]) for table in tables]
        found["rows"] = []
        for table in tables:
            columns = ", ".join(_q(row[1]) for row in found["columns"] if row[0] == table)
            rows = run(conn, f"SELECT {columns} FROM {_q(table)}", raw=True)
            key = sorted(rows, key=lambda r: tuple((v is None, bytes(v) if v is not None else b"") for v in r))
            found["rows"].append((table, len(rows), hashlib.sha256(repr(key).encode()).hexdigest()[:16]))
        return found
    finally:
        conn.close()


def without(found: dict[str, list[tuple]], *names: str) -> dict[str, list[tuple]]:
    """`found` less every row about the objects `names` (each row's first column names its object)."""
    return {key: [row for row in rows if row[0] not in names] for key, rows in found.items()}


def differences(source: dict[str, list[tuple]], restored: dict[str, list[tuple]]) -> list[str]:
    """What the restored inventory lacks or has more of, per kind of row; [] when they are the same."""
    found = []
    for key in sorted(set(source) | set(restored)):
        a, b = source.get(key, []), restored.get(key, [])
        if a != b:
            found.append(f"{key}: source-only {[r for r in a if r not in b][:6]} / restored-only "
                         f"{[r for r in b if r not in a][:6]}")
    return found
