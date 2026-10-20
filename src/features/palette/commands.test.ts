import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi, type Mock } from "vitest";
import type { AppSettings, EngineInfo, ProfileEntry } from "@/lib/api/types";
import type { Tab } from "@/stores/workspace";
import { enginesKey, profilesKey } from "../connections/hooks";
import { tableEditActions, type TableEditActionId, type TableEditActions } from "../edits/editActions";
import { COMMANDS, SHORTCUTS, type CommandContext } from "./commands";

/*
 * ⌘⇧P's edit commands (spec ⑤ §3.6.4): listed on a table tab of an engine that edits rows only, each enabled as the
 * tab's own control is, and run through the tab's own handler, asked again when it runs (H11).
 */

/** The edit actions of the table tab mounted now, as the Data view would have registered them (null: none mounted). */
const mounted = vi.hoisted(() => ({ actions: null as TableEditActions | null }));
vi.mock("../edits/editActions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../edits/editActions")>()),
  readTableEditActions: (tabId: string) => (tabId === "table:1" ? mounted.actions : null),
}));

const PROFILE = "p1";
const TABLE_TAB: Tab = { id: "table:1", kind: "table", profileId: PROFILE, path: [], title: "items" };
const QUERY_TAB = { id: "query:1", kind: "query", profileId: PROFILE, title: "Query" } as unknown as Tab;

const EDIT_COMMANDS: [TableEditActionId, string, string | undefined][] = [
  ["addRow", "Add row", undefined],
  ["deleteRows", "Delete selected rows", SHORTCUTS.deleteRows],
  ["setNull", "Set NULL", SHORTCUTS.setNull],
  ["commit", "Commit changes…", SHORTCUTS.commitEdits],
  ["discardAll", "Discard all changes", undefined],
  ["undo", "Undo staged change", SHORTCUTS.undoEdit],
  ["redo", "Redo staged change", SHORTCUTS.redoEdit],
];
const IDS = EDIT_COMMANDS.map(([id]) => id);

function queryClient(rowEditing: boolean): QueryClient {
  const client = new QueryClient();
  client.setQueryData<ProfileEntry[]>(profilesKey, [{ profile: { id: PROFILE, engine: "postgres" } } as ProfileEntry]);
  client.setQueryData<EngineInfo[]>(enginesKey, [{ engine: "postgres", row_editing: rowEditing } as EngineInfo]);
  return client;
}

/** A mock handler per action. */
const handlers = () => {
  const run = {} as Record<TableEditActionId, Mock<() => void>>;
  for (const id of IDS) run[id] = vi.fn();
  return run;
};
const editable = { editable: true, changes: 2, commitBlocked: null, applying: false, undoable: true, redoable: false };

function context(activeTab: Tab | null, editActions: CommandContext["editActions"], rowEditing = true): CommandContext {
  return {
    activeTab,
    tabActions: null,
    editActions,
    settings: { loaded: true, current: {} as AppSettings, save: async (next) => next },
    queryClient: queryClient(rowEditing),
  };
}

const editCommandsOf = (ctx: CommandContext) =>
  COMMANDS.filter((command) => command.id.startsWith("edits.") && (command.visible?.(ctx) ?? true));

describe("the palette's edit commands", () => {
  it("are listed, in order and with their keys, on a table tab that edits rows", () => {
    const ctx = context(TABLE_TAB, tableEditActions(editable, handlers()));
    expect(editCommandsOf(ctx).map((command) => [command.id, command.label, command.shortcut])).toEqual(
      EDIT_COMMANDS.map(([id, label, shortcut]) => [`edits.${id}`, label, shortcut]),
    );
  });

  it("are not listed on a query tab, with no tab, or on an engine without row editing", () => {
    expect(editCommandsOf(context(QUERY_TAB, null))).toEqual([]);
    expect(editCommandsOf(context(null, null))).toEqual([]);
    expect(editCommandsOf(context(TABLE_TAB, tableEditActions(editable, handlers()), false))).toEqual([]);
  });

  it("are enabled as the tab's own controls are", () => {
    const ctx = context(TABLE_TAB, tableEditActions(editable, handlers()));
    expect(Object.fromEntries(editCommandsOf(ctx).map((command) => [command.id, command.enabled(ctx)]))).toEqual({
      "edits.addRow": true,
      "edits.deleteRows": true,
      "edits.setNull": true,
      "edits.commit": true,
      "edits.discardAll": true,
      "edits.undo": true,
      "edits.redo": false,
    });
  });
});

describe("running an edit command", () => {
  const command = (id: TableEditActionId) => COMMANDS.find((c) => c.id === `edits.${id}`)!;

  it("calls the tab's own handler, as the tab's actions are when it runs", () => {
    const shown = handlers();
    const now = handlers();
    const ctx = context(TABLE_TAB, tableEditActions(editable, shown));
    mounted.actions = tableEditActions(editable, now);
    command("undo").run(ctx);
    expect(now.undo).toHaveBeenCalledTimes(1);
    expect(shown.undo).not.toHaveBeenCalled();
  });

  it("does nothing once the action is no longer possible, or the tab is gone", () => {
    const now = handlers();
    const ctx = context(TABLE_TAB, tableEditActions(editable, handlers()));
    mounted.actions = tableEditActions({ ...editable, editable: false }, now);
    command("addRow").run(ctx);
    expect(now.addRow).not.toHaveBeenCalled();
    mounted.actions = null;
    command("commit").run(ctx);
    expect(now.commit).not.toHaveBeenCalled();
  });
});
