import type { EngineInfo, ObjectNode, PathSegment, Profile, QueryContext } from "@/lib/api/types";
import type { Tab } from "@/stores/workspace";
import { relationOf } from "../table-data/filterValue";

/*
 * Where Dump… and Run SQL file… start (spec ⑥ §3.6 入口, D-06-13): a tree node, the table-data tab, a script, the
 * palette. Each gives the dialog its connection, its database and what the node means (a dump's scope, a run's
 * database, search path or query context). Pure; the engine's capabilities decide whether there is an entry at all.
 */

/** What a dump covers, as the tree names it: its scope is built from it and the dialog's choices (`dumpForm.ts`). */
export type DumpTarget =
  | { kind: "database"; database: string }
  | { kind: "schema"; schema: string }
  /** The Tables folder: the dialog lists its tables (the folder's children) and dumps the ones checked. */
  | { kind: "tables"; schema: string; folder: PathSegment[] }
  /** One table, view, materialized view or partition. */
  | { kind: "relation"; schema: string; name: string; relationKind: string };

export interface DumpEntry {
  profileId: string;
  /** The session's (and the plan's) database: PostgreSQL's and SQL Server's database node; else null (the profile's). */
  database: string | null;
  target: DumpTarget;
  /** The table-data tab's filter: "Only rows matching the filter" (one table). */
  filter: string | null;
}

export interface RunEntry {
  /** null: the dialog asks (a script, with no connection chosen yet). */
  profileId: string | null;
  /** The session's database: a database node (PostgreSQL, SQL Server) or a schema node (MySQL); null: the profile's. */
  database: string | null;
  /** PostgreSQL: the schema node's name, sent as the run's search path. */
  searchPath: string | null;
  /** Couchbase: the bucket or scope node; null: the connection's default. */
  queryContext: QueryContext | null;
  /** The file; "" until chosen. */
  path: string;
  /** "Run again…": the form starts with these. */
  preset?: { mode?: "all_or_nothing" | "statements"; on_error?: "stop" | "continue"; from_statement?: number };
  /** Said above the file (a script's tab with changes not saved to it). */
  note?: string | null;
}

const RELATION_DATA: ReadonlySet<string> = new Set(["table", "partition"]);

/** Whether a relation kind has rows a dump writes (a view, a materialized view: only its definition, spec §3.3.1). */
export const relationHasRows = (relationKind: string): boolean => RELATION_DATA.has(relationKind);

const schemaOf = (path: PathSegment[]) => path.findLast((segment) => segment.kind === "schema")?.name ?? null;
const databaseOf = (path: PathSegment[]) => (path[0]?.kind === "database" ? path[0].name : null);

/** Dump… on a tree node: a database, schema, Tables folder or relation of an engine that dumps; else null. */
export function dumpEntryForNode(profileId: string, engine: EngineInfo | null, node: ObjectNode): DumpEntry | null {
  if (!engine?.dump) return null;
  const database = databaseOf(node.path);
  const entry = (target: DumpTarget): DumpEntry => ({ profileId, database, target, filter: null });
  switch (node.kind) {
    case "database":
      return entry({ kind: "database", database: node.name });
    case "schema":
      // SQLite dumps its main database only; MySQL's database is its schema node (06a S18)
      return engine.engine === "sqlite" && node.name !== "main" ? null : entry({ kind: "schema", schema: node.name });
    case "folder": {
      const schema = schemaOf(node.path);
      // the schema's own Tables folder, never a table's (its columns, indexes …)
      const parent = node.path.at(-2);
      if (node.name !== "tables" || !schema || parent?.kind !== "schema") return null;
      if (engine.engine === "sqlite" && schema !== "main") return null;
      return entry({ kind: "tables", schema, folder: node.path });
    }
    default: {
      const relation = relationOf(node.path);
      if (!relation?.schema || (engine.engine === "sqlite" && relation.schema !== "main")) return null;
      return entry({ kind: "relation", schema: relation.schema, name: relation.name, relationKind: relation.kind });
    }
  }
}

