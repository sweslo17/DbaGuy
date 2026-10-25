import type { Engine } from "@/lib/api/types";
import { fileNameOf, safeFileStem } from "@/lib/filePaths";
import type { DumpEntry } from "./entryPoints";

/** `yyyyMMdd-HHmm` in local time. */
export function dumpStamp(at: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${at.getFullYear()}${pad(at.getMonth() + 1)}${pad(at.getDate())}-${pad(at.getHours())}${pad(at.getMinutes())}`;
}

/** A SQLite file's stem ("app" of "/x/app.sqlite3"): its dumps are named after it. */
export function sqliteStem(path: string): string {
  const name = fileNameOf(path);
  return name.replace(/\.[^.]+$/, "") || name;
}

/**
 * A dump's default file name (spec ⑥ §3.3.4): `<database>_<schema or table>_<yyyyMMdd-HHmm>.sql`. The database is the
 * entry's (PostgreSQL, SQL Server), MySQL's schema (it is the database: not written twice), or the SQLite file's stem
 * (`sqlitePath`); SQLite's "main" says nothing. A Tables folder says how many tables are checked, when any are (06b
 * F-f: no "0-tables").
 */
export function dumpFileName(entry: DumpEntry, engine: Engine, sqlitePath: string | null, tables: number, at: Date): string {
  const target = entry.target;
  const database =
    entry.database ??
    (engine === "sqlite" && sqlitePath ? sqliteStem(sqlitePath) : engine === "mysql" && target.kind !== "database" ? target.schema : null);
  const parts =
    target.kind === "database"
      ? [database]
      : target.kind === "schema"
        ? [database, target.schema]
        : target.kind === "relation"
          ? [database, target.name]
          : [database, target.schema, tables > 0 ? `${tables}-tables` : null];
  const named = parts.filter((part): part is string => !!part && part !== "main");
  const unique = named.filter((part, i) => named.indexOf(part) === i);
  return `${safeFileStem(unique.join("_"), "dump")}_${dumpStamp(at)}.sql`;
}
