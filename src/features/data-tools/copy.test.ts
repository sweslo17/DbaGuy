import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api/client";
import type { ColumnMapping, EngineInfo, ObjectNode, PathSegment, Profile, TableTransfer, TransferPlan } from "@/lib/api/types";
import {
  copyCommand,
  copyDialogTitle,
  copyJobTitle,
  copyProblem,
  copyRequest,
  initialCopyForm,
  passwordOwner,
  QUERY_TABLE,
  requestProblem,
  restForm,
  tablesProblem,
  withTarget,
  type CopyForm,
} from "./copyForm";
import type { CopyEntry } from "./entryPoints";
import { mappingRows, previewProblems, statementLines, withIdentity, withLeftOut, withOverride } from "./mappingRows";

/* Copy data's form and its column mapping (spec ⑥ §3.6 CopyDataDialog, MappingTable; 06c Task 6). */

const seg = (kind: string, name: string): PathSegment => ({ kind, name });
const ordersPath = [seg("database", "shop"), seg("schema", "public"), seg("folder", "tables"), seg("table", "orders")];
const relation: CopyEntry = {
  profileId: "src",
  database: "shop",
  source: { kind: "relation", path: ordersPath, schema: "public", name: "orders", relationKind: "table", filter: "id > 5" },
};
const folder = [seg("database", "shop"), seg("schema", "public"), seg("folder", "tables")];
const tables: CopyEntry = { profileId: "src", database: "shop", source: { kind: "tables", schema: "public", folder } };
const query: CopyEntry = {
  profileId: "src",
  database: null,
  source: { kind: "query", sessionId: "tab-1", sql: "SELECT 1 AS a", columns: [{ name: "a", type_name: "int4", category: "number" }], title: "Query 1" },
};
const node = (name: string): ObjectNode => ({ kind: "table", name, path: [...folder, seg("table", name)], has_children: true, system: false });
const listed = [node("a"), node("b"), node("c")];
const mysql = { engine: "mysql", per_database_connection: false } as EngineInfo;
const pg = { engine: "postgres", per_database_connection: true } as EngineInfo;
const profile = (fields: Partial<Profile> = {}) => ({ id: "dst", name: "Dev", engine: "mysql", database: "app", read_only: false, ...fields }) as Profile;
const targeted = (entry: CopyEntry, fields: Partial<CopyForm> = {}): CopyForm => ({
  ...withTarget(initialCopyForm(entry), profile(), mysql),
  ...fields,
});

