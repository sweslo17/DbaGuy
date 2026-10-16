import { create } from "zustand";
import { pathKey, type ObjectNode, type PathSegment } from "@/lib/api/types";

export interface ObjectTab {
  id: string;
  kind: "object";
  profileId: string;
  path: PathSegment[];
  title: string;
  subtitle: string;
}
export interface ConsoleTab {
  id: string;
  kind: "console";
  profileId: string;
  database: string | null;
  title: string;
  subtitle: string;
  /** The editor's text when the tab opens (e.g. a table data tab's "Open in query tab"). */
  initialSql?: string;
}
export type Tab = ObjectTab | ConsoleTab;

interface WorkspaceState {
  tabs: Tab[];
  activeTabId: string | null;
  selectedProfileId: string | null;
  openObject: (profileId: string, node: ObjectNode, profileName: string) => void;
  /** Opens a new query tab, its editor prefilled with `initialSql`. */
  openQuery: (profileId: string, database: string | null, profileName: string, initialSql?: string) => void;
  closeTab: (id: string) => void;
  activate: (id: string) => void;
  selectProfile: (id: string | null) => void;
}

let consoleCounter = 0;

export const useWorkspace = create<WorkspaceState>((set, get) => ({
  tabs: [],
  activeTabId: null,
  selectedProfileId: null,
  openObject: (profileId, node, profileName) => {
    const id = `object:${profileId}:${pathKey(node.path)}`;
    if (!get().tabs.some((t) => t.id === id)) {
      const tab: ObjectTab = {
        id,
        kind: "object",
        profileId,
        path: node.path,
        title: node.name,
        subtitle: profileName,
      };
      set((s) => ({ tabs: [...s.tabs, tab] }));
    }
    set({ activeTabId: id });
  },
  openQuery: (profileId, database, profileName, initialSql) => {
    consoleCounter += 1;
    const id = `console:${crypto.randomUUID()}`;
    const tab: ConsoleTab = {
      id,
      kind: "console",
      profileId,
      database,
      title: `Query ${consoleCounter}`,
      subtitle: profileName,
      initialSql,
    };
    set((s) => ({ tabs: [...s.tabs, tab], activeTabId: id }));
  },
  closeTab: (id) =>
    set((s) => {
      const index = s.tabs.findIndex((t) => t.id === id);
      const tabs = s.tabs.filter((t) => t.id !== id);
      const activeTabId = s.activeTabId === id ? (tabs[Math.min(index, tabs.length - 1)]?.id ?? null) : s.activeTabId;
      return { tabs, activeTabId };
    }),
  activate: (id) => set({ activeTabId: id }),
  selectProfile: (id) => set({ selectedProfileId: id }),
}));
