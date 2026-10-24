import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import { useWorkspace } from "@/stores/workspace";

/** Why a tab's guards are asked: the tab closes, or it stays but its connection goes away (the profile is deleted). */
export type CloseIntent = "close" | "disconnect";

/** Asked before a tab closes: true lets it close, false keeps it open (the guard has said why, e.g. in a dialog). */
export type CloseGuard = (intent: CloseIntent) => Promise<boolean> | boolean;

/**
 * The order a tab's guards are asked in, lowest first (ruling R-02c-8): the unsaved script first, while nothing has
 * been done yet (its Cancel must never follow a side effect), then a running export, then the run (no further
 * statement is sent), then an open transaction. A disconnect asks only the session's guards (the tab, and so its
 * script, stays). A document tab's unsaved text (`document`, ruling C20) and a table tab's staged grid edits (`edits`,
 * spec ⑤ R-05-11) are asked first too: a tab has at most one of a script, a document and staged edits. A copy reading
 * the tab's result (`copy`, 06c pre-flight L-f) comes before them all: it only refuses, and a refusal must never
 * follow a side effect or an answered question.
 */
export const CLOSE_GUARD_ORDER = { copy: 1, script: 5, document: 5, edits: 5, export: 10, run: 15, transaction: 20 } as const;

/**
 * The guards that only closing asks: losing the connection keeps the tab, its text and its staged edits (and a copy
 * reading the tab already keeps the connection: deleting a profile waits for it, B13).
 */
const CLOSE_ONLY: ReadonlySet<number> = new Set([
  CLOSE_GUARD_ORDER.copy,
  CLOSE_GUARD_ORDER.script,
  CLOSE_GUARD_ORDER.document,
  CLOSE_GUARD_ORDER.edits,
]);

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

/**
 * What a tab with unsaved work says when it is about to close: true there is something to lose (it asks), false
 * nothing (it closes), or why it cannot close now (`refuse`: said in a toast, the tab stays).
 */
export type DiscardNeed = boolean | { refuse: string };

/** One tab's "discard?" close question, outside React: at most one is asked at a time. */
export interface DiscardQuestion {
  /** The close guard's answer to `need`: true closes, false keeps the tab, a promise asks the question. */
  decide: (need: DiscardNeed) => Promise<boolean> | boolean;
  /** The question's answer: Discard (true) lets the tab close, Cancel (false) keeps it. */
  answer: (discard: boolean) => void;
  /** The tab goes while asking: it keeps its work, the answer is no. */
  dispose: () => void;
}

/**
 * The state of a "discard?" question (`useDiscardQuestion` holds one per tab). `show` tells the tab to show or hide its
 * question; `refuse` says why it cannot close. Asking while a question waits answers the older one "no" first.
 */
export function createDiscardQuestion(
  show: (asking: boolean) => void,
  refuse: (why: string) => void,
): DiscardQuestion {
  let waiting: ((discard: boolean) => void) | null = null;
  const settle = (discard: boolean) => {
    const resolve = waiting;
    waiting = null;
    resolve?.(discard);
  };
  return {
    decide: (need) => {
      if (need === false) return true;
      if (need !== true) {
        refuse(need.refuse);
        return false;
      }
      settle(false);
      return new Promise<boolean>((resolve) => {
        waiting = resolve;
        show(true);
      });
    },
    answer: (discard) => {
      show(false);
      settle(discard);
    },
    dispose: () => settle(false),
  };
}

/**
 * A close guard that asks "discard?" in the tab's own dialog (pre-flight M6: one for every tab that holds unsaved work:
 * a document's text, a table's staged edits). `needs()`, asked when the tab is about to close and decided from the
 * stores rather than the last render, says whether there is something to lose or why the tab cannot close now
 * (`DiscardNeed`). While it asks, `asking` is true and the tab shows its question with Discard (`answer(true)`) and
 * Cancel (`answer(false)`); no Save, which may need a preview or a decision a close prompt cannot host. The guard never
 * discards anything itself: what the tab holds goes with it once it is closed (a later guard may still keep it open).
 */
export function useDiscardQuestion(
  tabId: string,
  order: number,
  needs: () => DiscardNeed,
): { asking: boolean; answer: (discard: boolean) => void } {
  const [asking, setAsking] = useState(false);
  const [question] = useState(() => createDiscardQuestion(setAsking, (why) => toast("info", why)));
  useEffect(() => question.dispose, [question]);
  useCloseGuard(tabId, order, () => question.decide(needs()));
  return { asking, answer: question.answer };
}

const isOpen = (tabId: string) => useWorkspace.getState().tabs.some((tab) => tab.id === tabId);

async function askGuards(tabId: string, intent: CloseIntent): Promise<boolean> {
  const entries = [...(guards.get(tabId) ?? [])]
    .filter((entry) => intent === "close" || !CLOSE_ONLY.has(entry.order))
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
