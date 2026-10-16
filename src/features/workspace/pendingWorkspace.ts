import type { SavedWorkspace } from "@/lib/api/types";

/*
 * A write-ahead copy of the workspace in `localStorage`, kept only while the backend has not acknowledged it (ruling
 * R-02c-7): written when a save fails and when the page goes away with unsaved changes, removed once a save of a
 * later state succeeds. The backend's `workspace.json` stays the source of truth; this copy is only ever newer.
 * Every storage access is guarded: storage can be disabled or full.
 */
const KEY = "dbaguy.workspace.pending";

export interface PendingWorkspace {
  file: SavedWorkspace;
  /** When it was written (`Date.now()`). */
  at: number;
}

/** Writes synchronously (it must work while the page unloads); false if the browser refused. */
export function writePendingWorkspace(json: string, at: number): boolean {
  try {
    // `json` is already JSON: embedded as is, not escaped into a string.
    localStorage.setItem(KEY, `{"at":${at},"workspace":${json}}`);
    return true;
  } catch (error) {
    console.warn("Could not keep the unsaved workspace in local storage", error);
    return false;
  }
}

export function clearPendingWorkspace(): void {
  try {
    localStorage.removeItem(KEY);
  } catch (error) {
    console.warn("Could not remove the unsaved workspace copy from local storage", error);
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;
const TAB_KINDS: ReadonlySet<unknown> = new Set(["query", "table", "object"]);

/** The stored copy, if it reads as a workspace; tabs without a usable id or kind are left out. */
function asPending(value: unknown): PendingWorkspace | null {
  if (!isRecord(value) || typeof value.at !== "number" || !isRecord(value.workspace)) return null;
  const { tabs } = value.workspace;
  if (!Array.isArray(tabs)) return null;
  const usable = tabs.filter((tab) => isRecord(tab) && typeof tab.id === "string" && tab.id && TAB_KINDS.has(tab.kind));
  return { at: value.at, file: { ...(value.workspace as SavedWorkspace), tabs: usable as SavedWorkspace["tabs"] } };
}

/** The unsaved copy left by an earlier page, or null. An unreadable one is dropped. */
export function readPendingWorkspace(): PendingWorkspace | null {
  let raw: string | null;
  try {
    raw = localStorage.getItem(KEY);
  } catch (error) {
    console.warn("Could not read local storage", error);
    return null;
  }
  if (raw === null) return null;
  let pending: PendingWorkspace | null = null;
  try {
    pending = asPending(JSON.parse(raw));
  } catch {
    // reported below
  }
  if (!pending) {
    console.warn("Dropped an unreadable unsaved-workspace copy from local storage");
    clearPendingWorkspace();
  }
  return pending;
}
