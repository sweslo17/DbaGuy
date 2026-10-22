import { describe, expect, it } from "vitest";
import type { EngineInfo, FileInspection, Profile } from "@/lib/api/types";
import {
  dumpChoices,
  dumpCommand,
  dumpDialogTitle,
  dumpFormFromCommand,
  dumpPlanRequest,
  dumpProblem,
  dumpScope,
  dumpTitle,
  initialDumpForm,
  MAX_DUMP_TABLES,
  planSummary,
  planTables,
} from "./dumpForm";
import type { DumpEntry, RunEntry } from "./entryPoints";
import {
  againEntry,
  EVERY_STATEMENT_RUNS,
  followEngineMode,
  numberedLines,
  initialRunForm,
  inspectionNotes,
  patchedForm,
  presetPicksMode,
  queryContextOf,
  RED_CONFIRM,
  runChoices,
  runCommand,
  runGate,
  runTitle,
} from "./runForm";

/* The two dialogs' forms (spec ⑥ §3.6): what is offered, greyed and why, and what is sent. */

const pg: DumpEntry = { profileId: "p1", database: "dbaguy", target: { kind: "schema", schema: "public" }, filter: null };
const table: DumpEntry = { profileId: "p1", database: "dbaguy", target: { kind: "relation", schema: "public", name: "orders", relationKind: "table" }, filter: "status = 'open'" };
const view: DumpEntry = { profileId: "p1", database: "dbaguy", target: { kind: "relation", schema: "public", name: "v", relationKind: "view" }, filter: null };
const folder: DumpEntry = { profileId: "p1", database: null, target: { kind: "tables", schema: "shop", folder: [] }, filter: null };