describe("the copy form", () => {
  it("starts with the source's own name, its filter on, and no target", () => {
    expect([initialCopyForm(relation).table, initialCopyForm(relation).onlyFiltered]).toEqual(["orders", true]);
    expect([initialCopyForm(query).table, initialCopyForm(tables).table, initialCopyForm(tables).checked]).toEqual([QUERY_TABLE, "", []]);
    expect(requestProblem(relation, initialCopyForm(relation), null)).toBe("Choose the connection to copy into");
  });

  it("takes a target's database and its default schema (MySQL: the database itself)", () => {
    expect(withTarget(initialCopyForm(relation), profile(), mysql)).toMatchObject({ targetDatabase: null, targetSchema: "app" });
    expect(withTarget(initialCopyForm(relation), profile({ engine: "postgres", database: "dw" }), pg)).toMatchObject({
      targetDatabase: "dw",
      targetSchema: "public",
    });
  });

  it("drops the types typed over for the connection before when the target changes, keeping the rest of the form", () => {
    const typed = withLeftOut(withOverride(targeted(relation, { mode: "append" }), "public.orders", "id", "numeric(20)"), "public.orders", "note", true);
    const other = withTarget(typed, profile({ id: "dst2", engine: "postgres", database: "dw" }), pg);
    expect([other.overrides, other.targetProfileId, other.targetSchema]).toEqual([{}, "dst2", "public"]);
    expect([other.leftOut, other.mode, other.table]).toEqual([{ "public.orders": ["note"] }, "append", "orders"]);
  });

  it("asks for a relation with its filter, a schema's checked tables in the tree's order, a query with its columns", () => {
    expect(copyRequest(relation, targeted(relation), null)).toEqual({
      source: { profile_id: "src", database: "shop", tables: [ordersPath], filter: "id > 5" },
      target: { profile_id: "dst", database: null, schema_name: "app", table: "orders", mode: "create", overrides: {}, identity: {}, left_out: {} },
    });
    expect(copyRequest(relation, targeted(relation, { onlyFiltered: false }), null)?.source.filter).toBeUndefined();
    const some = copyRequest(tables, targeted(tables, { checked: ["c", "a"] }), listed);
    expect([some?.source.tables, some?.target.table]).toEqual([[listed[0].path, listed[2].path], undefined]);
    expect(copyRequest(query, targeted(query), null)?.source.query?.columns).toEqual(query.source.kind === "query" ? query.source.columns : []);
  });

  it("says what is missing before it asks", () => {
    expect(requestProblem(tables, targeted(tables), null)).toBe("Reading the tables…");
    expect(requestProblem(tables, targeted(tables), listed)).toBe("Check the tables to copy");
    expect(requestProblem(relation, targeted(relation, { table: "  " }), null)).toBe("Name the table to copy into");
    expect(requestProblem(relation, targeted(relation, { targetSchema: null }), null)).toBe("Choose the schema to copy into");
  });

  it("copies only the preview shown, with a plan, and a query reads on its tab's session", () => {
    const plan = { plan_id: "p1", source_profile_id: "src", tables: [], warnings: [], problems: [] } as TransferPlan;
    expect(copyProblem(relation, targeted(relation), null, plan, true)).toBe("Reading the preview…");
    expect(copyProblem(relation, targeted(relation), null, { ...plan, plan_id: null }, false)).toBe("The preview has problems: see them above");
    expect(copyProblem(relation, targeted(relation), null, plan, false)).toBeNull();
    expect(copyCommand(plan, query, "r1")).toEqual({ type: "copy_data", request_id: "r1", plan_id: "p1", source_session_id: "tab-1" });
    expect(copyCommand(plan, relation, "r1")?.source_session_id).toBeNull();
  });

  it("never makes a command of a preview without a plan id (it has problems)", () => {
    const plan = { plan_id: null, source_profile_id: "src", tables: [], warnings: [], problems: [] } as TransferPlan;
    expect(copyCommand(plan, relation, "r1")).toBeNull();
    expect(copyCommand({ ...plan, plan_id: "" }, relation, "r1")).toBeNull();
  });

  it("says what is wrong with the checked tables, for the list to show: none, or more than a copy takes", () => {
    expect(tablesProblem(tables, targeted(tables, { checked: [] }), listed)).toBe("Check the tables to copy");
    expect(tablesProblem(tables, targeted(tables, { checked: ["a", "c"] }), listed)).toBeNull();
    expect(tablesProblem(tables, targeted(tables, { checked: [] }), null)).toBeNull(); // not listed yet
    expect(tablesProblem(relation, targeted(relation), null)).toBeNull(); // not a schema's copy
    const many = Array.from({ length: 501 }, (_, i) => node(`t${i}`));
    expect(tablesProblem(tables, targeted(tables, { checked: many.map((t) => t.name) }), many)).toBe("A copy takes at most 500 tables");
    expect(requestProblem(tables, targeted(tables, { checked: [] }), listed)).toBe("Check the tables to copy"); // the same words
  });

  it("names the dialog and the job by what is copied", () => {
    expect([copyDialogTitle(relation), copyDialogTitle(tables), copyDialogTitle(query)]).toEqual([
      "Copy public.orders",
      "Copy tables of public",
      "Copy the result of Query 1 to a table",
    ]);
    const plan = { tables: [{ source: "public.orders" }, { source: "public.items" }] } as TransferPlan;
    expect(copyJobTitle(plan, "Local PG", "Dev")).toBe("Copy 2 tables from Local PG to Dev");
    expect(restForm(targeted(tables, { checked: ["a", "b", "c"] }), ["c"]).checked).toEqual(["c"]);
  });
});

const col = (fields: Partial<ColumnMapping> & Pick<ColumnMapping, "source_name" | "source_type" | "family">): ColumnMapping => ({
  type_editable: false,
  overridden: false,
  ...fields,
});
const table: TableTransfer = {
  source: "public.orders",
  target: "orders",
  source_key: "public.orders",
  mode: "create",
  columns: [
    col({ source_name: "id", source_type: "bigint", family: "int64", target_name: "id", target_type: "bigint", type_editable: true }),
    col({ source_name: "placed", source_type: "timestamptz", family: "timestamp_tz", target_name: "placed", target_type: "datetime(6)", type_editable: true, conversion: "converted to UTC: the offset is not kept" }),
    col({ source_name: "g", source_type: "geography", family: "spatial", target_name: "g", type_editable: true, problem: "g (geography) has no MySQL type: choose one, or leave the column out" }),
    col({ source_name: "note", source_type: "text", family: "text", skipped: "left out" }),
    col({ source_name: "v", source_type: "sql_variant", family: "variant", skipped: "not copied: sql_variant column: only its raw bytes can be read" }),
  ],
  before: ["CREATE TABLE orders (…)", "DELETE FROM orders"],
  after: ["SELECT setval(…)"],
  warnings: [],
  problems: [{ code: "unmappable_type", message: "g (geography) has no MySQL type: choose one, or leave the column out", column: "g" }],
};

