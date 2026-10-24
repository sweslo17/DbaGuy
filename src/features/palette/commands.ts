import type { QueryClient } from "@tanstack/react-query";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import type { AppSettings, EngineInfo, Profile, ProfileEntry } from "@/lib/api/types";
import type { TabAction } from "@/lib/tabActions";
import { openNewQuery, useWorkspace, type Tab } from "@/stores/workspace";
import { enginesKey, findEngineInfo, profilesKey } from "../connections/hooks";
import { openCopyDialog, openDumpDialog, openRunDialog, setJobsPopoverOpen } from "../data-tools/dataDialogs";
import { paletteCopyEntry, paletteDumpEntry, paletteRunEntry } from "../data-tools/entryPoints";
import { useJobs } from "../data-tools/jobsStore";
import { readTableEditActions, type TableEditActionId, type TableEditActions } from "../edits/editActions";
import { refreshSchema } from "../explorer/refreshSchema";
import { HISTORY_VIEW } from "../history/useHistory";
import { readQueryTabActions, type QueryTabActionId, type QueryTabActions } from "../query/tabActions";
import { openSettings } from "../settings/SettingsDialog";
import { readTableTabActions, type TableTabActions } from "../table-data/tableTabActions";
import { requestCloseTab } from "../workspace/closeGuards";

/**
 * The shortcut labels the palette shows, in one place. The bindings: ⌘↵, ⌘⇧↵, ⌘E and ⌘⇧F are the query tab's editor
 * keymap; ⌘S, ⌘⌫, ⌥⌘N, ⌘Z and ⇧⌘Z a table tab's grid (spec ⑤ §3.6.4); ⌘W the workspace; ⌘, the settings dialog;
 * ⌘P and ⌘⇧P the app. A command without a binding has none.
 */
export const SHORTCUTS = {
  run: "⌘↵",
  commitEdits: "⌘S",
  deleteRows: "⌘⌫",
  setNull: "⌥⌘N",
  undoEdit: "⌘Z",
  redoEdit: "⇧⌘Z",
  runAll: "⌘⇧↵",
  explain: "⌘E",
  format: "⌘⇧F",
  closeTab: "⌘W",
  settings: "⌘,",
  quickOpen: "⌘P",
  commandPalette: "⌘⇧P",
} as const;

/** What the commands look at, read when the palette shows them. */
export interface CommandContext {
  /** The active tab; null when none is open. */
  activeTab: Tab | null;
  /** The active query tab's own actions and whether each is possible; null when no query tab is active. */
  tabActions: QueryTabActions | null;
  /** The active table tab's edit actions; null when no table tab is active. */
  editActions: TableEditActions | null;
  /** The active table tab's other actions (Dump table…, Copy rows to…); null when no table tab is active. */
  tableActions: TableTabActions | null;
  settings: {
    /** The stored settings are known (until then nothing may be saved over them). */
    loaded: boolean;
    current: AppSettings;
    save: (next: AppSettings) => Promise<AppSettings>;
  };
  queryClient: QueryClient;
}

export interface PaletteCommand {
  id: string;
  label: string;
  shortcut?: string;
  /** Disabled commands are shown greyed. */
  enabled: (ctx: CommandContext) => boolean;
  /** A command that only some engines have is left out of the list for the others (default: always listed). */
  visible?: (ctx: CommandContext) => boolean;
  /** For a toggle: whether it is on now. */
  checked?: (ctx: CommandContext) => boolean | undefined;
  run: (ctx: CommandContext) => void;
}

const always = () => true;

/**
 * Runs a tab's own action as it is now (read again from its registry: the tab may have changed while the palette was
 * open), or does nothing once it is no longer possible.
 */
function runTabAction(action: TabAction | undefined, label: string): void {
  if (action?.enabled) action.run();
  else console.info(`Palette: ${label} is no longer possible in the active tab`);
}

/**
 * A command the active query tab carries out with its own handler (ruling R-02c-15), the one its toolbar button or
 * keymap calls. Asked again when it runs: the tab may have changed while the palette was open.
 */
