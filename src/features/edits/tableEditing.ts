import type { EditColumn } from "@/lib/api/types";
import { plural } from "@/lib/format";
import type { DispatchResult } from "@/stores/stagedEdits";
import type { RowMark } from "../grid/marks";
import type { GridSelection, Rect } from "../grid/selection";
import { editText, type CellInput } from "./cells";
import { editStart, KEY_INCOMPLETE, MARKED_DELETED, notWritable, type EditCellInput, type EditStart, type RowState } from "./editing";
import type { GridEditKey } from "./editKeys";
import { REPEATED_KEY } from "./overlay";
import { UNSAVEABLE_HINT, type SavedEdits } from "./savedEdits";
import {
  MAX_CHANGES,
  own,
  restoresRows,
  type CellTarget,
  type RowKey,
  type RowRef,
  type StagedEdits,
  type StagedInsert,
} from "./staging";

/*
 * What a table-data tab's grid lets the user stage, and when (spec ⑤ §3.6.3–§3.6.6; rulings H13, H15, K6, K19, L5), as
 * pure functions: the mode the grid is in, which keys and menu items it offers in it, why an edit cannot start, which
 * cells and rows a selection gives (never more than MAX_CHANGES rows: H15), how the edit target's read is shown. The
 * hook `useTableEdits` asks these and does the I/O.
 */

export const TOO_MANY_CHANGES = `At most ${MAX_CHANGES.toLocaleString()} changes are committed at once: use SQL for this many rows`;
export const KEY_CHANGED = "The table's key changed since these changes were staged: commit or discard them first";
export const TOO_MANY_ROWS = `Select at most ${MAX_CHANGES.toLocaleString()} rows`;
/** The rows shown are held while a re-run loads them again, or not there yet: nothing new is staged on them (K19). */
export const ROWS_LOADING = "Wait for the rows to load";
/**
 * The grid draws the staging from before the last commit (its afterglow) and that commit's re-read was cut short (K17):
 * nothing loads by itself, and nothing new is staged until a Refresh reads the rows again (Task 10).
 */
export const REREAD_FIRST = "Refresh first: the rows shown are from before the last commit";
/** The edit target has not been read yet. */
export const CHECKING = "Checking whether this table's rows can be edited";
/** A target that is not editable and gives no reason. */
export const NOT_EDITABLE = "Rows of this table cannot be edited";
export const TOO_LARGE_TO_KEEP = "Too large to keep across a restart";
export const KEEP_MINE_TITLE = "Check the staged values against the server's values now";

/** The palette's Delete selected rows and Set NULL with nothing selected in the grid. */
export const NOTHING_SELECTED = "Select cells in the grid first";
/** Closing the tab is refused while a commit runs (R-05-11): its dialog is modal, so only a profile deletion gets here. */
export const CLOSE_WHILE_COMMITTING = "A commit is running: wait for it to end, or cancel it in its dialog";
/** The export dialog's line while changes are staged (spec ⑤ §3.6.6): it writes what the server holds. */
export const NOT_IN_EXPORT = "Staged changes are not committed and are not in the export";

export const COPY_TEXT = "Copy text";
export const COPY_FAILED = "Could not copy the text";

export const notNullSkipped = (cells: number) => `Skipped ${plural(cells, "cell")} of NOT NULL columns`;
export const notNullColumn = (column: string) => `${column} is NOT NULL`;

/** Why staging is limited now; each null when it is not. */
export interface EditGate {
  /** A commit is running (Task 10): nothing changes the staging. */
  locked: string | null;
  /** The rows are held while a re-run loads, or not there yet (K19): nothing new is staged, what is staged may go. */
  waiting: string | null;
  /** The table's rows cannot be edited: its target says so, has not been read, or the changes are on another key (H13). */
  readOnly: string | null;
}

/**
 * What the grid offers: "edit" everything; "revert" only taking staged changes back (Revert, Restore, undo: the table
 * cannot be edited now, L5, or its rows are held, K19); "locked" nothing (a commit runs).
 */
export type EditMode = "edit" | "revert" | "locked";

export function editMode(gate: EditGate): EditMode {
  if (gate.locked) return "locked";
  return gate.waiting || gate.readOnly ? "revert" : "edit";
}

/**
 * Why an action may not be staged now, or null (a table that cannot be edited says so before rows that are loading).
 * `adds`: it stages something new (the store's `adds`). `endingEdit`: it stages what an editor held when it ended
 * (H12): that edit began on rows that could be edited, so only a running commit refuses it (and the store, for too many
 * changes or another key). Taking staged changes back is never refused but by a commit.
 */
