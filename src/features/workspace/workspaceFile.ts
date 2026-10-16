import type { SavedTab, SavedWorkspace, TextSelection } from "@/lib/api/types";
import type { EditorBuffer } from "@/stores/editorBuffers";
import type { QueryTabState, Tab, WorkspaceSnapshot } from "@/stores/workspace";

/*
 * `workspace.json` ↔ the tab store (spec §4.8). A query tab keeps its connection, script binding, editor text,
 * cursor/selection and auto-commit mode; a table or object tab keeps its connection and object path.
 */

const WORKSPACE_VERSION = 1;

/** Lone UTF-16 surrogates (a broken paste): the backend cannot write them as UTF-8, which would fail every save. */
const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g;

const wellFormed = (text: string) => text.replace(LONE_SURROGATE, "�");

/** The tab's selection as last known: its editor's, else what it was restored with. */
function selectionOf(tab: QueryTabState, buffer: EditorBuffer | null): TextSelection | null {
  if (buffer) return buffer.selection;
  if (tab.initialSelection) return tab.initialSelection;
  return tab.initialCursor === undefined ? null : { anchor: tab.initialCursor, head: tab.initialCursor };
}

function savedTab(tab: Tab, readBuffer: (tabId: string) => EditorBuffer | null): SavedTab {
  if (tab.kind !== "query") {
    return { id: tab.id, kind: tab.kind, title: tab.title, profile_id: tab.profileId, object_path: tab.path };
  }
  const buffer = readBuffer(tab.id);
  const selection = selectionOf(tab, buffer);
  return {
    id: tab.id,
    kind: "query",
    title: tab.title,
    profile_id: tab.profileId,
    database: tab.database,
    script_path: tab.scriptPath,
    buffer: wellFormed(buffer?.text ?? tab.initialText),
    cursor: selection?.head ?? null,
    // Only a real range: a bare cursor is `cursor`.
    selection: selection && selection.anchor !== selection.head ? selection : null,
    autocommit: tab.autocommit,
  };
}

/** The workspace as `PUT /workspace` takes it; `readBuffer` gives a query tab's current editor content. */
export function toSavedWorkspace(
  snapshot: WorkspaceSnapshot,
  readBuffer: (tabId: string) => EditorBuffer | null,
): SavedWorkspace {
  return {
    version: WORKSPACE_VERSION,
    tabs: snapshot.tabs.map((tab) => savedTab(tab, readBuffer)),
    active_tab_id: snapshot.activeTabId,
    sidebar_tab: snapshot.sidebarTab,
  };
}

export interface RestoredWorkspace {
  snapshot: WorkspaceSnapshot;
  /** Titles of the table and object tabs dropped because their connection no longer exists. */
  closed: string[];
  /** Titles of the query tabs reopened without a connection because theirs no longer exists. */
  detached: string[];
}

/**
 * The saved workspace as store tabs, keeping their ids. A query tab whose profile is gone reopens without a
 * connection; a table or object tab whose profile is gone is dropped. A saved auto-commit of null means on: the tab's
 * mode must be definite, or nothing could run.
 */
export function fromSavedWorkspace(saved: SavedWorkspace, profileExists: (id: string) => boolean): RestoredWorkspace {
  const tabs: Tab[] = [];
  const closed: string[] = [];
  const detached: string[] = [];
  const seen = new Set<string>();
  for (const entry of saved.tabs) {
    if (seen.has(entry.id)) continue; // React keys and close guards need unique ids
    seen.add(entry.id);
    const profileId = entry.profile_id ?? null;
    const connected = profileId !== null && profileExists(profileId);
    if (entry.kind === "query") {
      if (profileId !== null && !connected) detached.push(entry.title);
      tabs.push({
        id: entry.id,
        kind: "query",
        title: entry.title || "Query",
        profileId: connected ? profileId : null,
        database: connected ? (entry.database ?? null) : null,
        scriptPath: entry.script_path ?? null,
        savedText: null, // unknown until the script is read again
        initialText: entry.buffer ?? "",
        initialCursor: entry.cursor ?? undefined,
        initialSelection: entry.selection ?? undefined,
        autocommit: entry.autocommit ?? true,
      });
    } else if (connected && entry.object_path?.length) {
      tabs.push({ id: entry.id, kind: entry.kind, profileId, path: entry.object_path, title: entry.title });
    } else {
      closed.push(entry.title);
    }
  }
  const active = saved.active_tab_id ?? null;
  return {
    snapshot: {
      tabs,
      activeTabId: tabs.some((tab) => tab.id === active) ? active : (tabs[0]?.id ?? null),
      sidebarTab: saved.sidebar_tab ?? null,
    },
    closed,
    detached,
  };
}
