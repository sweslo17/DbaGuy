import { clamp } from "@/lib/math";
import { rowKeyOf } from "../edits/overlay";
import type { RowKey } from "../edits/staging";
import type { CellPos, GridSelection } from "../grid/selection";

/*
 * The grid's place by key (05b K14, K14b). A re-run of the same browse (Refresh, the re-read after a commit) keeps the
 * scroll and the selection (D-05-12), but by position they would show other rows when rows moved meanwhile: deleted or
 * added above, or (PostgreSQL's unsorted order) an UPDATE moved the row. So before the new rows take over, the place is
 * captured by key: the row the view is anchored on (the active cell's row when it is in view, else the first row in
 * view) with its offset on screen, and the selection with its active row's key. Once the new rows are in, each key is
 * found again and its row put back: the view's row at the same offset, the selection on the same row.
 *
 * A key identifies a row only when one row has it: rows can share a key (NULL in a nullable UNIQUE, a SQLite PK
 * holding NULL, binary keys longer than their preview), and a key that more than one row has, before or after, counts
 * as not found (the overlay's `repeated` rule). A view's row not found leaves the scroll where it is (the position,
 * Task 6's rule). A selection whose row is not found is cleared (K14b): an active cell left on another row is a
 * wrong-row edit waiting to happen (Q3). Pure.
 *
 * Indexes are display rows: `leading` new rows (staged on top, Task 9's overlay) come before the loaded rows, and only
 * a loaded row has a key; keys are read from the loaded rows themselves, never from a display row (a staged key cell
 * is not the row's key). Offsets and scroll positions are px of the rows' viewport, below the sticky header.
 */

export interface Anchor {
  /** The new rows above the loaded rows when it was captured. */
  readonly leading: number;
  /** The row kept in place on screen, and how far its top was below the viewport's top (negative: partly above). */
  readonly view: { readonly key: RowKey; readonly offset: number } | null;
  /**
   * The selection as made, the display row of its active cell, and that row's key: null when no key identifies it
   * (a new row, or a key another row has too). The whole part is null when nothing is selected, or whole columns or
   * everything are: those stay as they are.
   */
  readonly selection: { readonly selection: GridSelection; readonly row: number; readonly key: RowKey | null } | null;
}

export interface AnchorInput {
  /** The loaded rows shown now. */
  readonly rows: readonly (readonly unknown[])[];
  /** Where the key's cells are in a row (`resultColumns(columns, target.key).keyIndexes`). */
  readonly keyIndexes: readonly number[];
  /** The selection as made (display rows), not clamped to the rows shown later. */
  readonly selection: GridSelection | null;
  readonly scrollTop: number;
  /** The height of the rows' viewport (below the header). */
  readonly viewportHeight: number;
  readonly rowHeight: number;
  /** New rows shown above the loaded rows now (0 until Task 9's overlay is wired). */
  readonly leading: number;
}

/** Where a selection is placed by a row: its active cell's, unless it is whole columns or everything. */
const placedByRow = (selection: GridSelection) => selection.mode === "cells" || selection.mode === "rows";

const REPEATED = -1;

/** Each wanted key's display row among `rows`, or REPEATED when more than one row has it. Every row is read. */
function locate(
  rows: readonly (readonly unknown[])[],
  keyIndexes: readonly number[],
  leading: number,
  wanted: ReadonlySet<RowKey>,
): ReadonlyMap<RowKey, number> {
  const found = new Map<RowKey, number>();
  if (wanted.size === 0) return found;
  for (let index = 0; index < rows.length; index += 1) {
    const key = rowKeyOf(rows[index], keyIndexes);
    if (wanted.has(key)) found.set(key, found.has(key) ? REPEATED : leading + index);
  }
  return found;
}

/** The row a key identifies in `found`, or null (not there, or more than one row has it). */
function rowOf(found: ReadonlyMap<RowKey, number>, key: RowKey): number | null {
  const row = found.get(key);
  return row === undefined || row === REPEATED ? null : row;
}

