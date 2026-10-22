import { describe, expect, it } from "vitest";
import type { EngineInfo, ObjectNode, PathSegment } from "@/lib/api/types";
import type { ObjectTabState, QueryTabState, TableTabState } from "@/stores/workspace";
import {
  dumpEntryForNode,
  dumpEntryForTable,
  paletteDumpEntry,
  paletteRunEntry,
  runEntryForNode,
  runEntryForScript,
} from "./entryPoints";
import { dumpFileName, dumpStamp, sqliteStem } from "./fileNames";

/* Where Dump… and Run SQL file… start (spec ⑥ §3.6): the scope and defaults each tree node gives, by capability. */

const engine = (name: EngineInfo["engine"], fields: Partial<EngineInfo> = {}): EngineInfo => ({
  engine: name,
  default_port: null,
  url_schemes: [],
  dialect: name === "mssql" ? "tsql" : name === "couchbase" ? "sqlpp" : name,
  per_database_connection: name === "postgres",
  cancel_aborts_transaction: name === "postgres",
  server_read_only: name !== "mssql",
  transactions: name !== "couchbase",
  query_context: name === "couchbase",
  parameters: name === "couchbase",
  documents: name === "couchbase",
  executed_plan: false,
  plan_format: null,
  row_editing: name !== "couchbase",
  orders_browse_by_key: name === "postgres",
  run_file: true,
  dump: name !== "couchbase",
  ...fields,
});
const seg = (kind: string, name: string): PathSegment => ({ kind, name });
const node = (path: PathSegment[]): ObjectNode => {
  const last = path[path.length - 1];
  return { kind: last.kind, name: last.name, path, has_children: true, system: false };
};
const PG = engine("postgres");
const MYSQL = engine("mysql");
const MSSQL = engine("mssql");
const SQLITE = engine("sqlite");
const COUCHBASE = engine("couchbase");
const P = "p1";

describe("Dump… on the tree", () => {
  it("dumps a PostgreSQL database, schema, Tables folder, table and view on the database's own session", () => {
    const db = [seg("database", "dbaguy")];
    const schema = [...db, seg("schema", "public")];
    const folder = [...schema, seg("folder", "tables")];
    expect(dumpEntryForNode(P, PG, node(db))).toEqual({ profileId: P, database: "dbaguy", target: { kind: "database", database: "dbaguy" }, filter: null });
    expect(dumpEntryForNode(P, PG, node(schema))?.target).toEqual({ kind: "schema", schema: "public" });
    expect(dumpEntryForNode(P, PG, node(folder))?.target).toEqual({ kind: "tables", schema: "public", folder });
    expect(dumpEntryForNode(P, PG, node([...folder, seg("table", "orders")]))?.target).toEqual({
      kind: "relation", schema: "public", name: "orders", relationKind: "table",
    });
    const view = node([...schema, seg("folder", "views"), seg("view", "v")]);
    expect(dumpEntryForNode(P, PG, view)?.target).toEqual({ kind: "relation", schema: "public", name: "v", relationKind: "view" });
  });

  it("dumps MySQL's database as its schema node, with no database of the session's own (06a S18)", () => {
    const schema = [seg("schema", "shop")];
    expect(dumpEntryForNode(P, MYSQL, node(schema))).toEqual({ profileId: P, database: null, target: { kind: "schema", schema: "shop" }, filter: null });
    expect(dumpEntryForNode(P, MYSQL, node([...schema, seg("folder", "tables"), seg("table", "t")]))?.target).toMatchObject({ schema: "shop", name: "t" });
  });

  it("dumps only SQLite's main database", () => {
    expect(dumpEntryForNode(P, SQLITE, node([seg("schema", "main")]))?.target).toEqual({ kind: "schema", schema: "main" });
    expect(dumpEntryForNode(P, SQLITE, node([seg("schema", "aux")]))).toBeNull();
  });

  it("offers nothing on objects that are not dumped alone, nor on an engine without dumps", () => {
    const table = [seg("database", "d"), seg("schema", "s"), seg("folder", "tables"), seg("table", "t")];
    expect(dumpEntryForNode(P, PG, node([...table, seg("folder", "columns")]))).toBeNull();
    expect(dumpEntryForNode(P, PG, node([seg("database", "d"), seg("schema", "s"), seg("folder", "functions"), seg("function", "f()")]))).toBeNull();
    expect(dumpEntryForNode(P, COUCHBASE, node([seg("bucket", "b")]))).toBeNull();
    expect(dumpEntryForNode(P, null, node(table))).toBeNull();
  });

  it("takes the table-data tab's filter for a table, never for a view", () => {
    const path = [seg("database", "d"), seg("schema", "s"), seg("folder", "tables"), seg("table", "t")];
    expect(dumpEntryForTable(P, PG, path, "  status = 'open' ")?.filter).toBe("status = 'open'");
    expect(dumpEntryForTable(P, PG, path, "   ")?.filter).toBeNull();
    const view = [seg("database", "d"), seg("schema", "s"), seg("folder", "views"), seg("view", "v")];
    expect(dumpEntryForTable(P, PG, view, "x = 1")?.filter).toBeNull();
  });
});

