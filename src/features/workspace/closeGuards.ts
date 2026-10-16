import { useEffect, useLayoutEffect, useRef } from "react";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import { useWorkspace } from "@/stores/workspace";

/** Asked before a tab closes: true lets it close, false keeps it open (the guard has said why, e.g. in a dialog). */
export type CloseGuard = () => Promise<boolean> | boolean;

/** The order a tab's guards are asked in, lowest first: a running export, then an open transaction, then the script. */
export const CLOSE_GUARD_ORDER = { export: 10, transaction: 20, script: 30 } as const;

interface Entry {
  order: number;
  guard: CloseGuard;
}

const guards = new Map<string, Set<Entry>>();
/** Closes that are being asked about, by tab: asking again (a second ⌘W) gets the same answer. */
const pending = new Map<string, Promise<boolean>>();
/** Every close waits for the one before it, so two tabs never ask at the same time. */
let queue: Promise<unknown> = Promise.resolve();

export function registerCloseGuard(tabId: string, order: number, guard: CloseGuard): () => void {
  const entry: Entry = { order, guard };
  let entries = guards.get(tabId);
  if (!entries) {
    entries = new Set();
    guards.set(tabId, entries);
  }
  entries.add(entry);
  const owned = entries;
  return () => {
    owned.delete(entry);
    if (owned.size === 0 && guards.get(tabId) === owned) guards.delete(tabId);
  };
}

/** Registers a close guard for the component's lifetime; the latest `guard` passed is the one asked. */
export function useCloseGuard(tabId: string, order: number, guard: CloseGuard): void {
  const latest = useRef(guard);
  useLayoutEffect(() => {
    latest.current = guard;
  });
  useEffect(() => registerCloseGuard(tabId, order, () => latest.current()), [tabId, order]);
}

const isOpen = (tabId: string) => useWorkspace.getState().tabs.some((tab) => tab.id === tabId);

async function askGuards(tabId: string): Promise<boolean> {
  const entries = [...(guards.get(tabId) ?? [])].sort((a, b) => a.order - b.order);
  for (const entry of entries) {
    if (!isOpen(tabId)) return false; // closed some other way meanwhile
    try {
      if (!(await entry.guard())) return false;
    } catch (error) {
      console.error("A close guard failed", error);
      toast("error", `Could not check the tab before closing it: ${errorMessage(error)}`);
      return false;
    }
  }
  return isOpen(tabId);
}

/**
 * The one way a tab is closed by the user (close button, middle click, ⌘W, the palette): the tab's guards are asked
 * one at a time, in order, and the tab closes only if every one agrees. Resolves true once it is closed.
 */
export function requestCloseTab(tabId: string): Promise<boolean> {
  const asking = pending.get(tabId);
  if (asking) return asking;
  const result = queue
    .then(() => askGuards(tabId))
    .then((close) => {
      if (close) useWorkspace.getState().closeTab(tabId);
      return close;
    })
    .finally(() => pending.delete(tabId));
  pending.set(tabId, result);
  queue = result.catch(() => undefined);
  return result;
}
