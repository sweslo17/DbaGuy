import { create } from "zustand";
import { pathKey, type ObjectNode, type PathSegment } from "@/lib/api/types";
import { relationOf } from "@/features/table-data/filterValue";

/** A SQL editor tab with its own session. */
export interface QueryTabState {
  id: string;
  kind: "query";
  title: string;
  /** null: no connection chosen yet (the editor works, running does not). */
  profileId: string | null;
  database: string | null;
  /** The script file the tab is bound to, and its content as last saved (Task 6). */
  scriptPath: string | null;
  savedText: string | null;
  /** The editor's content when the tab was created or restored. */
  initialText: string;
  initialCursor?: number;
  initialSelection?: { anchor: number; head: number };
  /** The desired auto-commit mode, applied when the session opens. */
  autocommit: boolean;
}

/** A relation (table, view, materialized view, partition): its data, columns and DDL. */
export interface TableTabState {
  id: string;
  kind: "table";
  profileId: string;
  path: PathSegment[];
  title: string;
}

/** Any other database object (function, sequence, index, …): its detail page. */
export interface ObjectTabState {
  id: string;
  kind: "object";
  profileId: string;
  path: PathSegment[];
  title: string;
}

export type Tab = QueryTabState | TableTabState | ObjectTabState;
export type TabKind = Tab["kind"];
export type TabOf<K extends TabKind> = Extract<Tab, { kind: K }>;

/** The fields of a tab of kind `K` that can change after it opened (never its id or kind). */
export type TabPatch<K extends TabKind> = Partial<Omit<TabOf<K>, "id" | "kind">>;

/** Everything `workspace.json` keeps (spec §4.8), besides the editors' text, which is never React state. */
export interface WorkspaceSnapshot {
  tabs: Tab[];
  activeTabId: string | null;
  /** The sidebar's open view (Task 6 wires it); kept as found until then. */
  sidebarTab: string | null;
}

export interface OpenQueryOptions {
  /** The editor's text (e.g. a table data tab's SELECT, or a script's content). */
  text?: string;
  /** Binds the tab to a script file; its `text` is then the saved content. */
  scriptPath?: string;
  title?: string;
}

interface WorkspaceState extends WorkspaceSnapshot {
  /** The connection selected in the explorer (not persisted). */
  selectedProfileId: string | null;
  /** Opens (or activates) a relation as a table tab, any other object as an object tab. */
  openObject: (profileId: string, node: ObjectNode) => void;
  /** Opens a new query tab and returns its id. */
  openQuery: (profileId: string | null, database: string | null, options?: OpenQueryOptions) => string;
  closeTab: (id: string) => void;
  activate: (id: string) => void;
  /** Patches the tab `id` if it is of `kind`: a field of another kind cannot be patched onto it. */
  updateTab: <K extends TabKind>(id: string, kind: K, patch: TabPatch<K>) => void;
  selectProfile: (id: string | null) => void;
  setSidebarTab: (tab: string | null) => void;
  /** Replaces the tabs with the saved workspace (startup only, before the workspace renders). */
  restore: (snapshot: WorkspaceSnapshot) => void;
}

/** The number in the default title "Query N"; new query tabs count on from the highest one open. */
let queryCounter = 0;
const queryNumber = (tab: Tab) => (tab.kind === "query" ? Number(/^Query (\d+)$/.exec(tab.title)?.[1] ?? 0) : 0);

export const useWorkspace = create<WorkspaceState>((set, get) => ({
  tabs: [],
  activeTabId: null,
  sidebarTab: null,
  selectedProfileId: null,
  openObject: (profileId, node) => {
    const kind = relationOf(node.path) ? "table" : "object";
    const key = pathKey(node.path);
    const open = get().tabs.find(
      (t) => t.kind !== "query" && t.kind === kind && t.profileId === profileId && pathKey(t.path) === key,
    );
    if (open) {
      set({ activeTabId: open.id });
      return;
    }
    // A short random id: a path can be long, and `workspace.json` limits ids to 100 characters.
    const id = `${kind}:${crypto.randomUUID()}`;
    const tab: TableTabState | ObjectTabState = { id, kind, profileId, path: node.path, title: node.name };
    set((s) => ({ tabs: [...s.tabs, tab], activeTabId: id }));
  },
  openQuery: (profileId, database, options = {}) => {
    queryCounter += 1;
    const id = `query:${crypto.randomUUID()}`;
    const text = options.text ?? "";
    const tab: QueryTabState = {
      id,
      kind: "query",
      title: options.title ?? `Query ${queryCounter}`,
      profileId,
      database,
      scriptPath: options.scriptPath ?? null,
      savedText: options.scriptPath ? text : null,
      initialText: text,
      autocommit: true,
    };
    set((s) => ({ tabs: [...s.tabs, tab], activeTabId: id }));
    return id;
  },
  closeTab: (id) =>
    set((s) => {
      const index = s.tabs.findIndex((t) => t.id === id);
      const tabs = s.tabs.filter((t) => t.id !== id);
      const activeTabId = s.activeTabId === id ? (tabs[Math.min(index, tabs.length - 1)]?.id ?? null) : s.activeTabId;
      return { tabs, activeTabId };
    }),
  activate: (id) => set({ activeTabId: id }),
  updateTab: (id, kind, patch) =>
    set((s) => ({ tabs: s.tabs.map((t) => (t.id === id && t.kind === kind ? ({ ...t, ...patch } as Tab) : t)) })),
  selectProfile: (id) => set({ selectedProfileId: id }),
  setSidebarTab: (tab) => set({ sidebarTab: tab }),
  restore: ({ tabs, activeTabId, sidebarTab }) => {
    queryCounter = Math.max(queryCounter, ...tabs.map(queryNumber));
    set({ tabs, activeTabId, sidebarTab });
  },
}));
