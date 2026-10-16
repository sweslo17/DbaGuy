import { useMemo } from "react";
import { clamp } from "@/lib/math";
import { HEADER_HEIGHT, ROW_HEIGHT } from "./columns";
import type { ColumnLayout } from "./layout";
import type { CellPos } from "./selection";

/** Where the grid's parts sit, for mapping a pointer to a cell without asking the DOM (virtualized cells come and go). */
export interface GridGeometry {
  rowCount: number;
  colCount: number;
  rowNumberWidth: number;
  /** Row numbers plus pinned columns: unpinned columns scroll under this. */
  fixedWidth: number;
  pinnedCount: number;
  pinnedLeft: number[];
  widths: number[];
  /** Content x of the right edge of each unpinned column (index `col - pinnedCount`). */
  unpinnedEnds: number[];
}

/** A point inside the scroll area (relative to its top-left corner) with the area's scroll state. */
export interface ViewportPoint {
  x: number;
  y: number;
  /** Client size: scrollbars excluded. */
  width: number;
  height: number;
  scrollLeft: number;
  scrollTop: number;
}

export type GridHit =
  | { kind: "corner" }
  | { kind: "header"; col: number }
  | { kind: "rowNumber"; row: number }
  | { kind: "cell"; row: number; col: number };

export function useGridGeometry(layout: ColumnLayout, rowCount: number, colCount: number): GridGeometry {
  const { widths, pinnedCount, pinnedLeft, fixedWidth, rowNumberWidth } = layout;
  const unpinnedEnds = useMemo(() => {
    const ends: number[] = [];
    let x = fixedWidth;
    for (let c = pinnedCount; c < widths.length; c += 1) ends.push((x += widths[c]));
    return ends;
  }, [widths, pinnedCount, fixedWidth]);
  return useMemo(
    () => ({ rowCount, colCount, rowNumberWidth, fixedWidth, pinnedCount, pinnedLeft, widths, unpinnedEnds }),
    [rowCount, colCount, rowNumberWidth, fixedWidth, pinnedCount, pinnedLeft, widths, unpinnedEnds],
  );
}

export function viewportPoint(el: HTMLElement, clientX: number, clientY: number): ViewportPoint {
  const box = el.getBoundingClientRect();
  return {
    x: clientX - box.left,
    y: clientY - box.top,
    width: el.clientWidth,
    height: el.clientHeight,
    scrollLeft: el.scrollLeft,
    scrollTop: el.scrollTop,
  };
}

/** Column under viewport `x` (right of the row numbers), or -1 past the last column. */
function columnAt(g: GridGeometry, x: number, scrollLeft: number): number {
  if (x < g.fixedWidth) {
    for (let c = 0; c < g.pinnedCount; c += 1) if (x < g.pinnedLeft[c] + g.widths[c]) return c;
  }
  const contentX = Math.max(x, g.fixedWidth) + scrollLeft;
  let lo = 0;
  let hi = g.unpinnedEnds.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (g.unpinnedEnds[mid] > contentX) hi = mid;
    else lo = mid + 1;
  }
  return lo < g.unpinnedEnds.length ? g.pinnedCount + lo : -1;
}

const rowAt = (y: number, scrollTop: number) => Math.floor((y + scrollTop - HEADER_HEIGHT) / ROW_HEIGHT);

/** What a press at `p` lands on; null for the footer and the empty space past the last row or column. */
export function hitTest(g: GridGeometry, p: ViewportPoint): GridHit | null {
  const inHeader = p.y < HEADER_HEIGHT;
  const inNumbers = p.x < g.rowNumberWidth;
  if (inHeader && inNumbers) return { kind: "corner" };
  const row = rowAt(p.y, p.scrollTop);
  if (!inHeader && row >= g.rowCount) return null;
  if (inNumbers) return { kind: "rowNumber", row };
  const col = columnAt(g, p.x, p.scrollLeft);
  if (col < 0) return null;
  return inHeader ? { kind: "header", col } : { kind: "cell", row, col };
}

/** The visible body cell nearest to `p` (a drag that leaves the body keeps extending to its edge). */
export function cellNear(g: GridGeometry, p: ViewportPoint): CellPos {
  const y = clamp(p.y, HEADER_HEIGHT, p.height - 1);
  const x = clamp(p.x, g.rowNumberWidth, p.width - 1);
  const col = columnAt(g, x, p.scrollLeft);
  return {
    row: clamp(rowAt(y, p.scrollTop), 0, g.rowCount - 1),
    col: col < 0 ? g.colCount - 1 : col,
  };
}

/** The top-left body cell in view of the scroll area `el`. */
export function firstCellInView(g: GridGeometry, el: HTMLElement): CellPos {
  const { clientWidth: width, clientHeight: height, scrollLeft, scrollTop } = el;
  return cellNear(g, { x: 0, y: 0, width, height, scrollLeft, scrollTop });
}