describe("the dump form", () => {
  it("offers the engine's object kinds for a schema, and only triggers for a table", () => {
    const form = initialDumpForm(pg, "/tmp/a.sql");
    expect(dumpChoices(pg, "postgres", form).kinds.map((k) => k.value)).toEqual(["views", "sequences", "types", "routines", "triggers"]);
    expect(dumpChoices(pg, "mysql", form).kinds.map((k) => k.label)).toEqual(["Views", "Routines and events", "Triggers"]);
    expect(dumpChoices(pg, "sqlite", form).kinds.map((k) => k.value)).toEqual(["views", "triggers"]);
    expect(dumpChoices(table, "postgres", initialDumpForm(table, "")).kinds.map((k) => k.value)).toEqual(["triggers"]);
    expect(dumpChoices(view, "postgres", initialDumpForm(view, "")).kinds).toEqual([]);
  });

  it("dumps a view's definition only, and greys the rest with why", () => {
    const form = initialDumpForm(view, "/tmp/v.sql");
    expect(form.content).toBe("schema");
    expect(dumpChoices(view, "postgres", form).contents.map((c) => [c.value, c.disabled])).toEqual([
      ["both", "A view has no rows of its own: its definition is dumped"],
      ["schema", null],
      ["data", "A view has no rows of its own: its definition is dumped"],
    ]);
  });

  it("sends the table tab's filter only while it is checked and rows are written, to the plan and the dump alike", () => {
    const form = initialDumpForm(table, "/tmp/o.sql");
    expect([form.onlyFiltered, dumpPlanRequest(table, form)?.filter, dumpCommand(table, form, "r").filter]).toEqual([true, "status = 'open'", "status = 'open'"]);
    const unchecked = { ...form, onlyFiltered: false };
    expect(dumpCommand(table, unchecked, "r").filter).toBeNull();
    const schemaOnly = { ...form, content: "schema" as const };
    expect([dumpChoices(table, "postgres", schemaOnly).filter, dumpCommand(table, schemaOnly, "r").filter]).toEqual([
      { shown: true, disabled: "A schema-only dump writes no rows" },
      null,
    ]);
  });

  it("drops nothing in a data-only dump, and says so", () => {
    const form = { ...initialDumpForm(pg, "/tmp/a.sql"), dropFirst: true, content: "data" as const };
    expect([dumpChoices(pg, "postgres", form).dropFirst, dumpCommand(pg, form, "r").drop_first, dumpPlanRequest(pg, form)?.drop_first]).toEqual([
      "A data-only dump creates no objects, so it drops none",
      false,
      false,
    ]);
  });

  it("dumps the Tables folder's checked tables, at least one and at most 500", () => {
    const form = initialDumpForm(folder, "/tmp/s.sql", ["a", "b"]);
    expect(dumpScope(folder, form)).toEqual({ kind: "tables", schema_name: "shop", names: ["a", "b"] });
    expect(dumpProblem(folder, { ...form, checked: [] })).toBe("Check at least one table");
    expect(dumpScope(folder, { ...form, checked: [] })).toBeNull();
    const many = Array.from({ length: MAX_DUMP_TABLES + 1 }, (_, i) => `t${i}`);
    expect(dumpProblem(folder, { ...form, checked: many })).toBe("A dump of chosen tables names at most 500: dump the schema instead");
  });

  it("needs an absolute file, and sends a path to the plan only once it is one", () => {
    expect(dumpProblem(pg, initialDumpForm(pg, ""))).toBe("Choose a file to dump into");
    expect(dumpProblem(pg, initialDumpForm(pg, "a.sql"))).toBe("Enter an absolute path (starting with /)");
    expect(dumpPlanRequest(pg, initialDumpForm(pg, "a.sql"))?.path).toBeNull();
    expect(dumpPlanRequest(pg, initialDumpForm(pg, " /tmp/a.sql "))).toEqual({
      profile_id: "p1", database: "dbaguy", scope: { kind: "schema", schema_name: "public", names: [] }, content: "both",
      objects: { views: true, sequences: true, types: true, routines: true, triggers: true }, path: "/tmp/a.sql", drop_first: false, filter: null,
    });
  });

  it("titles the dialog by what it dumps", () => {
    expect(dumpDialogTitle(pg)).toBe("Dump public to a SQL file");
    expect(dumpDialogTitle(table)).toBe("Dump public.orders to a SQL file");
    expect(dumpDialogTitle(folder)).toBe("Dump tables of shop to a SQL file");
    expect(dumpDialogTitle({ ...pg, target: { kind: "database", database: "dbaguy" } })).toBe("Dump the database dbaguy to a SQL file");
    expect(dumpDialogTitle({ ...pg, target: { kind: "relation", schema: "main", name: "t", relationKind: "table" } })).toBe("Dump t to a SQL file");
  });

  it("titles the job as the backend does", () => {
    expect(dumpTitle({ kind: "schema", schema_name: "public", names: [] }, "Local PG", "/tmp/a.sql")).toBe("Dump public of Local PG to a.sql");
    expect(dumpTitle({ kind: "tables", schema_name: "s", names: ["a", "b"] }, "X", "/b.sql")).toBe("Dump 2 tables of X to b.sql");
    expect(dumpTitle({ kind: "database", names: [] }, "X", "/b.sql")).toBe("Dump the database of X to b.sql");
  });
});

const engine = (name: EngineInfo["engine"], fields: Partial<EngineInfo> = {}) =>
  ({ engine: name, transactions: name !== "couchbase", query_context: name === "couchbase", run_file: true, ...fields }) as EngineInfo;
const entry: RunEntry = { profileId: "p1", database: null, searchPath: "sales", queryContext: null, path: "/tmp/x.sql" };
const profile = (fields: Partial<Profile> = {}) => ({ id: "p1", name: "Local PG", color: "none", read_only: false, ...fields }) as Profile;
const inspection = (warnings: string[], header = false): FileInspection =>
  ({ path: "/tmp/x.sql", size: 10, preview: "", preview_truncated: false, warnings, header: header ? ({ format: 1, engine: "postgres", lines: [] } as never) : null }) as FileInspection;

