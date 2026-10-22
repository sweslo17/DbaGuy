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
  kindCount,
  MAX_DUMP_TABLES,
  planLineText,
  planSummary,
  planTables,
  planWarnings,
  tablesProblem,
} from "./dumpForm";
import type { DumpEntry, RunEntry } from "./entryPoints";
import {
  againEntry,
  databaseNodes,
  EVERY_STATEMENT_RUNS,
  followEngineMode,
  numberedLines,
  initialRunForm,
  inspectionNotes,
  patchedForm,
  pickMode,
  pickOnError,
  presetPicksMode,
  queryContextLabel,
  queryContextOf,
  RED_CONFIRM,
  retargeted,
  runChoices,
  runCommand,
  runGate,
  runTitle,
  settledMode,
  withDatabase,
  withPath,
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

  it("says what is wrong with the checked tables beside them, and asks no plan for more than 500", () => {
    const form = initialDumpForm(folder, "", ["a"]);
    const many = Array.from({ length: MAX_DUMP_TABLES + 1 }, (_, i) => `t${i}`);
    const exactly = many.slice(0, MAX_DUMP_TABLES);
    // only the tables' own problem: the path's is the Dump button's
    expect([tablesProblem(folder, form), tablesProblem(folder, { ...form, checked: [] }), tablesProblem(pg, initialDumpForm(pg, ""))]).toEqual([
      null,
      "Check at least one table",
      null,
    ]);
    expect(tablesProblem(folder, { ...form, checked: many })).toBe("A dump of chosen tables names at most 500: dump the schema instead");
    expect(dumpPlanRequest(folder, { ...form, checked: many })).toBeNull();
    expect(dumpPlanRequest(folder, { ...form, checked: exactly })?.scope.names).toHaveLength(MAX_DUMP_TABLES);
  });

  it("makes no plan request for an empty Tables folder, and no command for an incomplete form", () => {
    const empty = initialDumpForm(folder, "/tmp/s.sql");
    expect(dumpPlanRequest(folder, empty)).toBeNull();
    expect(dumpPlanRequest(folder, { ...empty, checked: ["a"] })?.scope).toEqual({ kind: "tables", schema_name: "shop", names: ["a"] });
    expect(() => dumpCommand(folder, empty, "r")).toThrow("The dump's form is not complete");
    expect(() => dumpCommand(pg, initialDumpForm(pg, ""), "r")).toThrow("The dump's form is not complete");
    expect(() => dumpCommand(pg, initialDumpForm(pg, "a.sql"), "r")).toThrow("The dump's form is not complete");
    expect(dumpCommand(pg, initialDumpForm(pg, " /tmp/a.sql "), "r").path).toBe("/tmp/a.sql");
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
const entry: RunEntry = { profileId: "p1", database: null, searchPath: "sales", path: "/tmp/x.sql" };
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

  it("says what is missing before Run can be pressed: a connection, the engine's details, a file, an absolute path", () => {
    const form = initialRunForm(entry, engine("postgres"));
    expect(runGate(null, engine("postgres"), form, true).blocked).toBe("Choose a connection");
    // the engines have not loaded yet: that is waiting, not "cannot run files"
    expect(runGate(profile(), null, form, true).blocked).toBe("Reading the connection's details…");
    expect(runGate(profile(), engine("postgres", { run_file: false }), form, true).blocked).toBe("This connection cannot run SQL files");
    expect(runGate(profile(), engine("postgres"), { ...form, path: "  " }, true).blocked).toBe("Choose a file");
    expect(runGate(profile(), engine("postgres"), { ...form, path: "x.sql" }, true).blocked).toBe("Enter an absolute path (starting with /)");
    expect(runGate(profile(), engine("postgres"), { ...form, fromStatement: "1.5" }, true).blocked).toBe("A statement number, 1 or more");
    // the red box comes last: only a form that could run is asked to be confirmed
    expect(runGate(profile({ color: "red" }), engine("postgres"), { ...form, path: "" }, true).blocked).toBe("Choose a file");
  });

  it("asks for the box again after a run-again action", () => {
    const form = { ...initialRunForm(entry, engine("postgres")), confirmed: true };
    expect(patchedForm(form, { mode: "statements", on_error: "stop", from_statement: 7 })).toMatchObject({ mode: "statements", fromStatement: "7", confirmed: false });
    // "run again as it was": nothing else changes, and the box is still asked again
    expect(patchedForm(form, null)).toEqual({ ...form, confirmed: false });
  });

  it("keeps a run-again's mode: only an unchosen mode follows the engine as it loads (06b L7)", () => {
    const fresh = initialRunForm(entry, null);
    expect([presetPicksMode(entry), fresh.modePicked, fresh.mode]).toEqual([false, false, "statements"]);
    // the engine's info arrives: all or nothing on PostgreSQL, still statement by statement on MySQL
    expect(followEngineMode(fresh, engine("postgres")).mode).toBe("all_or_nothing");
    expect(followEngineMode(fresh, engine("mysql"))).toBe(fresh);
    expect(followEngineMode(initialRunForm(entry, engine("postgres")), engine("couchbase")).mode).toBe("statements");
    // a run again from line 7 (statements) stays so on PostgreSQL, however late the engine's info comes
    const again: RunEntry = { ...entry, preset: { mode: "statements", on_error: "stop", from_statement: 7 } };
    const form = initialRunForm(again, null);
    expect([presetPicksMode(again), form.modePicked]).toEqual([true, true]);
    expect(followEngineMode(form, engine("postgres"))).toBe(form);
    // a preset that sets only the other fields does not pick the mode
    expect(presetPicksMode({ ...entry, preset: { from_statement: 3 } })).toBe(false);
  });

  it("never lets the engine undo a mode the user or a run-again patch chose", () => {
    const fresh = initialRunForm(entry, engine("postgres"));
    // the user picks statement by statement on PostgreSQL, then the target changes to another PostgreSQL connection
    const picked = pickMode(fresh, "statements");
    expect(picked).toMatchObject({ mode: "statements", modePicked: true });
    expect(followEngineMode(picked, engine("postgres"))).toBe(picked);
    // a patch that sets a mode picks it, even on a form that had followed the engine
    const patched = patchedForm(fresh, { mode: "statements", on_error: "continue", from_statement: 7 });
    expect(patched).toMatchObject({ mode: "statements", modePicked: true, onError: "continue", fromStatement: "7" });
    expect(followEngineMode(patched, engine("postgres"))).toBe(patched);
    // a patch back to all or nothing is picked as well
    expect(followEngineMode(patchedForm(initialRunForm(entry, engine("mysql")), { mode: "all_or_nothing", on_error: "stop", from_statement: 1 }), engine("mysql")).mode).toBe("all_or_nothing");
    // an all-or-nothing run stops at the first error: picking it drops "skip errors"
    expect(pickMode({ ...picked, onError: "continue" }, "all_or_nothing")).toMatchObject({ mode: "all_or_nothing", onError: "stop" });
    expect(pickMode({ ...picked, onError: "continue" }, "statements").onError).toBe("continue");
  });

  it("sends the entry's query context only while the run is on the entry's own connection (06b L6)", () => {
    const bucket: RunEntry = { ...entry, queryContext: { bucket: "b", scope: "s" } };
    const form = initialRunForm(bucket, engine("couchbase"));
    expect(queryContextOf(bucket, form)).toEqual({ bucket: "b", scope: "s" });
    // another connection has its own default: undefined, never the entry's and never "none"
    expect(queryContextOf(bucket, { ...form, profileId: "p2" })).toBeUndefined();
    expect(queryContextOf(bucket, { ...form, profileId: null })).toBeUndefined();
    const none: RunEntry = { ...entry, queryContext: null };
    expect(queryContextOf(none, { ...initialRunForm(none, engine("couchbase")), profileId: "p2" })).toBeUndefined();
    expect(queryContextOf(none, initialRunForm(none, engine("couchbase")))).toBeNull();
    expect(queryContextOf(entry, initialRunForm(entry, engine("couchbase")))).toBeUndefined();
  });

  it("says a query context in words, the three states apart", () => {
    expect(queryContextLabel(undefined)).toBe("the connection's default");
    expect(queryContextLabel(null)).toBe("none: names must be qualified");
    expect(queryContextLabel({ bucket: "b" })).toBe("bucket b");
    expect(queryContextLabel({ bucket: "b", scope: "s" })).toBe("bucket b · scope s");
    expect(queryContextLabel({ bucket: "b", scope: null })).toBe("bucket b");
  });

  it("asks the box again on another connection: red A checked, then green, then red B (CR-3)", () => {
    const onA: RunEntry = { ...entry, profileId: "A", database: "dbA", searchPath: "sales" };
    const checkedOnA = { ...initialRunForm(onA, engine("postgres")), confirmed: true };
    const green = retargeted(checkedOnA, "green");
    expect(green).toMatchObject({ profileId: "green", confirmed: false, database: null, searchPath: "" });
    const redB = retargeted({ ...green, confirmed: true }, "B");
    expect(redB.confirmed).toBe(false);
    // red A, checked, to green and straight back: still asked (the box was for the file and the connection as they were)
    expect(retargeted(green, "A").confirmed).toBe(false);
    // the same connection again: nothing changes, the box stays checked
    expect(retargeted(checkedOnA, "A")).toBe(checkedOnA);
    // what the user typed for the run is kept: the file, the statement to start at and the mode
    const typed = { ...checkedOnA, path: "/tmp/y.sql", fromStatement: "9", modePicked: true };
    expect(retargeted(typed, "B")).toMatchObject({ path: "/tmp/y.sql", fromStatement: "9", modePicked: true });
    // back on the entry's own connection, the node's database and search path come back
    expect(retargeted(green, "A", onA)).toMatchObject({ profileId: "A", database: "dbA", searchPath: "sales", confirmed: false });
    expect(retargeted(green, "B", onA)).toMatchObject({ profileId: "B", database: null, searchPath: "" });
  });

  it("asks the box again in another database of the same red connection, or for another file (CR-3)", () => {
    const checked = { ...initialRunForm({ ...entry, database: "dbA" }, engine("postgres")), confirmed: true };
    expect(withDatabase(checked, "dbB")).toMatchObject({ database: "dbB", confirmed: false });
    expect(withDatabase(checked, null)).toMatchObject({ database: null, confirmed: false });
    // the same database again: nothing changes
    expect(withDatabase(checked, "dbA")).toBe(checked);
    // another file: the box was for the file too
    expect(withPath(checked, "/tmp/other.sql")).toMatchObject({ path: "/tmp/other.sql", confirmed: false });
    expect(withPath(checked, checked.path)).toBe(checked);
  });

  it("moves a greyed mode to statement by statement, and follows the engine while unchosen (06b T4 re-review)", () => {
    // picked all or nothing on PostgreSQL, then the target is a Couchbase connection: no transactions
    const picked = pickMode(initialRunForm(entry, engine("postgres")), "all_or_nothing");
    expect(settledMode(picked, engine("couchbase"), null)).toMatchObject({ mode: "statements", modePicked: true });
    // a DbaGuy dump cut short: all or nothing refuses it, even where it is the engine's default; settled once, it stays
    const cut = inspection(["The file does not end with its 'Dump complete' line: it was cut short"], true);
    const fresh = initialRunForm(entry, engine("postgres"));
    const settled = settledMode(fresh, engine("postgres"), cut);
    expect(settled.mode).toBe("statements");
    expect(settledMode(settled, engine("postgres"), cut)).toBe(settled);
    // an unchosen mode follows the engine as it loads; nothing to change gives the same form
    const loading = initialRunForm(entry, null);
    expect(settledMode(loading, engine("postgres"), null).mode).toBe("all_or_nothing");
    expect(settledMode(fresh, engine("postgres"), null)).toBe(fresh);
  });

  it("picks statement by statement with Skip it and continue: the engine never turns it into a stop", () => {
    const loading = initialRunForm(entry, null);
    const skipping = pickOnError(loading, "continue");
    expect(skipping).toMatchObject({ onError: "continue", modePicked: true, mode: "statements" });
    expect(followEngineMode(skipping, engine("postgres"))).toBe(skipping);
    // stopping at the error is either mode's: it picks nothing
    expect(pickOnError(loading, "stop").modePicked).toBe(false);
  });

  it("sends a run again's line while the form starts there in the same file, and never otherwise (06b review M3)", () => {
    const pg = engine("postgres");
    const again: RunEntry = { ...entry, preset: { mode: "statements", on_error: "stop", from_statement: 4, from_line: 3 } };
    const form = initialRunForm(again, pg);
    expect(form.fromLine).toEqual({ statement: 4, line: 3, path: "/tmp/x.sql" });
    expect(runCommand(form, pg, null, "r")).toMatchObject({ from_statement: 4, from_line: 3 });
    // another statement typed, or another file: no line to check
    expect(runCommand({ ...form, fromStatement: "5" }, pg, null, "r")).not.toHaveProperty("from_line");
    expect(runCommand({ ...form, fromStatement: " 4 " }, pg, null, "r").from_line).toBe(3);
    expect(runCommand({ ...form, path: "/tmp/y.sql" }, pg, null, "r")).not.toHaveProperty("from_line");
    // another connection keeps it: the file is the same
    expect(runCommand(retargeted(form, "p2"), pg, null, "r").from_line).toBe(3);
    // a fresh form, or a patch without a line, sends none
    expect(runCommand(initialRunForm(entry, pg), pg, null, "r")).not.toHaveProperty("from_line");
    expect(patchedForm(form, { mode: "statements", on_error: "stop", from_statement: 5 }).fromLine).toBeNull();
    expect(patchedForm(form, { mode: "statements", on_error: "stop", from_statement: 5, from_line: 9 }).fromLine).toEqual({ statement: 5, line: 9, path: "/tmp/x.sql" });
    // "run again as it was" of a run again keeps its line; of a plain run, none
    const sent = runCommand(form, pg, null, "r");
    expect(againEntry({ profileId: "p1", database: null, command: sent }, null).preset).toEqual({ mode: "statements", on_error: "stop", from_statement: 4, from_line: 3 });
  });

  it("lists the target's databases where the engine has them: MySQL's are its schemas", () => {
    expect([databaseNodes(engine("postgres")), databaseNodes(engine("mssql")), databaseNodes(engine("mysql")), databaseNodes(engine("sqlite")), databaseNodes(engine("couchbase")), databaseNodes(null)])
      .toEqual(["database", "database", "schema", null, null, null]);
  });

  it("sends the query context in its three states, Couchbase only", () => {
    const form = initialRunForm(entry, engine("couchbase"));
    const sent = (context: Parameters<typeof runCommand>[2], eng = engine("couchbase")) => runCommand(form, eng, context, "r");
    // undefined: the connection's default, left to the job runner (it knows the session's own): not on the wire here
    expect(sent(undefined).query_context).toBeUndefined();
    expect(JSON.stringify(sent(undefined))).not.toContain("query_context");
    // null: no context, sent as such
    expect(sent(null).query_context).toBeNull();
    expect(JSON.stringify(sent(null))).toContain('"query_context":null');
    expect(sent({ bucket: "b", scope: "s" }).query_context).toEqual({ bucket: "b", scope: "s" });
    // another engine sends none, whatever it was handed
    expect(sent(undefined, engine("postgres")).query_context).toBeNull();
    expect(sent({ bucket: "b" }, engine("postgres")).query_context).toBeNull();
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

  it("says the plan's lines in English, the rows only when known", () => {
    expect(planLineText({ kind: "table", count: 12, rows: 1_234_567 })).toBe("12 tables · ≈ 1,234,567 rows");
    expect(planLineText({ kind: "table", count: 1, rows: 0 })).toBe("1 table · ≈ 0 rows");
    expect(planLineText({ kind: "view", count: 2, rows: null })).toBe("2 views");
    expect(["policy", "security policy", "statistics", "refresh", "materialized view", "alias type"].map((kind) => kindCount(2, kind)))
      .toEqual(["2 policies", "2 security policies", "2 statistics", "2 refreshes", "2 materialized views", "2 alias types"]);
    expect(kindCount(1, "policy")).toBe("1 policy");
  });

  it("puts a filtered dump's warning first, in red, and keeps the rest in order", () => {
    const filtered = "The file holds only the rows of public.t that match a filter: restoring it drops the table and replaces it with those rows";
    expect(planWarnings(["Disk almost full", filtered, "Not a consistent snapshot"])).toEqual({
      prominent: [filtered],
      other: ["Disk almost full", "Not a consistent snapshot"],
    });
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

  it("runs again in the same Couchbase query context: the default stays the default, none stays none", () => {
    const cb = engine("couchbase");
    const form = initialRunForm({ ...entry, searchPath: null }, cb);
    const again = (context: Parameters<typeof runCommand>[2]) =>
      againEntry({ profileId: "p1", database: null, command: runCommand(form, cb, context, "r") }, null).queryContext;
    expect(again(undefined)).toBeUndefined();
    expect(again(null)).toBeNull();
    expect(again({ bucket: "b", scope: "s" })).toEqual({ bucket: "b", scope: "s" });
  });

  it("numbers a file's lines from where they start, without their CR", () => {
    expect(numberedLines("a\r\nb\n", 30)).toEqual([{ number: 30, text: "a" }, { number: 31, text: "b" }]);
    expect(numberedLines("", 1)).toEqual([]);
  });
});