function tabCommand(
  id: QueryTabActionId,
  label: string,
  shortcut?: string,
  visible?: PaletteCommand["visible"],
): PaletteCommand {
  return {
    id,
    label,
    shortcut,
    visible,
    enabled: (ctx) => ctx.tabActions?.[id].enabled ?? false,
    checked: (ctx) => ctx.tabActions?.[id].checked,
    run: (ctx) => runTabAction(ctx.activeTab ? readQueryTabActions(ctx.activeTab.id)?.[id] : undefined, label),
  };
}

/** What the active tab's connection is and can do, from the profiles and engines already loaded; null if unknown. */
function activeEngine(ctx: CommandContext): EngineInfo | null {
  return findEngineInfo(
    ctx.queryClient.getQueryData<ProfileEntry[]>(profilesKey),
    ctx.queryClient.getQueryData<EngineInfo[]>(enginesKey),
    ctx.activeTab?.profileId ?? null,
  );
}

/**
 * A table tab's edit command (spec ⑤ §3.6.4), listed only while a table tab of an engine that edits rows is active
 * (Couchbase has none: a document is edited in its own tab), with the tab's own handler; asked again when it runs.
 */
function editCommand(id: TableEditActionId, label: string, shortcut?: string): PaletteCommand {
  return {
    id: `edits.${id}`,
    label,
    shortcut,
    visible: (ctx) => ctx.editActions !== null && activeEngine(ctx)?.row_editing === true,
    enabled: (ctx) => ctx.editActions?.[id].enabled ?? false,
    run: (ctx) => runTabAction(ctx.activeTab ? readTableEditActions(ctx.activeTab.id)?.[id] : undefined, label),
  };
}

/** The active tab's connection, from the profiles already loaded; null if unknown. */
function activeProfile(ctx: CommandContext): Profile | null {
  const id = ctx.activeTab?.profileId ?? null;
  return ctx.queryClient.getQueryData<ProfileEntry[]>(profilesKey)?.find((entry) => entry.profile.id === id)?.profile ?? null;
}

/**
 * ⌘⇧P Dump… (spec ⑥ §3.6): a table tab dumps its table through its own handler (it knows the filter it shows); a query
 * tab dumps its database (PostgreSQL, SQL Server), its schema (MySQL) or main (SQLite). Listed for engines that dump.
 */
const dumpCommand: PaletteCommand = {
  id: "dataTools.dump",
  label: "Dump…",
  visible: (ctx) => activeEngine(ctx)?.dump === true,
  enabled: (ctx) =>
    ctx.activeTab?.kind === "table"
      ? (ctx.tableActions?.dumpTable.enabled ?? false)
      : paletteDumpEntry(ctx.activeTab, activeProfile(ctx), activeEngine(ctx), "") !== null,
  run: (ctx) => {
    if (ctx.activeTab?.kind === "table") {
      runTabAction(readTableTabActions(ctx.activeTab.id)?.dumpTable, "Dump…");
      return;
    }
    const entry = paletteDumpEntry(ctx.activeTab, activeProfile(ctx), activeEngine(ctx), "");
    if (entry) openDumpDialog(entry);
  },
};

/**
 * ⌘⇧P Copy data to… (spec ⑥ §3.6, 06c): a table tab copies its rows through its own handler (it knows its filter); a
 * query tab its schema's tables (its database's default schema; MySQL: its database). Listed for engines that copy.
 */
const copyCommand: PaletteCommand = {
  id: "dataTools.copy",
  label: "Copy data to…",
  visible: (ctx) => activeEngine(ctx)?.copy_data === true,
  enabled: (ctx) =>
    ctx.activeTab?.kind === "table"
      ? (ctx.tableActions?.copyRows.enabled ?? false)
      : paletteCopyEntry(ctx.activeTab, activeProfile(ctx), activeEngine(ctx), "") !== null,
  run: (ctx) => {
    if (ctx.activeTab?.kind === "table") {
      runTabAction(readTableTabActions(ctx.activeTab.id)?.copyRows, "Copy data to…");
      return;
    }
    const entry = paletteCopyEntry(ctx.activeTab, activeProfile(ctx), activeEngine(ctx), "");
    if (entry) openCopyDialog(entry);
  },
};

/** Couchbase: its plans (Explain). */
const takesPlans = (ctx: CommandContext) => activeEngine(ctx)?.plan_format === "couchbase";
/** Couchbase: the plan a statement ran with (Run with profile). */
const takesProfile = (ctx: CommandContext) => takesPlans(ctx) && !!activeEngine(ctx)?.executed_plan;