describe("the run form", () => {
  it("starts all or nothing where the engine has transactions, statement by statement on MySQL and Couchbase", () => {
    expect(initialRunForm(entry, engine("postgres")).mode).toBe("all_or_nothing");
    expect(initialRunForm(entry, engine("mysql")).mode).toBe("statements");
    expect(initialRunForm(entry, engine("couchbase")).mode).toBe("statements");
    expect(initialRunForm({ ...entry, preset: { mode: "statements", from_statement: 40 } }, engine("postgres"))).toMatchObject({ mode: "statements", fromStatement: "40" });
  });

  it("greys what does not apply, and says why", () => {
    const form = initialRunForm(entry, engine("couchbase"));
    expect(runChoices(engine("couchbase"), null, form).modes[0].disabled).toBe("This connection has no transactions: each statement takes effect on its own");
    const pgForm = initialRunForm(entry, engine("postgres"));
    expect(runChoices(engine("postgres"), null, pgForm).skip).toBe("All or nothing stops at the first error");
    expect(runChoices(engine("mysql"), null, pgForm).modes[0].note).toContain("MySQL commits around DDL");
    const cut = inspection(["The file does not end with its 'Dump complete' line: it was cut short"], true);
    expect(runChoices(engine("postgres"), cut, pgForm).modes[0].disabled).toBe("The dump is cut short: all or nothing refuses it");
    expect(runChoices(engine("postgres"), null, pgForm)).toMatchObject({ searchPath: true, databases: "database" });
    expect(runChoices(engine("mysql"), null, pgForm)).toMatchObject({ searchPath: false, databases: "schema" });
  });

  it("puts the inspection's warnings that change what a run does first (06a F2)", () => {
    const notes = inspectionNotes(inspection([
      "The file is empty",
      "The file holds only the rows of public.orders that match a filter: restoring it drops the table and replaces it with those rows",
      "This dump drops and recreates its objects",
      "Written for PostgreSQL, the target is MySQL",
    ]));
    expect(notes.prominent).toHaveLength(3);
    expect(notes.other).toEqual(["The file is empty"]);
  });

  it("asks a red connection for the box and a danger button (CR-3); a read-only one cannot run", () => {
    const form = initialRunForm(entry, engine("postgres"));
    expect(runGate(profile(), engine("postgres"), form, true)).toEqual({ red: false, blocked: null });
    expect(runGate(profile({ color: "red" }), engine("postgres"), form, true)).toEqual({ red: true, blocked: `Check “${RED_CONFIRM}” first` });
    expect(runGate(profile({ color: "red" }), engine("postgres"), { ...form, confirmed: true }, true).blocked).toBeNull();
    expect(runGate(profile({ read_only: true }), engine("postgres"), form, true).blocked).toBe("This connection is read-only: it cannot run files");
    expect(runGate(profile(), engine("postgres"), form, false).blocked).toBe("The file has not been read yet");
    expect(runGate(profile(), engine("postgres"), { ...form, fromStatement: "0" }, true).blocked).toBe("A statement number, 1 or more");
    expect(EVERY_STATEMENT_RUNS).toContain("DROP and TRUNCATE");
  });

  it("asks for the box again after a run-again action", () => {
    const form = { ...initialRunForm(entry, engine("postgres")), confirmed: true };
    expect(patchedForm(form, { mode: "statements", on_error: "stop", from_statement: 7 })).toMatchObject({ mode: "statements", fromStatement: "7", confirmed: false });
    // "run again as it was": nothing else changes, and the box is still asked again
    expect(patchedForm(form, null)).toEqual({ ...form, confirmed: false });
  });

  it("keeps a run-again's mode: only an unchosen mode follows the engine as it loads (06b L7)", () => {
    const fresh = initialRunForm(entry, null);
    expect([presetPicksMode(entry), fresh.mode]).toEqual([false, "statements"]);
    // the engine's info arrives: all or nothing on PostgreSQL, still statement by statement on MySQL
    expect(followEngineMode(fresh, engine("postgres"), presetPicksMode(entry)).mode).toBe("all_or_nothing");
    expect(followEngineMode(fresh, engine("mysql"), false)).toBe(fresh);
    expect(followEngineMode(initialRunForm(entry, engine("postgres")), engine("couchbase"), false).mode).toBe("statements");
    // a run again from line 7 (statements) stays so on PostgreSQL, however late the engine's info comes
    const again: RunEntry = { ...entry, preset: { mode: "statements", on_error: "stop", from_statement: 7 } };
    const form = initialRunForm(again, null);
    expect(presetPicksMode(again)).toBe(true);
    expect(followEngineMode(form, engine("postgres"), presetPicksMode(again))).toBe(form);
    // a mode the user chose is never moved
    expect(followEngineMode({ ...fresh, mode: "statements" }, engine("postgres"), true).mode).toBe("statements");
    // a preset that sets only the other fields does not pick the mode
    expect(presetPicksMode({ ...entry, preset: { from_statement: 3 } })).toBe(false);
  });

  it("sends the entry's query context only while the run is on the entry's own connection (06b L6)", () => {
    const bucket: RunEntry = { ...entry, queryContext: { bucket: "b", scope: "s" } };
    const form = initialRunForm(bucket, engine("couchbase"));
    expect(queryContextOf(bucket, form)).toEqual({ bucket: "b", scope: "s" });
    expect(queryContextOf(bucket, { ...form, profileId: "p2" })).toBeNull();
    expect(queryContextOf(bucket, { ...form, profileId: null })).toBeNull();
    expect(runCommand({ ...form, profileId: "p2" }, engine("couchbase"), queryContextOf(bucket, { ...form, profileId: "p2" }), "r").query_context).toBeNull();
  });

  it("sends the search path to PostgreSQL only, and the context to Couchbase only", () => {
    const form = { ...initialRunForm(entry, engine("postgres")), mode: "statements" as const, onError: "continue" as const, fromStatement: " 12 " };
    expect(runCommand(form, engine("postgres"), null, "r")).toEqual({
      type: "run_file", request_id: "r", path: "/tmp/x.sql", mode: "statements", on_error: "continue", from_statement: 12, search_path: "sales", query_context: null,
    });
    expect(runCommand(form, engine("mysql"), { bucket: "b" }, "r")).toMatchObject({ search_path: null, query_context: null });
    expect(runCommand(form, engine("couchbase"), { bucket: "b" }, "r").query_context).toEqual({ bucket: "b" });
    expect(runCommand({ ...form, mode: "all_or_nothing" }, engine("postgres"), null, "r").on_error).toBe("stop");
    expect(runTitle("/tmp/x.sql", "Local PG")).toBe("Run x.sql on Local PG");
  });
});

