import { createTabActionRegistry, type TabAction } from "@/lib/tabActions";

/** What the command palette can do with a query tab (ruling R-02c-15). */
export type QueryTabActionId =
  | "run"
  | "runAll"
  | "explain"
  | "runWithProfile"
  | "format"
  | "toggleAutocommit"
  | "commit"
  | "rollback";

/** One of them: whether the toolbar button or keymap would allow it now, and the handler they call. */
export type QueryTabAction = TabAction;
export type QueryTabActions = Record<QueryTabActionId, QueryTabAction>;

/*
 * The mounted query tabs' actions, by tab id. The palette never runs or ends anything itself: it calls the same
 * functions as the tab's toolbar and keymap, so confirmations, `canRun` and the transaction state apply alike.
 */
const registry = createTabActionRegistry<QueryTabActionId>();

/** The tab's actions as they are now; null when no query tab with that id is mounted. */
export const readQueryTabActions = registry.read;
/** Registers the tab's actions while the component is mounted; the latest `actions` passed are the ones read. */
export const useQueryTabActions = registry.use;
