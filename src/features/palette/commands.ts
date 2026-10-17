import type { QueryClient } from "@tanstack/react-query";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import type { AppSettings } from "@/lib/api/types";
import { openNewQuery, useWorkspace, type Tab } from "@/stores/workspace";
import { refreshSchema } from "../explorer/refreshSchema";
import { HISTORY_VIEW } from "../history/useHistory";
import { readQueryTabActions, type QueryTabActionId, type QueryTabActions } from "../query/tabActions";
import { openSettings } from "../settings/SettingsDialog";
import { requestCloseTab } from "../workspace/closeGuards";

/**
 * The shortcut labels the palette shows, in one place. The bindings: ⌘↵, ⌘⇧↵ and ⌘⇧F are the query tab's editor
 * keymap, ⌘W the workspace, ⌘, the settings dialog, ⌘P and ⌘⇧P the app. A command without a binding has none.
 */
export const SHORTCUTS = {
  run: "⌘↵",
  runAll: "⌘⇧↵",
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
  /** For a toggle: whether it is on now. */
  checked?: (ctx: CommandContext) => boolean | undefined;
  run: (ctx: CommandContext) => void;
}

const always = () => true;

/**
 * A command the active query tab carries out with its own handler (ruling R-02c-15), the one its toolbar button or
 * keymap calls. Asked again when it runs: the tab may have changed while the palette was open.
 */
function tabCommand(id: QueryTabActionId, label: string, shortcut?: string): PaletteCommand {
  return {
    id,
    label,
    shortcut,
    enabled: (ctx) => ctx.tabActions?.[id].enabled ?? false,
    checked: (ctx) => ctx.tabActions?.[id].checked,
    run: (ctx) => {
      const action = ctx.activeTab ? readQueryTabActions(ctx.activeTab.id)?.[id] : undefined;
      if (action?.enabled) action.run();
      else console.info(`Palette: ${label} is no longer possible in the active tab`);
    },
  };
}

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
  tabCommand("format", "Format", SHORTCUTS.format),
  {
    id: "toggleVim",
    label: "Toggle vim",
    enabled: (ctx) => ctx.settings.loaded,
    checked: (ctx) => ctx.settings.current.editor.vim,
    run: (ctx) => void toggleVim(ctx.settings),
  },
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