export function stageRefusal(gate: EditGate, adds: boolean, endingEdit = false): string | null {
  if (gate.locked) return gate.locked;
  if (!adds || endingEdit) return null;
  return gate.readOnly ?? gate.waiting;
}

/** Why the store refused a dispatch (`dispatchStaged`), or null when it did not. */
export function dispatchRefusal(outcome: DispatchResult | null): string | null {
  if (outcome === "too_many") return TOO_MANY_CHANGES;
  return outcome === "key_changed" ? KEY_CHANGED : null;
}

/**
 * What a refused staging of an editor's text says (ruling K20): the edit has ended (the editors never reopen), so the
 * text is offered to copy (`COPY_TEXT`) rather than lost with only a toast.
 */
export const typedNotStaged = (why: string): string => `${why.replace(/\.$/, "")}. What you typed was not staged.`;

/** Where a row of the grid stands for editing: `editing.ts`'s states, and a key that more than one loaded row has (K6). */
export type GridRowState = RowState | "repeated";

/** Whether every key cell was loaded whole: NULL, a cut text or bytes the grid holds only the start of identify nothing. */
export function keyComplete(key: Readonly<Record<string, unknown>>): boolean {
  return Object.values(key).every((cell) => cell !== null && cell !== undefined && editText(cell).complete);
}

/** `repeated`: the keys more than one loaded row has (the binding's); no staged change binds to them. */
export function rowStateOf(ref: RowRef, staged: StagedEdits | null, repeated: ReadonlySet<RowKey> | null): GridRowState {
  if (ref.kind === "insert") return "new";
  if (repeated?.has(ref.rowKey)) return "repeated";
  if (staged && Object.hasOwn(staged.deletes, ref.rowKey)) return "deleted";
  return keyComplete(ref.key) ? "loaded" : "key_incomplete";
}

/** The new row of a staging with this id. */
const insertOf = (staged: StagedEdits | null, id: string): StagedInsert | undefined =>
  staged?.inserts.find((insert) => insert.id === id);

/** A cell's staged value, if it has one (by own property: a column may be named `constructor` or `__proto__`, K2). */
export function stagedInputOf(staged: StagedEdits | null, ref: RowRef, column: string): CellInput | undefined {
  if (!staged) return undefined;
  if (ref.kind === "insert") {
    const insert = insertOf(staged, ref.id);
    return insert && own(insert.values, column);
  }
  const update = own(staged.updates, ref.rowKey);
  return update && own(update.set, column);
}

/**
 * The loaded value a changed cell's edit was staged against (the drawer's "Was:"), else the cell as loaded now: they
 * differ once the server changed it (a conflict).
 */
export function wasValueOf(staged: StagedEdits | null, ref: RowRef, column: string, loaded: unknown): unknown {
  if (ref.kind === "insert" || !staged) return loaded;
  const update = own(staged.updates, ref.rowKey);
  return update && Object.hasOwn(update.set, column) ? own(update.original, column) : loaded;
}

/**
 * How an edit starts on a cell now, or why it cannot: a running commit, a table that cannot be edited, held rows, a
 * repeated key (K6), then the cell's own reasons (`editStart`).
 */
export function startEdit(
  gate: EditGate,
  row: GridRowState,
  cell: Omit<EditCellInput, "row">,
): EditStart | { refuse: string } {
  const paused = gate.locked ?? gate.readOnly ?? gate.waiting;
  if (paused) return { refuse: paused };
  if (row === "repeated") return { refuse: REPEATED_KEY };
  return editStart({ ...cell, row });
}

/** What a key on the focused grid does in `mode`: anything when editing, only undo and redo otherwise (L5, K19). */
export function gridKeyAction(key: GridEditKey | null, mode: EditMode): GridEditKey | null {
  if (!key) return null;
  return mode === "edit" || key === "undo" || key === "redo" ? key : null;
}

/** The display rows `rect` covers, top first; null when they are more than MAX_CHANGES (H15: nothing is built then). */
export function selectedRows(rect: Rect | null): number[] | null {
  if (!rect) return [];
  const count = rect.bottom - rect.top + 1;
  if (count > MAX_CHANGES) return null;
  return Array.from({ length: Math.max(0, count) }, (_, i) => rect.top + i);
}

