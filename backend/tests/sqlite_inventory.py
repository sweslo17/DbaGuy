"""SQLite dump tests' independent check (06a Task 6 review, ruling T10's SQLite side), adopted from the reviewer's
probe: a file's sqlite_master (less virtual tables, their internal tables and SQLite's statistics, which no dump
writes), its header numbers, sqlite_sequence, every table's pragma_table_xinfo, foreign keys and indexes, and its rows
with their storage types — never the dump's own reader."""

import sqlite3
from pathlib import Path


def _literal(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def inventory(path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(path)
    try:
        kinds = dict(conn.execute("SELECT name, type FROM pragma_table_list WHERE schema = 'main'").fetchall())
        found: dict[str, list[tuple]] = {
            "master": [row for row in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name")
                       if kinds.get(row[2]) not in ("virtual", "shadow") and not row[1].startswith("sqlite_stat")],
            "user_version": conn.execute("PRAGMA user_version").fetchall(),
            "application_id": conn.execute("PRAGMA application_id").fetchall(),
        }
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
            found["sequence"] = sorted(conn.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
        for (name,) in conn.execute("SELECT name FROM pragma_table_list WHERE schema = 'main' AND type = 'table' "
                                    "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            found[f"xinfo {name}"] = conn.execute(f"SELECT * FROM pragma_table_xinfo({_literal(name)})").fetchall()
            found[f"fk {name}"] = conn.execute(f"SELECT * FROM pragma_foreign_key_list({_literal(name)})").fetchall()
            found[f"idx {name}"] = conn.execute(
                f"SELECT * FROM pragma_index_list({_literal(name)}) ORDER BY name").fetchall()
            columns = ['"' + row[1].replace('"', '""') + '"' for row in found[f"xinfo {name}"]]
            select = ", ".join(f"{column}, typeof({column})" for column in columns)
            quoted = '"' + name.replace('"', '""') + '"'
            found[f"rows {name}"] = sorted(conn.execute(f"SELECT {select} FROM {quoted}").fetchall(), key=repr)
        return found
    finally:
        conn.close()


def differences(source: dict[str, list[tuple]], restored: dict[str, list[tuple]]) -> list[str]:
    return [f"{key}: source {source.get(key)} / restored {restored.get(key)}"
            for key in sorted(set(source) | set(restored)) if source.get(key) != restored.get(key)]
