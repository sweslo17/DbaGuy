import { clamp } from "@/lib/math";
import { rowKeyOf } from "../edits/overlay";
import type { RowKey } from "../edits/staging";
import type { CellPos, GridSelection } from "../grid/selection";

/*
 * The grid's place by key (05b K14). A re-run of the same browse (Refresh, the re-read after a commit) keeps the scroll
 * and the selection (D-05-12), but by position they would show other rows when rows moved meanwhile: deleted or added
 * above, or (PostgreSQL's unsorted order) an UPDATE moved the row. So before the new rows take over, the place is
 * captured by key: the row the view is anchored on (the active cell's row when it is in view, else the first row in
 * view) with its offset on screen, and the selection with its active row's key. Once the new rows are in, each key is
 * found again and its row put back: the view's row at the same offset, the selection on the same row. A key no longer
 * among the rows leaves that part where it is (the position, Task 6's rule). Pure.
 *
 * Indexes are display rows: `leading` new rows (staged on top, Task 9's overlay) come before the loaded rows, and only
 * a loaded row has a key; keys are read from the loaded rows themselves, never from a display row (a staged key cell
 * is not the row's key). Offsets and scroll positions are px of the rows' viewport, below the sticky header.
 */

export interface Anchor {
  /** The row kept in place on screen, and how far its top was below the viewport's top (negative: partly above). */
  readonly view: { readonly key: RowKey; readonly offset: number } | null;
  /** The selection as made, the display row of its active cell and that row's key; null when no loaded row holds it. */
  readonly selection: { readonly selection: GridSelection; readonly row: number; readonly key: RowKey } | null;
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
  /** New rows shown above the loaded rows (0 until Task 9's overlay is wired). */
  readonly leading: number;
}

/** Where a selection is placed by a row: its active cell's, unless it is whole columns or everything. */
const placedByRow = (selection: GridSelection) => selection.mode === "cells" || selection.mode === "rows";

/** The grid's place by key, before its rows are replaced; null when there is no loaded row to anchor on. */
export function captureAnchor(input: AnchorInput): Anchor | null {
  const { rows, keyIndexes, selection, scrollTop, viewportHeight, rowHeight, leading } = input;
  if (rows.length === 0 || rowHeight <= 0) return null;
  const end = leading + rows.length;
  const offsetOf = (row: number) => row * rowHeight - scrollTop;
  const inView = (row: number) => offsetOf(row) + rowHeight > 0 && offsetOf(row) < viewportHeight;
  const keyAt = (row: number) => rowKeyOf(rows[row - leading], keyIndexes);

  const active = selection && placedByRow(selection) ? selection.focus.row : -1;
  const selected =
    selection && active >= leading && active < end ? { selection, row: active, key: keyAt(active) } : null;
  // The first loaded row at least partly in view (or the first loaded row, below new rows that fill the view).
  const first = clamp(Math.floor(scrollTop / rowHeight), leading, end - 1);
  const viewRow = selected && inView(selected.row) ? selected.row : first;
  return { view: { key: keyAt(viewRow), offset: offsetOf(viewRow) }, selection: selected };
}

export interface Placement {
  /** The scroll position that puts the view's row back at its offset, within the content; null: keep the position. */
  readonly scrollTop: number | null;
  /** The selection moved with its row; null: keep it where it is. */
  readonly selection: GridSelection | null;
}

export interface PlaceInput {
  /** The loaded rows that replaced the anchored ones. */
  readonly rows: readonly (readonly unknown[])[];
  readonly keyIndexes: readonly number[];
  readonly rowHeight: number;
  readonly leading: number;
  /** The largest scroll position the new content allows. */
  readonly maxScrollTop: number;
}

function shifted(selection: GridSelection, by: number): GridSelection {
  const move = (pos: CellPos): CellPos => ({ row: Math.max(0, pos.row + by), col: pos.col });
  return by === 0 ? selection : { ...selection, anchor: move(selection.anchor), focus: move(selection.focus) };
}

/** Puts `anchor` back over the new rows: each of its keys found by one pass of `rowKeyOf` (stopping once both are). */
export function placeAnchor(anchor: Anchor, input: PlaceInput): Placement {
  const { rows, keyIndexes, rowHeight, leading, maxScrollTop } = input;
  const wanted = new Set<RowKey>();
  if (anchor.view) wanted.add(anchor.view.key);
  if (anchor.selection) wanted.add(anchor.selection.key);
  const found = new Map<RowKey, number>();
  for (let index = 0; index < rows.length && found.size < wanted.size; index += 1) {
    const key = rowKeyOf(rows[index], keyIndexes);
    if (wanted.has(key) && !found.has(key)) found.set(key, leading + index);
  }
  const viewRow = anchor.view ? found.get(anchor.view.key) : undefined;
  const selectedRow = anchor.selection ? found.get(anchor.selection.key) : undefined;
  return {
    scrollTop:
      anchor.view && viewRow !== undefined
        ? clamp(viewRow * rowHeight - anchor.view.offset, 0, Math.max(0, maxScrollTop))
        : null,
    selection:
      anchor.selection && selectedRow !== undefined
        ? shifted(anchor.selection.selection, selectedRow - anchor.selection.row)
        : null,
  };
}