/** What the picks read of the grid: its rows as staging knows them, and its columns as the table knows them. */
export interface GridLookup {
  /** The row at a display index, or null (past the end, or no key in the result). */
  refAt(row: number): RowRef | null;
  stateOf(ref: RowRef): GridRowState;
  /** The result's column at an index: its name, and the table's column of that name (undefined: not the table's). */
  columnAt(col: number): { name: string; column: EditColumn | undefined } | null;
  /** The loaded cell at a display position (undefined on a new row). */
  loadedAt(row: number, col: number): unknown;
}

/** Why a row gives nothing to a pick, or null: its key repeats or is incomplete; `writable` also skips a marked row. */
function rowSkipped(state: GridRowState, writable: boolean): string | null {
  if (state === "repeated") return REPEATED_KEY;
  if (state === "key_incomplete") return KEY_INCOMPLETE;
  return writable && state === "deleted" ? MARKED_DELETED : null;
}

export interface PickedCells {
  cells: CellTarget[];
  /** Cells of NOT NULL columns left out (Set NULL). */
  notNull: number;
  /** Why the first cell left out was (a read-only column, a marked or repeated row…): said when nothing was picked. */
  refusal: string | null;
}

/**
 * The cells of a selection an action takes; null when it covers more than MAX_CHANGES rows (H15). `writable`: only
 * cells that can be written, on rows that are not marked for deletion (Set NULL, Use default); without it, any cell of
 * a row a change can be on (the reverts). `nullable`: NOT NULL columns are counted apart (Set NULL).
 */
export function pickCells(
  rect: Rect | null,
  lookup: GridLookup,
  options: { writable: boolean; nullable: boolean },
): PickedCells | null {
  const rows = selectedRows(rect);
  if (!rows) return null;
  const picked: PickedCells = { cells: [], notNull: 0, refusal: null };
  if (!rect) return picked;
  for (const row of rows) {
    const ref = lookup.refAt(row);
    if (!ref) continue;
    const skipped = rowSkipped(lookup.stateOf(ref), options.writable);
    if (skipped) {
      picked.refusal ??= skipped;
      continue;
    }
    for (let col = rect.left; col <= rect.right; col += 1) {
      const at = lookup.columnAt(col);
      if (!at) continue;
      if (options.writable && !at.column?.writable) {
        picked.refusal ??= notWritable(at.name, at.column);
        continue;
      }
      if (options.nullable && at.column && !at.column.nullable) {
        picked.notNull += 1;
        continue;
      }
      picked.cells.push({ row: ref, column: at.name, original: lookup.loadedAt(row, col) });
    }
  }
  return picked;
}

export interface PickedRows {
  rows: RowRef[];
  /** Why the first row left out was (its key repeats or is incomplete): said when nothing was picked. */
  refusal: string | null;
}

/** The rows of a selection a row action takes (Delete, Restore, Revert row); null beyond MAX_CHANGES rows (H15). */
export function pickRows(rect: Rect | null, lookup: GridLookup): PickedRows | null {
  const indexes = selectedRows(rect);
  if (!indexes) return null;
  const picked: PickedRows = { rows: [], refusal: null };
  for (const index of indexes) {
    const ref = lookup.refAt(index);
    if (!ref) continue;
    const skipped = rowSkipped(lookup.stateOf(ref), false);
    if (skipped) picked.refusal ??= skipped;
    else picked.rows.push(ref);
  }
  return picked;
}

/** What the menu needs to know of a selection's rows. */
export interface SelectionFacts {
  rows: number;
  /** Every row is a loaded row marked for deletion: Delete restores them (the store's `restoresRows`). */
  allDeleted: boolean;
  /** A row is new, changed or marked. */
  anyChanged: boolean;
  /** A cell of `columns` holds a staged value (a marked row's hidden UPDATE does not count: Revert cell skips it). */
  anyStagedCell: boolean;
}

export function selectionFacts(
  rows: readonly RowRef[],
  columns: readonly string[],
  staged: StagedEdits | null,
): SelectionFacts {
  if (!staged) return { rows: rows.length, allDeleted: false, anyChanged: false, anyStagedCell: false };
  const inserts = new Map(staged.inserts.map((insert) => [insert.id, insert]));
  let anyChanged = false;
  let anyStagedCell = false;
  for (const ref of rows) {
    if (ref.kind === "insert") {
      const insert = inserts.get(ref.id);
      if (!insert) continue;
      anyChanged = true;
      anyStagedCell ||= columns.some((column) => own(insert.values, column) !== undefined);
      continue;
    }
    const update = own(staged.updates, ref.rowKey);
    const deleted = Object.hasOwn(staged.deletes, ref.rowKey);
    anyChanged ||= deleted || update !== undefined;
    if (update && !deleted) anyStagedCell ||= columns.some((column) => Object.hasOwn(update.set, column));
    if (anyChanged && anyStagedCell) break;
  }
  return { rows: rows.length, allDeleted: restoresRows(staged, rows), anyChanged, anyStagedCell };
}

