import { useEffect, useLayoutEffect, useRef } from "react";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import { useWorkspace } from "@/stores/workspace";

/** Why a tab's guards are asked: the tab closes, or it stays but its connection goes away (the profile is deleted). */
export type CloseIntent = "close" | "disconnect";

/** Asked before a tab closes: true lets it close, false keeps it open (the guard has said why, e.g. in a dialog). */
export type CloseGuard = (intent: CloseIntent) => Promise<boolean> | boolean;

/**
 * The order a tab's guards are asked in, lowest first: a running export, then the run (no further statement is sent),
 * then an open transaction, then the unsaved script. A disconnect asks only the session's guards (below `script`).
 */
export const CLOSE_GUARD_ORDER = { export: 10, run: 15, transaction: 20, script: 30 } as const;

interface Entry {
  order: number;
  guard: CloseGuard;
}

const guards = new Map<string, Set<Entry>>();
/** Requests being asked about, by tab: asking again (a second ⌘W) gets the same answer. */
const pending = new Map<string, Promise<boolean>>();
/** Every request waits for the one before it, so two tabs never ask at the same time. */
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
  useEffect(() => registerCloseGuard(tabId, order, (intent) => latest.current(intent)), [tabId, order]);
}

const isOpen = (tabId: string) => useWorkspace.getState().tabs.some((tab) => tab.id === tabId);

async function askGuards(tabId: string, intent: CloseIntent): Promise<boolean> {
  const entries = [...(guards.get(tabId) ?? [])]
    .filter((entry) => intent === "close" || entry.order < CLOSE_GUARD_ORDER.script)
    .sort((a, b) => a.order - b.order);
  for (const entry of entries) {
    if (!isOpen(tabId)) return false; // closed some other way meanwhile
    try {
      if (!(await entry.guard(intent))) return false;
    } catch (error) {
      console.error("A close guard failed", error);
      toast("error", `Could not check the tab before closing it: ${errorMessage(error)}`);
      return false;
    }
  }
  return isOpen(tabId);
}

function request(tabId: string, intent: CloseIntent): Promise<boolean> {
  const asking = pending.get(tabId);
  if (asking) return asking;
  const result = queue
    .then(() => askGuards(tabId, intent))
    .then((agreed) => {
      if (agreed && intent === "close") useWorkspace.getState().closeTab(tabId);
      return agreed;
    })
    .finally(() => pending.delete(tabId));
  pending.set(tabId, result);
  queue = result.catch(() => undefined);
  return result;
}

/**
 * The one way a tab is closed by the user (close button, middle click, ⌘W, the palette, deleting its profile): the
 * tab's guards are asked one at a time, in order, and the tab closes only if every one agrees. Resolves true once it
 * is closed.
 */
export function requestCloseTab(tabId: string): Promise<boolean> {
  return request(tabId, "close");
}

/**
 * Asks a tab that stays open whether its session may go away (its profile is being deleted): only the session's
 * guards (export, run, transaction). Resolves true when they agree; the caller then takes the connection away.
 */
export function requestDisconnectTab(tabId: string): Promise<boolean> {
  return request(tabId, "disconnect");
}

/** A close (or disconnect) is being asked about: a shortcut should not queue another behind its dialog. */
export function isClosePending(): boolean {
  return pending.size > 0;
}
