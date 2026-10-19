import { create } from "zustand";
import {
  changeCount,
  emptyStaged,
  MAX_CHANGES,
  restoresRows,
  stage,
  type StageAction,
  type StagedEdits,
} from "@/features/edits/staging";
import { sameElements } from "@/features/grid/useHeldResult";

/*
 * The table-data tabs' staged grid edits, by tab id (spec ⑤ §3.6.1). Outside React, like `editorBuffers`: the one
 * workspace writer and the close guard read them without waiting for a tab to mount, and a tab that is not shown still
 * has its restored changes. Every change notifies the writer (`onStagedEditsChanged`), except a closed tab's drop.
 */

const useStore = create<Readonly<Record<string, StagedEdits>>>(() => ({}));
const listeners = new Set<() => void>();

/** What a dispatch did: "too_many" and "key_changed" changed nothing and the caller says why. */
export type DispatchResult = "staged" | "unchanged" | "too_many" | "key_changed";

/** The actions that stage something new: refused while the changes are on another key than the table's (ruling H13). */
const ADDING: ReadonlySet<StageAction["type"]> = new Set([
  "edit",
  "setNull",
  "useDefault",
  "deleteRows",
  "addRow",
  "keepMine",
]);

/** Whether `action` would stage something new on `staged` (Delete over rows all marked already restores them). */
function adds(staged: StagedEdits, action: StageAction): boolean {
  if (action.type === "deleteRows") return !restoresRows(staged, action.rows);
  return ADDING.has(action.type);
}

export function useStagedEdits(tabId: string): StagedEdits | null {
  return useStore((all) => all[tabId] ?? null);
}

export function readStagedEdits(tabId: string): StagedEdits | null {
  return useStore.getState()[tabId] ?? null;
}

/** Puts a tab's staged edits (a restored file's), or drops them (null). */
export function putStagedEdits(tabId: string, staged: StagedEdits | null): void {
  const current = useStore.getState();
  if ((current[tabId] ?? null) === staged) return;
  const next = { ...current };
  if (staged) next[tabId] = staged;
  else delete next[tabId];
  useStore.setState(next, true);
  for (const listener of listeners) listener();
}

/**
 * Applies `action` to the tab's staged edits. `keyColumns` is the table's key now (its edit target's): a tab with
 * nothing staged takes it, and its undo history goes with the old key (its RowKeys are of that key, so a redo would put
 * them under the new one); changes staged on another key are kept (they can be committed, reverted, undone or
 * discarded) but nothing new is staged on them (`key_changed`). An action that would leave more than MAX_CHANGES
 * changes is refused (`too_many`, D-05-11). Without `keyColumns` (an outcome, a refresh), a tab with nothing staged is
 * left alone.
 */
export function dispatchStaged(tabId: string, action: StageAction, keyColumns?: readonly string[]): DispatchResult {
  const current = readStagedEdits(tabId);
  if (!current && !keyColumns) return "unchanged";
  let base = current ?? emptyStaged(keyColumns ?? []);
  if (keyColumns && !sameElements(base.keyColumns, keyColumns)) {
    if (changeCount(base) > 0) {
      if (adds(base, action)) return "key_changed";
    } else base = { ...emptyStaged(keyColumns), unknownOutcome: base.unknownOutcome, nextSeq: base.nextSeq };
  }
  const next = stage(base, action);
  if (next === base) return "unchanged";
  const count = changeCount(next);
  if (count > MAX_CHANGES && count > changeCount(base)) return "too_many";
  putStagedEdits(tabId, next);
  return "staged";
}

/**
 * Drops what is kept for tabs that are no longer open. The workspace writer calls it as it saves, and a closed tab is
 * not saved, so the listeners are not told (telling them would cost one more save per closed tab).
 */
export function forgetStagedEditsExcept(openTabIds: ReadonlySet<string>): void {
  const current = useStore.getState();
  const gone = Object.keys(current).filter((tabId) => !openTabIds.has(tabId));
  if (gone.length === 0) return;
  const next = { ...current };
  for (const tabId of gone) delete next[tabId];
  useStore.setState(next, true);
}

export function onStagedEditsChanged(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// Dev only: like `editorBuffers`, a hot update would hand the writer an empty copy, so the page reloads instead.
if (import.meta.hot) {
  import.meta.hot.accept();
  import.meta.hot.dispose(() => window.location.reload());
}