/** Dump table… in a table-data tab: its relation, with the filter it shows (empty: none). */
export function dumpEntryForTable(
  profileId: string,
  engine: EngineInfo | null,
  path: PathSegment[],
  filter: string,
): DumpEntry | null {
  const relation = relationOf(path);
  if (!engine?.dump || !relation?.schema) return null;
  if (engine.engine === "sqlite" && relation.schema !== "main") return null;
  const target: DumpTarget = { kind: "relation", schema: relation.schema, name: relation.name, relationKind: relation.kind };
  const kept = filter.trim();
  return { profileId, database: relation.database, target, filter: kept && relationHasRows(relation.kind) ? kept : null };
}

/** Run SQL file… on a tree node (null: the connection's row) of an engine that runs files. */
export function runEntryForNode(profileId: string, engine: EngineInfo | null, node: ObjectNode | null): RunEntry | null {
  if (!engine?.run_file) return null;
  const entry = (fields: Partial<RunEntry> = {}): RunEntry => ({
    profileId,
    database: null,
    searchPath: null,
    queryContext: null,
    path: "",
    ...fields,
  });
  if (!node) return entry();
  switch (node.kind) {
    case "database":
      return entry({ database: node.name });
    case "schema":
      if (engine.engine === "mysql") return entry({ database: node.name });
      if (engine.engine === "postgres") return entry({ database: databaseOf(node.path), searchPath: node.name });
      if (engine.engine === "mssql") return entry({ database: databaseOf(node.path) });
      return entry();
    case "bucket":
      return entry({ queryContext: { bucket: node.name } });
    case "scope":
      return entry({ queryContext: { bucket: node.path[0]?.name ?? "", scope: node.name } });
    default:
      return null;
  }
}

/**
 * Run on connection… on a script: the file, and the connection selected in the tree when there is one. `unsavedTab`: a
 * tab bound to the script holds changes not saved to it (the file as saved runs, and the dialog says so).
 */
export function runEntryForScript(absolutePath: string, profileId: string | null, unsavedTab: string | null = null): RunEntry {
  const note = unsavedTab ? `${unsavedTab} has changes not saved to this file: the file as saved runs` : null;
  return { profileId, database: null, searchPath: null, queryContext: null, path: absolutePath, note };
}

/** ⌘⇧P Dump…: the active table tab's table (with its filter), else the active tab's connection as a whole. */
export function paletteDumpEntry(
  tab: Tab | null,
  profile: Profile | null,
  engine: EngineInfo | null,
  tableFilter: string,
): DumpEntry | null {
  if (!tab || !profile || !engine?.dump || tab.profileId !== profile.id) return null;
  if (tab.kind === "table") return dumpEntryForTable(profile.id, engine, tab.path, tableFilter);
  if (tab.kind !== "query") return null;
  const database = tab.database ?? ("database" in profile ? (profile.database ?? null) : null);
  switch (engine.engine) {
    case "sqlite":
      return { profileId: profile.id, database: null, target: { kind: "schema", schema: "main" }, filter: null };
    case "mysql":
      return database ? { profileId: profile.id, database: null, target: { kind: "schema", schema: database }, filter: null } : null;
    default: // PostgreSQL, SQL Server: the tab's database
      return database ? { profileId: profile.id, database, target: { kind: "database", database }, filter: null } : null;
  }
}

/**
 * ⌘⇧P Run SQL file…: on the active tab's connection and database, else the one selected in the tree. A query tab's
 * database is its own; for any other tab it is where its object sits in the tree: the path's database node (PostgreSQL,
 * SQL Server) or, for MySQL, its schema node, which is the database there (06b pre-flight L5: as `runEntryForNode`).
 */
export function paletteRunEntry(tab: Tab | null, selectedProfileId: string | null, engine: EngineInfo | null): RunEntry {
  const fromTab = tab && tab.profileId ? tab : null;
  const database =
    fromTab?.kind === "query"
      ? fromTab.database
      : fromTab
        ? engine?.engine === "mysql"
          ? schemaOf(fromTab.path)
          : databaseOf(fromTab.path)
        : null;
  const context = fromTab?.kind === "query" && typeof fromTab.queryContext === "object" ? fromTab.queryContext : null;
  return { profileId: fromTab?.profileId ?? selectedProfileId, database, searchPath: null, queryContext: context, path: "" };
}