/** A row with a staged value the server has changed since (a conflict corner: the overlay decides it). */
export const rowHasConflict = (mark: RowMark | undefined): boolean =>
  mark?.kind === "changed" && Object.values(mark.cells ?? {}).some((cell) => !!cell.conflict);

export type EditMenuId =
  | "edit"
  | "setNull"
  | "useDefault"
  | "revertCell"
  | "keepMine"
  | "revertRow"
  | "deleteRows"
  | "restoreRows"
  | "addRow";

export interface EditMenuEntry {
  id: EditMenuId;
  disabled?: boolean;
  /** Why it is disabled, or what it does. */
  title?: string;
  /** Its key does not do it here (the grid takes only ⌘Z / ⇧⌘Z when it cannot be edited now, L5): no shortcut shown. */
  keyless?: boolean;
}

/** How each item reads (spec §3.6.4). */
export const EDIT_MENU_ITEMS: Readonly<
  Record<EditMenuId, { label: string; shortcut?: string; danger?: boolean; separatorBefore?: boolean }>
> = {
  edit: { label: "Edit value", shortcut: "↵" },
  setNull: { label: "Set NULL", shortcut: "⌥⌘N" },
  useDefault: { label: "Use default" },
  revertCell: { label: "Revert cell" },
  keepMine: { label: "Keep mine" },
  revertRow: { label: "Revert row" },
  deleteRows: { label: "Delete rows", shortcut: "⌘⌫", danger: true },
  restoreRows: { label: "Restore rows", shortcut: "⌘⌫" },
  addRow: { label: "Add row", separatorBefore: true },
};

export interface EditMenuInput {
  mode: EditMode;
  /** The changes staged now. */
  changes: number;
  /** The cell the menu was opened on (a cell's menu); null for the row numbers' and the corner's. */
  at: {
    /** Why the cell cannot be edited (`startEdit`), or null. */
    refusal: string | null;
    /** It is a new row's cell (Use default). */
    isNew: boolean;
    /** Its row has a conflict (Keep mine). */
    conflict: boolean;
  } | null;
  /** The selection's rows; null when they are more than MAX_CHANGES (H15). */
  selection: SelectionFacts | null;
}

/**
 * The items leading the grid's cell and row-number menus (spec §3.6.4), in order: Edit value, Set NULL, Use default (a
 * new row), Revert cell, Keep mine (a conflict), Revert row, Delete rows or Restore rows, Add row. When the table cannot
 * be edited now (another key, held rows) only the reverts of what is staged, Restore included (L5, K19); none while a
 * commit runs. An item over a selection of more than MAX_CHANGES rows is disabled (H15).
 */
export function editMenu({ mode, changes, at, selection }: EditMenuInput): EditMenuEntry[] {
  if (mode === "locked" || (mode === "revert" && changes === 0)) return [];
  /** An item over the selection: off when the selection is too large or `enabled` says so. */
  const over = (id: EditMenuId, enabled: (facts: SelectionFacts) => boolean, keyless = false): EditMenuEntry => {
    if (!selection) return { id, disabled: true, title: TOO_MANY_ROWS };
    const entry: EditMenuEntry = enabled(selection) ? { id } : { id, disabled: true };
    return keyless ? { ...entry, keyless } : entry;
  };
  const items: EditMenuEntry[] = [];
  if (mode === "revert") {
    if (at) items.push(over("revertCell", (s) => s.anyStagedCell));
    items.push(over("revertRow", (s) => s.anyChanged));
    if (selection?.allDeleted) items.push({ id: "restoreRows", keyless: true });
    return items;
  }
  if (at) {
    items.push(at.refusal ? { id: "edit", disabled: true, title: at.refusal } : { id: "edit" });
    items.push(over("setNull", (s) => s.rows > 0));
    if (at.isNew) items.push(over("useDefault", (s) => s.rows > 0));
    items.push(over("revertCell", (s) => s.anyStagedCell));
    if (at.conflict) items.push({ id: "keepMine", title: KEEP_MINE_TITLE });
  }
  items.push(over("revertRow", (s) => s.anyChanged));
  items.push(selection?.allDeleted ? { id: "restoreRows" } : over("deleteRows", (s) => s.rows > 0));
  items.push({ id: "addRow" });
  return items;
}