describe("Run SQL file… on the tree", () => {
  it("runs on the node's database, PostgreSQL's schema as the search path", () => {
    expect(runEntryForNode(P, PG, node([seg("database", "dbaguy"), seg("schema", "sales")]))).toEqual({
      profileId: P, database: "dbaguy", searchPath: "sales", queryContext: null, path: "",
    });
    expect(runEntryForNode(P, MYSQL, node([seg("schema", "shop")]))?.database).toBe("shop");
    expect(runEntryForNode(P, MSSQL, node([seg("database", "app"), seg("schema", "dbo")]))).toMatchObject({ database: "app", searchPath: null });
    expect(runEntryForNode(P, PG, null)).toEqual({ profileId: P, database: null, searchPath: null, queryContext: null, path: "" });
  });

  it("runs on Couchbase in the bucket's or scope's context", () => {
    expect(runEntryForNode(P, COUCHBASE, node([seg("bucket", "b")]))?.queryContext).toEqual({ bucket: "b" });
    expect(runEntryForNode(P, COUCHBASE, node([seg("bucket", "b"), seg("scope", "s")]))?.queryContext).toEqual({ bucket: "b", scope: "s" });
  });

  it("offers nothing on other nodes or engines without the tool", () => {
    expect(runEntryForNode(P, PG, node([seg("database", "d"), seg("schema", "s"), seg("folder", "tables")]))).toBeNull();
    expect(runEntryForNode(P, engine("postgres", { run_file: false }), null)).toBeNull();
  });

  it("runs a script on the connection chosen in the tree", () => {
    expect(runEntryForScript("/u/scripts/a.sql", null)).toEqual({ profileId: null, database: null, searchPath: null, queryContext: null, path: "/u/scripts/a.sql", note: null });
    expect(runEntryForScript("/u/scripts/a.sql", "p1", "a.sql").note).toBe("a.sql has changes not saved to this file: the file as saved runs");
  });
});