async function toggleVim({ current, save }: CommandContext["settings"]): Promise<void> {
  const vim = !current.editor.vim;
  try {
    const saved = await save({ ...current, editor: { ...current.editor, vim } });
    console.info(`Vim key bindings ${saved.editor.vim ? "on" : "off"}`);
    toast("info", `Vim key bindings ${saved.editor.vim ? "on" : "off"}`);
  } catch (error) {
    toast("error", `Could not turn vim key bindings ${vim ? "on" : "off"}: ${errorMessage(error)}`);
  }
}

async function refreshActiveSchema(qc: QueryClient, profileId: string): Promise<void> {
  try {
    await refreshSchema(qc, profileId);
    toast("success", "Schema refreshed");
  } catch (error) {
    toast("error", `Could not refresh the schema: ${errorMessage(error)}`);
  }
}

/** The ⌘⇧P commands (spec §4.10), in the order shown. */
export const COMMANDS: readonly PaletteCommand[] = [
  {
    id: "newQuery",
    label: "New query",
    enabled: always,
    // On the active tab's connection, else the one selected in the explorer.
    run: () => void openNewQuery(),
  },
  tabCommand("run", "Run", SHORTCUTS.run),
  tabCommand("runAll", "Run all", SHORTCUTS.runAll),
  tabCommand("explain", "Explain", SHORTCUTS.explain, takesPlans),
  tabCommand("runWithProfile", "Run with profile", undefined, takesProfile),
  tabCommand("format", "Format", SHORTCUTS.format),
  {
    id: "toggleVim",
    label: "Toggle vim",
    enabled: (ctx) => ctx.settings.loaded,
    checked: (ctx) => ctx.settings.current.editor.vim,
    run: (ctx) => void toggleVim(ctx.settings),
  },
  editCommand("addRow", "Add row"),
  editCommand("deleteRows", "Delete selected rows", SHORTCUTS.deleteRows),
  editCommand("setNull", "Set NULL", SHORTCUTS.setNull),
  editCommand("commit", "Commit changes…", SHORTCUTS.commitEdits),
  editCommand("discardAll", "Discard all changes"),
  editCommand("undo", "Undo staged change", SHORTCUTS.undoEdit),
  editCommand("redo", "Redo staged change", SHORTCUTS.redoEdit),
  tabCommand("toggleAutocommit", "Toggle auto-commit"),
  tabCommand("commit", "Commit"),
  tabCommand("rollback", "Rollback"),
  {
    id: "refreshSchema",
    label: "Refresh schema",
    enabled: (ctx) => !!ctx.activeTab?.profileId,
    run: (ctx) => {
      const profileId = ctx.activeTab?.profileId;
      if (profileId) void refreshActiveSchema(ctx.queryClient, profileId);
    },
  },
  dumpCommand,
  copyCommand,
  {
    id: "dataTools.runFile",
    label: "Run SQL file…",
    enabled: always,
    // On the active tab's connection and database, else the one selected in the tree; the dialog can change them.
    run: (ctx) =>
      openRunDialog(paletteRunEntry(ctx.activeTab, useWorkspace.getState().selectedProfileId, activeEngine(ctx))),
  },
  {
    id: "dataTools.jobs",
    label: "Show jobs",
    enabled: () => useJobs.getState().order.length > 0,
    run: () => setJobsPopoverOpen(true),
  },
  {
    id: "closeTab",
    label: "Close tab",
    shortcut: SHORTCUTS.closeTab,
    enabled: (ctx) => ctx.activeTab !== null,
    // Through the close guards, like every other way of closing a tab.
    run: (ctx) => {
      if (ctx.activeTab) void requestCloseTab(ctx.activeTab.id);
    },
  },
  { id: "settings", label: "Settings", shortcut: SHORTCUTS.settings, enabled: always, run: openSettings },
  {
    id: "openHistory",
    label: "Open history",
    enabled: always,
    run: () => useWorkspace.getState().setSidebarTab(HISTORY_VIEW),
  },
  {
    id: "openScripts",
    label: "Open scripts",
    enabled: always,
    run: () => useWorkspace.getState().setSidebarTab("scripts"),
  },
];