/** How the edit target's read shows: nothing (no row editing), checking, read, an error with Retry, or an unlock (L13). */
export type TargetView = "none" | "checking" | "read" | "error" | "unlock";

export interface TargetRead {
  /** The engine edits rows (`row_editing`). */
  rowEditing: boolean;
  status: "pending" | "error" | "success";
  fetching: boolean;
  /** The read failed for want of a password. */
  passwordRequired: boolean;
  /** It failed after the session opened: a password the session needed was given by then. */
  failedSinceOpen: boolean;
}

/**
 * A target being read again keeps what was read (Refresh), and one being retried says nothing until it answers. A read
 * that wanted the password before the session opened is the session's to ask for: the session's own unlock gives it
 * and the read is made again (6b). Only one that still wants it after the session opened asks itself (L13: an unlock,
 * never a Retry that would only fail the same way).
 */
export function targetView(read: TargetRead): TargetView {
  if (!read.rowEditing) return "none";
  if (read.status === "success") return "read";
  if (read.status === "pending" || read.fetching) return "checking";
  if (!read.passwordRequired) return "error";
  return read.failedSinceOpen ? "unlock" : "checking";
}

/** Why the staged changes are not kept across a restart (`savedEdits`, ruling K5), or null. */
export function notKeptHint(saved: SavedEdits): string | null {
  if (saved.tooLarge) return TOO_LARGE_TO_KEEP;
  return saved.unsaveable ? UNSAVEABLE_HINT[saved.unsaveable] : null;
}

/** Discard… asks first only when more than one change would go (⌘Z brings them back either way). */
export const asksBeforeDiscard = (changes: number): boolean => changes > 1;

/** Closing a tab with changes staged (R-05-11): they go with the tab, and nothing brings them back. */
export const closeQuestion = (changes: number, table: string): string =>
  `Discard ${plural(changes, "staged change")} to ${table}?`;

/** Discard…: the same question, and ⌘Z brings them back. */
export const discardQuestion = (changes: number, table: string): string =>
  `${closeQuestion(changes, table)} ⌘Z brings them back.`;

/** The new rows drawn on top of the loaded rows: how many, and their ids from the top. */
export interface ShownTop {
  leading: number;
  newIds: readonly string[];
}

/**
 * The same loaded rows, or a page more of them (Load more appends; a re-run brings other row objects, and the anchor of
 * `useAnchoredRerun` puts the grid back then).
 */
export const sameLoadedRows = (before: readonly unknown[], now: readonly unknown[]): boolean =>
  before === now || (before.length > 0 && now.length >= before.length && now[0] === before[0]);

/**
 * Where a display row of `before` is in `after`: a loaded row moves with the new rows above it, a new row is found by
 * its id; null when it is gone.
 */
export function rowAfterTop(row: number, before: ShownTop, after: ShownTop): number | null {
  if (row >= before.leading) return row - before.leading + after.leading;
  const index = after.newIds.indexOf(before.newIds[row]);
  return index < 0 ? null : index;
}

/**
 * The grid's place once the new rows on top changed over the same loaded rows (a new row added, undone, reverted,
 * deleted, discarded): the selection stays on its rows and the loaded rows stay where they were on screen, the scroll
 * moving by the rows added or taken away above them; a view at the very top stays there, so new rows show. A selection
 * on a new row that went is cleared, never moved onto another row (K14b). Whole columns and select-all have no row.
 */
export function placeAfterTop(
  view: { selection: GridSelection | null; scrollTop: number },
  before: ShownTop,
  after: ShownTop,
  rowHeight: number,
): { selection: GridSelection | null; clearSelection: boolean; scrollTop: number } {
  const moved = view.scrollTop + (after.leading - before.leading) * rowHeight;
  const scrollTop = view.scrollTop <= 0 ? 0 : Math.max(0, moved);
  const selection = view.selection;
  if (!selection || selection.mode === "columns" || selection.mode === "all") {
    return { selection: null, clearSelection: false, scrollTop };
  }
  const anchor = rowAfterTop(selection.anchor.row, before, after);
  const focus = rowAfterTop(selection.focus.row, before, after);
  if (anchor === null || focus === null) return { selection: null, clearSelection: true, scrollTop };
  return {
    selection: { ...selection, anchor: { ...selection.anchor, row: anchor }, focus: { ...selection.focus, row: focus } },
    clearSelection: false,
    scrollTop,
  };
}