describe("the palette's entries", () => {
  const profile = { id: P, name: "Local PG", engine: "postgres", database: "dbaguy" } as never;
  const query = (fields: Partial<QueryTabState> = {}): QueryTabState => ({
    id: "t1", kind: "query", title: "Q", profileId: P, database: null, scriptPath: null, savedText: null,
    textFromFile: false, initialText: "", autocommit: true, ...fields,
  });
  const table: TableTabState = { id: "t2", kind: "table", profileId: P, title: "t", path: [seg("database", "d"), seg("schema", "s"), seg("folder", "tables"), seg("table", "t")] };

  it("dumps the active table with its filter, or the query tab's database", () => {
    expect(paletteDumpEntry(table, profile, PG, "a > 1")).toMatchObject({ database: "d", filter: "a > 1", target: { kind: "relation", name: "t" } });
    expect(paletteDumpEntry(query(), profile, PG, "")).toMatchObject({ database: "dbaguy", target: { kind: "database", database: "dbaguy" } });
    expect(paletteDumpEntry(query({ database: "analytics" }), profile, PG, "")?.database).toBe("analytics");
    expect(paletteDumpEntry(null, profile, PG, "")).toBeNull();
  });

  it("runs on the active tab's connection, else the tree's", () => {
    expect(paletteRunEntry(query({ database: "analytics" }), "other", PG)).toMatchObject({ profileId: P, database: "analytics" });
    expect(paletteRunEntry(table, null, PG)).toMatchObject({ profileId: P, database: "d" });
    expect(paletteRunEntry(null, "other", PG).profileId).toBe("other");
    expect(paletteRunEntry(query({ queryContext: { bucket: "b" } }), null, COUCHBASE).queryContext).toEqual({ bucket: "b" });
  });

  it("runs a MySQL table tab on the table's schema, which is its database there (06b L5), as the tree does", () => {
    const mysqlTable: TableTabState = { ...table, path: [seg("schema", "shop"), seg("folder", "tables"), seg("table", "t")] };
    expect(paletteRunEntry(mysqlTable, null, MYSQL)).toMatchObject({ profileId: P, database: "shop", searchPath: null });
    expect(paletteRunEntry(mysqlTable, null, MYSQL).database).toBe(
      runEntryForNode(P, MYSQL, node([seg("schema", "shop")]))?.database,
    );
    // an engine that is not known yet: no database is guessed for a path that has no database node
    expect(paletteRunEntry(mysqlTable, null, null).database).toBeNull();
    // SQLite's schema is "main" and no database of the session's own
    const sqliteTable: TableTabState = { ...table, path: [seg("schema", "main"), seg("folder", "tables"), seg("table", "t")] };
    expect(paletteRunEntry(sqliteTable, null, SQLITE).database).toBeNull();
  });

  it("runs another PostgreSQL object's tab on its database too, not only a table's", () => {
    const fn: ObjectTabState = {
      id: "t3", kind: "object", profileId: P, title: "f",
      path: [seg("database", "analytics"), seg("schema", "s"), seg("folder", "functions"), seg("function", "f()")],
    };
    expect(paletteRunEntry(fn, null, PG)).toMatchObject({ profileId: P, database: "analytics" });
  });
});

describe("a dump's file name", () => {
  const at = new Date(2026, 9, 23, 14, 5);
  it("is database_schema-or-table_stamp.sql, without what macOS does not take", () => {
    expect(dumpStamp(at)).toBe("20261023-1405");
    const schema = dumpEntryForNode(P, PG, node([seg("database", "dbaguy"), seg("schema", "public")]))!;
    expect(dumpFileName(schema, "postgres", null, 0, at)).toBe("dbaguy_public_20261023-1405.sql");
    const db = dumpEntryForNode(P, PG, node([seg("database", "dbaguy")]))!;
    expect(dumpFileName(db, "postgres", null, 0, at)).toBe("dbaguy_20261023-1405.sql");
    const mysql = dumpEntryForNode(P, MYSQL, node([seg("schema", "shop"), seg("folder", "tables"), seg("table", "a/b:c")]))!;
    expect(dumpFileName(mysql, "mysql", null, 0, at)).toBe("shop_abc_20261023-1405.sql");
    const mysqlSchema = dumpEntryForNode(P, MYSQL, node([seg("schema", "shop")]))!;
    expect(dumpFileName(mysqlSchema, "mysql", null, 0, at)).toBe("shop_20261023-1405.sql");
    const folder = dumpEntryForNode(P, MYSQL, node([seg("schema", "shop"), seg("folder", "tables")]))!;
    expect(dumpFileName(folder, "mysql", null, 3, at)).toBe("shop_3-tables_20261023-1405.sql");
    const sqlite = dumpEntryForNode(P, SQLITE, node([seg("schema", "main")]))!;
    expect(dumpFileName(sqlite, "sqlite", "/x/app.sqlite3", 0, at)).toBe("app_20261023-1405.sql");
    const odd = dumpEntryForNode(P, MYSQL, node([seg("schema", "a\nb")]))!;
    expect(dumpFileName(odd, "mysql", null, 0, at)).toBe("a b_20261023-1405.sql");
    expect(sqliteStem("/x/.hidden")).toBe(".hidden");
  });
});