describe("the forms' helpers", () => {
  it("sums the plan by kind in the file's order, with the tables' rows", () => {
    expect(planSummary([
      { kind: "schema", name: "public", notes: [] },
      { kind: "table", name: "public.a", rows_estimate: 10, notes: [] },
      { kind: "table", name: "public.b", rows_estimate: null, notes: [] },
      { kind: "view", name: "public.v", notes: [] },
      { kind: "table", name: "public.c", rows_estimate: 5, notes: [] },
    ])).toEqual([
      { kind: "schema", count: 1, rows: null },
      { kind: "table", count: 3, rows: 15 },
      { kind: "view", count: 1, rows: null },
    ]);
    expect(planTables([{ kind: "table", name: "a", notes: [] }, { kind: "view", name: "v", notes: [] }])).toEqual(["a"]);
  });

  it("dumps again with the job's own choices", () => {
    const command = dumpCommand(table, { ...initialDumpForm(table, "/tmp/o.sql"), dropFirst: true }, "r");
    expect(dumpFormFromCommand(table, command)).toEqual({
      content: "both", objects: { views: true, sequences: true, types: true, routines: true, triggers: true },
      dropFirst: true, onlyFiltered: true, checked: null, path: "/tmp/o.sql",
    });
  });

  it("runs again on the job's target, from where the action says", () => {
    const command = runCommand({ ...initialRunForm(entry, engine("postgres")), mode: "statements" }, engine("postgres"), null, "r");
    const job = { profileId: "p1", database: "dbaguy", command };
    expect(againEntry(job, { mode: "statements", on_error: "stop", from_statement: 9 })).toEqual({
      profileId: "p1", database: "dbaguy", searchPath: "sales", queryContext: null, path: "/tmp/x.sql",
      preset: { mode: "statements", on_error: "stop", from_statement: 9 },
    });
    expect(againEntry(job, null).preset).toEqual({ mode: "statements", on_error: "stop", from_statement: 1 });
  });

  it("numbers a file's lines from where they start, without their CR", () => {
    expect(numberedLines("a\r\nb\n", 30)).toEqual([{ number: 30, text: "a" }, { number: 31, text: "b" }]);
    expect(numberedLines("", 1)).toEqual([]);
  });
});