describe("whose password a preview asks for (pre-flight M3)", () => {
  const needed = (detail: unknown) => new ApiError("A password is required", "password_required", 428, detail);
  it("is the profile the backend names, else the source's", () => {
    expect(passwordOwner(needed({ password: true, ssh_password: false, profile_id: "dst" }), "src")).toBe("dst");
    expect(passwordOwner(needed({ password: true, ssh_password: false }), "src")).toBe("src");
    expect(passwordOwner(needed(null), "src")).toBe("src");
    expect(passwordOwner(needed(undefined), "src")).toBe("src");
    expect(passwordOwner(needed({ profile_id: 7 }), "src")).toBe("src");
    expect(passwordOwner(new Error("x"), "src")).toBe("src");
    // an inherited property is not a named profile (05b K2)
    expect(passwordOwner(needed(JSON.parse('{"__proto__": {"profile_id": "evil"}}')), "src")).toBe("src");
  });
});

describe("the column mapping", () => {
  it("shows each column, what changes on the way, what blocks the copy and what can be left out", () => {
    const form = withLeftOut(targeted(relation), "public.orders", "note", true);
    const rows = mappingRows(table, withOverride(form, "public.orders", "id", "numeric(20)"));
    expect(rows.map((row) => [row.source, row.target, row.typed, row.leftOut, row.canLeaveOut])).toEqual([
      ["id", "id", "numeric(20)", false, true],
      ["placed", "placed", null, false, true],
      ["g", "g", null, false, true],
      ["note", null, null, true, true],
      ["v", null, null, false, false],
    ]);
    expect([rows[1].conversion, rows[2].problem]).toEqual(["converted to UTC: the offset is not kept", table.problems[0].message]);
  });

  it("shows what the form asks for at once, before the next preview answers (a stale preview is not the form)", () => {
    const identity = col({ source_name: "id", source_type: "int", family: "int32", target_name: "id", target_type: "int", identity: "keep" });
    const stale: TableTransfer = { ...table, columns: [identity, table.columns[1]] };
    const asked = withLeftOut(withIdentity(targeted(relation), "public.orders", "id", "generate"), "public.orders", "placed", true);
    const rows = mappingRows(stale, asked);
    // the preview still says "keep" and still copies `placed`: the form's choices show, not the old preview's
    expect([rows[0].identity, rows[1].leftOut, rows[1].canLeaveOut]).toEqual(["generate", true, true]);
    // no identity column in the preview: no choice to show, whatever the form holds
    expect(mappingRows({ ...stale, columns: [table.columns[0]] }, asked)[0].identity).toBeNull();
    expect(mappingRows(stale, targeted(relation))[0].identity).toBe("keep");
  });

  it("keeps a typed type, an identity's choice and a column left out per table, clearing what goes back", () => {
    let form = withOverride(targeted(relation), "t", "a", "text");
    expect(form.overrides).toEqual({ t: { a: "text" } });
    expect(withOverride(form, "t", "a", " ").overrides).toEqual({});
    form = withIdentity(form, "t", "id", "generate");
    expect([form.identity, withIdentity(form, "t", "id", "keep").identity]).toEqual([{ t: { id: "generate" } }, {}]);
    form = withLeftOut(withLeftOut(form, "t", "x", true), "t", "y", true);
    expect([form.leftOut, withLeftOut(form, "t", "x", false).leftOut]).toEqual([{ t: ["x", "y"] }, { t: ["y"] }]);
  });

  it("reads a table or column named __proto__ or constructor as a name (05b K2)", () => {
    const form = withOverride(withOverride(targeted(relation), "__proto__", "constructor", "text"), "t", "__proto__", "int");
    expect(Object.hasOwn(form.overrides, "__proto__") && Object.hasOwn(form.overrides.t, "__proto__")).toBe(true);
    expect(Object.getPrototypeOf(form.overrides)).toBe(Object.prototype);
    const named = { ...table, source_key: "__proto__", columns: [{ ...table.columns[0], source_name: "constructor" }] };
    expect(mappingRows(named, form)[0].typed).toBe("text");
    expect(mappingRows({ ...table, source_key: "constructor" }, targeted(relation))[0].typed).toBeNull();
  });

  it("marks the DELETE of the rows a copy replaces as dangerous, and says each problem once", () => {
    expect(statementLines(table).map((line) => line.danger)).toEqual([false, true, false]);
    expect(previewProblems({ problems: [{ message: "x" }], tables: [table, table] })).toEqual(["x", table.problems[0].message]);
  });
});