/** The grid's place by key, before its rows are replaced; null when there is no loaded row to anchor on. */
export function captureAnchor(input: AnchorInput): Anchor | null {
  const { rows, keyIndexes, selection, scrollTop, viewportHeight, rowHeight, leading } = input;
  if (rows.length === 0 || rowHeight <= 0) return null;
  const end = leading + rows.length;
  const offsetOf = (row: number) => row * rowHeight - scrollTop;
  const inView = (row: number) => offsetOf(row) + rowHeight > 0 && offsetOf(row) < viewportHeight;
  const keyAt = (row: number) => rowKeyOf(rows[row - leading], keyIndexes);

  const active = selection && placedByRow(selection) && selection.focus.row < end ? selection.focus.row : -1;
  const activeKey = active >= leading ? keyAt(active) : null;
  // The view's row: the active one when in view, else the first loaded row at least partly in view (or the first
  // loaded row, below new rows that fill the view); the first of them that one row alone has.
  const first = clamp(Math.floor(scrollTop / rowHeight), leading, end - 1);
  const candidates = activeKey !== null && inView(active) && active !== first ? [active, first] : [first];
  const keys = candidates.map(keyAt);
  const found = locate(rows, keyIndexes, leading, new Set(activeKey === null ? keys : [...keys, activeKey]));
  const viewAt = candidates.findIndex((_, i) => rowOf(found, keys[i]) !== null);
  return {
    leading,
    view: viewAt < 0 ? null : { key: keys[viewAt], offset: offsetOf(candidates[viewAt]) },
    selection:
      selection && active >= 0
        ? { selection, row: active, key: activeKey !== null && rowOf(found, activeKey) !== null ? activeKey : null }
        : null,
  };
}

export interface Placement {
  /** The scroll position that puts the view's row back at its offset, within the content; null: keep the position. */
  readonly scrollTop: number | null;
  /** The selection moved with its row; null: no move (see `clearSelection`). */
  readonly selection: GridSelection | null;
  /** The selection's row is gone (not found, or its key is not one row's): no cell stays active (K14b). */
  readonly clearSelection: boolean;
}

export interface PlaceInput {
  /** The loaded rows that replaced the anchored ones. */
  readonly rows: readonly (readonly unknown[])[];
  readonly keyIndexes: readonly number[];
  readonly rowHeight: number;
  /** New rows shown above the loaded rows now. */
  readonly leading: number;
  /** The largest scroll position the new content allows. */
  readonly maxScrollTop: number;
}

function shifted(selection: GridSelection, by: number): GridSelection {
  const move = (pos: CellPos): CellPos => ({ row: Math.max(0, pos.row + by), col: pos.col });
  return by === 0 ? selection : { ...selection, anchor: move(selection.anchor), focus: move(selection.focus) };
}

/** The selection's place over the new rows: moved with its row, kept, or cleared. */
function placeSelection(
  anchor: Anchor,
  found: ReadonlyMap<RowKey, number>,
  leading: number,
): Pick<Placement, "selection" | "clearSelection"> {
  const held = anchor.selection;
  if (!held) return { selection: null, clearSelection: false }; // nothing selected, or not by a row: as it is
  if (held.row < anchor.leading) {
    // A new row: still there while the new rows are the same (a Refresh); a commit took them off the top.
    return { selection: null, clearSelection: leading !== anchor.leading };
  }
  const row = held.key === null ? null : rowOf(found, held.key);
  return row === null
    ? { selection: null, clearSelection: true }
    : { selection: shifted(held.selection, row - held.row), clearSelection: false };
}

/** Puts `anchor` back over the new rows, each of its keys found by one pass of `rowKeyOf` over all of them. */
export function placeAnchor(anchor: Anchor, input: PlaceInput): Placement {
  const { rows, keyIndexes, rowHeight, leading, maxScrollTop } = input;
  const wanted = new Set<RowKey>();
  if (anchor.view) wanted.add(anchor.view.key);
  if (anchor.selection && anchor.selection.key !== null) wanted.add(anchor.selection.key);
  const found = locate(rows, keyIndexes, leading, wanted);
  const viewRow = anchor.view ? rowOf(found, anchor.view.key) : null;
  const scrollTop =
    anchor.view && viewRow !== null
      ? clamp(viewRow * rowHeight - anchor.view.offset, 0, Math.max(0, maxScrollTop))
      : null;
  return { scrollTop, ...placeSelection(anchor, found, leading) };
}
