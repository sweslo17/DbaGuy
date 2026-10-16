import { clamp } from "@/lib/math";

/**
 * The grid's selection model (pure): one rectangle between an anchor and a focus cell. The focus is the active cell
 * (outlined, followed by the keyboard, the value viewer and the record view); in "rows" / "columns" mode the rectangle
 * spans every column / row, so it grows with the loaded rows. "all" spans everything and only keeps an active cell.
 */
export interface CellPos {
  row: number;
  col: number;
}
export type SelectionMode = "cells" | "rows" | "columns" | "all";
export interface GridSelection {
  anchor: CellPos;
  focus: CellPos;
  mode: SelectionMode;
}
/** Inclusive bounds. */
export interface Rect {
  top: number;
  left: number;
  bottom: number;
  right: number;
}
export interface Dims {
  rows: number;
  cols: number;
}
export type MoveKey = "ArrowUp" | "ArrowDown" | "ArrowLeft" | "ArrowRight" | "Home" | "End" | "PageUp" | "PageDown";

export function rectOf(sel: GridSelection, dims: Dims): Rect {
  if (sel.mode === "all") return { top: 0, bottom: dims.rows - 1, left: 0, right: dims.cols - 1 };
  const top = Math.min(sel.anchor.row, sel.focus.row);
  const bottom = Math.max(sel.anchor.row, sel.focus.row);
  const left = Math.min(sel.anchor.col, sel.focus.col);
  const right = Math.max(sel.anchor.col, sel.focus.col);
  if (sel.mode === "rows") return { top, bottom, left: 0, right: dims.cols - 1 };
  if (sel.mode === "columns") return { top: 0, bottom: dims.rows - 1, left, right };
  return { top, bottom, left, right };
}

export const cellCount = (r: Rect) => Math.max(0, r.bottom - r.top + 1) * Math.max(0, r.right - r.left + 1);
export const selectCell = (pos: CellPos): GridSelection => ({ anchor: pos, focus: pos, mode: "cells" });
/** Moves the focus to `pos`; extending a select-all starts a rectangle at its active cell. */
export const extendTo = (sel: GridSelection, pos: CellPos): GridSelection =>
  sel.mode === "all" ? { anchor: sel.focus, focus: pos, mode: "cells" } : { ...sel, focus: pos };

/** Whole columns. The focus stays in the first row: the active cell is the column's top cell. */
export function selectColumns(anchorCol: number, focusCol: number): GridSelection {
  return { anchor: { row: 0, col: anchorCol }, focus: { row: 0, col: focusCol }, mode: "columns" };
}

/** Whole rows. The focus stays in the first column: the active cell is the row's first cell. */
export function selectRows(anchorRow: number, focusRow: number): GridSelection {
  return { anchor: { row: anchorRow, col: 0 }, focus: { row: focusRow, col: 0 }, mode: "rows" };
}

/** Everything, rows loaded later included; `active` stays the active cell. */
export function selectAll(active: CellPos): GridSelection {
  return { anchor: active, focus: active, mode: "all" };
}

export function move(
  sel: GridSelection,
  key: MoveKey,
  opts: { extend: boolean; toEdge: boolean; page: number },
  dims: Dims,
): GridSelection {
  const { row, col } = sel.focus;
  const lastRow = dims.rows - 1;
  const lastCol = dims.cols - 1;
  let next: CellPos;
  switch (key) {
    case "ArrowUp":
      next = { row: opts.toEdge ? 0 : row - 1, col };
      break;
    case "ArrowDown":
      next = { row: opts.toEdge ? lastRow : row + 1, col };
      break;
    case "ArrowLeft":
      next = { row, col: opts.toEdge ? 0 : col - 1 };
      break;
    case "ArrowRight":
      next = { row, col: opts.toEdge ? lastCol : col + 1 };
      break;
    case "Home":
      next = opts.toEdge ? { row: 0, col: 0 } : { row, col: 0 };
      break;
    case "End":
      next = opts.toEdge ? { row: lastRow, col: lastCol } : { row, col: lastCol };
      break;
    case "PageUp":
      next = { row: row - opts.page, col };
      break;
    case "PageDown":
      next = { row: row + opts.page, col };
      break;
  }
  next = { row: clamp(next.row, 0, lastRow), col: clamp(next.col, 0, lastCol) };
  return opts.extend ? extendTo(sel, next) : selectCell(next);
}

/** The selection inside `dims` (null when the grid is empty); the same object when nothing had to move. */
export function clampSelection(sel: GridSelection, dims: Dims): GridSelection | null {
  if (dims.rows === 0 || dims.cols === 0) return null;
  const inside = (p: CellPos) => p.row >= 0 && p.row < dims.rows && p.col >= 0 && p.col < dims.cols;
  if (inside(sel.anchor) && inside(sel.focus)) return sel;
  const fix = (p: CellPos): CellPos => ({ row: clamp(p.row, 0, dims.rows - 1), col: clamp(p.col, 0, dims.cols - 1) });
  return { ...sel, anchor: fix(sel.anchor), focus: fix(sel.focus) };
}
