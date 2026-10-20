import { useLayoutEffect, useRef } from "react";

/*
 * What the command palette can do with a mounted tab, by tab id (ruling R-02c-15). The palette never runs or ends
 * anything itself: it calls the tab's own handlers, the ones its toolbar and keys call, so confirmations and state apply
 * alike. One registry per kind of tab (query tabs, table tabs).
 */

export interface TabAction {
  /** Whether the tab's own control would allow it now (its button is enabled). */
  enabled: boolean;
  /** For a toggle: whether it is on now. */
  checked?: boolean;
  /** The tab's own handler. */
  run: () => void;
}

export interface TabActionRegistry<Id extends string> {
  /** The tab's actions as they are now; null when no tab with that id is mounted. */
  read: (tabId: string) => Record<Id, TabAction> | null;
  /**
   * Registers a reader of the tab's actions; the returned function removes it (only it: a newer registration of the
   * same tab stays). What `use` does for a mounted component.
   */
  register: (tabId: string, read: () => Record<Id, TabAction>) => () => void;
  /** Registers the tab's actions while the component is mounted; the latest `actions` passed are the ones read. */
  use: (tabId: string, actions: Record<Id, TabAction>) => void;
}

export function createTabActionRegistry<Id extends string>(): TabActionRegistry<Id> {
  const readers = new Map<string, () => Record<Id, TabAction>>();
  const register = (tabId: string, read: () => Record<Id, TabAction>) => {
    readers.set(tabId, read);
    return () => {
      if (readers.get(tabId) === read) readers.delete(tabId);
    };
  };
  return {
    read: (tabId) => readers.get(tabId)?.() ?? null,
    register,
    use: function useTabActions(tabId, actions) {
      const latest = useRef(actions);
      useLayoutEffect(() => {
        latest.current = actions;
      });
      useLayoutEffect(() => register(tabId, () => latest.current), [tabId]);
    },
  };
}
