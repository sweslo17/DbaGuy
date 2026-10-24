import { QueryClient } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppSettings, EngineInfo, ProfileEntry } from "@/lib/api/types";
import type { Tab } from "@/stores/workspace";
import { enginesKey, profilesKey } from "../connections/hooks";
import { useJobs } from "../data-tools/jobsStore";
import type { TableTabActions } from "../table-data/tableTabActions";
import { COMMANDS, type CommandContext } from "./commands";

/* ⌘⇧P's data-tools commands (spec ⑥ §3.6): Dump… where the engine dumps, Run SQL file… always, Show jobs once there
   are jobs; a table tab's Dump runs its own handler (it knows its filter). */

const opened = vi.hoisted(() => ({ dump: [] as unknown[], run: [] as unknown[], copy: [] as unknown[], jobs: [] as boolean[] }));
vi.mock("../data-tools/dataDialogs", () => ({
  openDumpDialog: (entry: unknown) => opened.dump.push(entry),
  openCopyDialog: (entry: unknown) => opened.copy.push(entry),
  openRunDialog: (entry: unknown) => opened.run.push(entry),
  setJobsPopoverOpen: (open: boolean) => opened.jobs.push(open),
}));
const mounted = vi.hoisted(() => ({ actions: null as TableTabActions | null }));
vi.mock("../table-data/tableTabActions", () => ({ readTableTabActions: (id: string) => (id === "table:1" ? mounted.actions : null) }));

const P = "p1";
const TABLE: Tab = { id: "table:1", kind: "table", profileId: P, path: [{ kind: "database", name: "d" }, { kind: "schema", name: "s" }, { kind: "folder", name: "tables" }, { kind: "table", name: "t" }], title: "t" };
const QUERY = { id: "query:1", kind: "query", profileId: P, database: null, title: "Q" } as unknown as Tab;

function context(
  activeTab: Tab | null,
  dump = true,
  tableActions: TableTabActions | null = null,
  copy = true,
  database: string | null = "dbaguy",
): CommandContext {
  const queryClient = new QueryClient();
  queryClient.setQueryData<ProfileEntry[]>(profilesKey, [{ profile: { id: P, engine: "postgres", name: "Local PG", database } } as ProfileEntry]);
  queryClient.setQueryData<EngineInfo[]>(enginesKey, [
    { engine: "postgres", dump, run_file: true, copy_data: copy, per_database_connection: true } as EngineInfo,
  ]);
  return { activeTab, tabActions: null, editActions: null, tableActions, settings: { loaded: true, current: {} as AppSettings, save: async (n) => n }, queryClient };
}
const command = (id: string) => COMMANDS.find((c) => c.id === id)!;

describe("the palette's data-tools commands", () => {
  beforeEach(() => {
    opened.dump = [];
    opened.run = [];
    opened.copy = [];
    opened.jobs = [];
    useJobs.setState({ jobs: {}, order: [] });
  });

  it("list Dump… only where the engine dumps, and dump a query tab's database", () => {
    expect(command("dataTools.dump").visible!(context(QUERY, false))).toBe(false);
    const ctx = context(QUERY);
    expect(command("dataTools.dump").enabled(ctx)).toBe(true);
    command("dataTools.dump").run(ctx);
    expect(opened.dump).toEqual([{ profileId: P, database: "dbaguy", target: { kind: "database", database: "dbaguy" }, filter: null }]);
  });

  it("run a table tab's own Dump table…, as it is when it runs", () => {
    const run = vi.fn();
    mounted.actions = { dumpTable: { enabled: true, run }, copyRows: { enabled: true, run: vi.fn() } };
    const ctx = context(TABLE, true, mounted.actions);
    expect(command("dataTools.dump").enabled(ctx)).toBe(true);
    command("dataTools.dump").run(ctx);
    expect([run.mock.calls.length, opened.dump]).toEqual([1, []]);
  });

  it("grey a table tab's Dump… while its own action cannot run, and run nothing then", () => {
    const run = vi.fn();
    mounted.actions = { dumpTable: { enabled: false, run }, copyRows: { enabled: false, run: vi.fn() } };
    const ctx = context(TABLE, true, mounted.actions);
    expect(command("dataTools.dump").enabled(ctx)).toBe(false);
    command("dataTools.dump").run(ctx);
    expect([run.mock.calls.length, opened.dump]).toEqual([0, []]);
  });

  it("always offer Run SQL file… on the active tab's connection", () => {
    command("dataTools.runFile").run(context(QUERY));
    // A query tab with no context of its own: undefined, the connection's default (the three states); never null (none)
    expect(opened.run).toEqual([{ profileId: P, database: null, searchPath: null, queryContext: undefined, path: "" }]);
  });

  it("enable Show jobs once there are jobs", () => {
    expect(command("dataTools.jobs").enabled(context(null))).toBe(false);
    useJobs.setState({ jobs: {}, order: ["r1"] });
    expect(command("dataTools.jobs").enabled(context(null))).toBe(true);
    command("dataTools.jobs").run(context(null));
    expect(opened.jobs).toEqual([true]);
  });

  it("list Copy data to… where the engine copies: a table tab's own Copy rows to…, a query tab's schema", () => {
    expect(command("dataTools.copy").visible!(context(QUERY, true, null, false))).toBe(false);
    const copyRows = vi.fn();
    mounted.actions = { dumpTable: { enabled: true, run: vi.fn() }, copyRows: { enabled: true, run: copyRows } };
    command("dataTools.copy").run(context(TABLE, true, mounted.actions));
    expect([copyRows.mock.calls.length, opened.copy]).toEqual([1, []]);
    command("dataTools.copy").run(context(QUERY));
    expect(opened.copy).toEqual([{
      profileId: P,
      database: "dbaguy",
      source: { kind: "tables", schema: "public", folder: [{ kind: "database", name: "dbaguy" }, { kind: "schema", name: "public" }, { kind: "folder", name: "tables" }] },
    }]);
  });

  it("grey Copy data to… on a query tab whose connection has no database, as Dump… is, and open nothing", () => {
    const ctx = context(QUERY, true, null, true, null);
    expect([command("dataTools.dump").enabled(ctx), command("dataTools.copy").enabled(ctx)]).toEqual([false, false]);
    command("dataTools.copy").run(ctx);
    expect(opened.copy).toEqual([]);
    // with a database it is on
    expect(command("dataTools.copy").enabled(context(QUERY))).toBe(true);
  });

  it("grey a table tab's Copy data to… while its own action cannot run, and run nothing then", () => {
    const run = vi.fn();
    mounted.actions = { dumpTable: { enabled: true, run: vi.fn() }, copyRows: { enabled: false, run } };
    const ctx = context(TABLE, true, mounted.actions);
    expect(command("dataTools.copy").enabled(ctx)).toBe(false);
    command("dataTools.copy").run(ctx);
    expect([run.mock.calls.length, opened.copy]).toEqual([0, []]);
  });
});
