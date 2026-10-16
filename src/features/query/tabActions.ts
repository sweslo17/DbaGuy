import { useLayoutEffect, useRef } from "react";

/** What the command palette can do with a query tab (ruling R-02c-15). */
export type QueryTabActionId = "run" | "runAll" | "format" | "toggleAutocommit" | "commit" | "rollback";

export interface QueryTabAction {
  /** Whether the tab's own control would allow it now (the toolbar button is enabled). */
  enabled: boolean;
  /** For a toggle: whether it is on now. */
  checked?: boolean;
  /** The tab's own handler: the one its toolbar button or keymap calls. */
  run: () => void;
}

export type QueryTabActions = Record<QueryTabActionId, QueryTabAction>;

/** Reads a tab's actions as they are at the moment it is called. */
type ReadActions = () => QueryTabActions;

/*
 * The mounted query tabs' actions, by tab id. The palette never runs or ends anything itself: it calls the same
 * functions as the tab's toolbar and keymap, so confirmations, `canRun` and the transaction state apply alike.
 */
const registry = new Map<string, ReadActions>();

function registerQueryTabActions(tabId: string, read: ReadActions): () => void {
  registry.set(tabId, read);
  return () => {
    if (registry.get(tabId) === read) registry.delete(tabId);
  };
}

/** The tab's actions as they are now; null when no query tab with that id is mounted. */
export function readQueryTabActions(tabId: string): QueryTabActions | null {
  return registry.get(tabId)?.() ?? null;
}

/** Registers the tab's actions while the component is mounted; the latest `actions` passed are the ones read. */
export function useQueryTabActions(tabId: string, actions: QueryTabActions): void {
  const latest = useRef(actions);
  useLayoutEffect(() => {
    latest.current = actions;
  });
  useLayoutEffect(() => registerQueryTabActions(tabId, () => latest.current), [tabId]);
}
