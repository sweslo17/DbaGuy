import { createTabActionRegistry, type TabAction } from "@/lib/tabActions";

/** What the command palette can do with a table tab's staged edits (spec ⑤ §3.6.4, ruling H11 for undo / redo). */
export type TableEditActionId = "addRow" | "deleteRows" | "setNull" | "commit" | "discardAll" | "undo" | "redo";
export type TableEditActions = Record<TableEditActionId, TabAction>;

/** Where the tab's edits stand now: what its own controls are enabled by. */
export interface TableEditFacts {
  /** Rows can be edited now (the edits' `editable`: not while the rows are held, a commit runs or the key changed). */
  editable: boolean;
  /** The grid is on screen: the tab is active and its Data view shown (not Columns or DDL). */
  shown: boolean;
  /** The changes staged. */
  changes: number;
  /** Why Commit… cannot start now (the commit's `disabledReason`), or null. */
  commitBlocked: string | null;
  /** A commit is running: nothing changes the staging. */
  applying: boolean;
  /** The staging has a step to go back to (its `past`), and one to go forward to (its `future`). */
  undoable: boolean;
  redoable: boolean;
}

/**
 * The palette's table-edit actions: each enabled as its own control is, and run through the tab's own handler (H11:
 * the palette is the way to undo when WKWebView keeps ⌘Z for its Edit menu, A10). Add row, Delete selected rows and
 * Set NULL as the grid's keys and + Row, so only while the grid is on screen (they act on its selection or open an
 * editor in it); Commit changes… as the edit bar's Commit…; Discard all changes as its Discard… (shown with changes,
 * asking first when more than one would go); Undo and Redo as ⌘Z / ⇧⌘Z, which also take changes back while the rows
 * cannot be edited (L5), but never during a commit. Those four act on the staging, so also from Columns or DDL.
 */
export function tableEditActions(facts: TableEditFacts, run: Record<TableEditActionId, () => void>): TableEditActions {
  const { changes, applying } = facts;
  const onGrid = facts.editable && facts.shown;
  return {
    addRow: { enabled: onGrid, run: run.addRow },
    deleteRows: { enabled: onGrid, run: run.deleteRows },
    setNull: { enabled: onGrid, run: run.setNull },
    commit: { enabled: changes > 0 && facts.commitBlocked === null, run: run.commit },
    discardAll: { enabled: changes > 0 && !applying, run: run.discardAll },
    undo: { enabled: facts.undoable && !applying, run: run.undo },
    redo: { enabled: facts.redoable && !applying, run: run.redo },
  };
}

/*
 * The mounted table tabs' edit actions, by tab id. The palette never stages anything itself: it calls the same
 * functions as the grid's keys and the edit bar, so the gate, the confirmations and the store's limits apply alike.
 */
const registry = createTabActionRegistry<TableEditActionId>();

/** The table tab's edit actions as they are now; null when no table tab with that id is mounted. */
export const readTableEditActions = registry.read;
/** Registers the tab's edit actions while its Data view is mounted; the latest `actions` passed are the ones read. */
export const useTableEditActions = registry.use;
