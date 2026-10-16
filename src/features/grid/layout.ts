import { useCallback, useMemo, useState, type CSSProperties } from "react";
import type { ColumnMeta } from "@/lib/api/types";
import { autofitWidth, canvasMeasure, estimateWidths, MIN_WIDTH, rowNumberWidth, sampledRows } from "./columns";

/** What the user changed for one column signature: dragged or autofitted widths, and the pinned column count. */
interface UserLayout {
  signature: string;
  widths: ReadonlyMap<number, number>;
  pinnedCount: number;
}

export interface ColumnLayout {
  widths: number[];
  /** Columns `0..pinnedCount-1` stay next to the row numbers. */
  pinnedCount: number;
  rowNumberWidth: number;
  /** Sticky `left` of each pinned column. */
  pinnedLeft: number[];
  /** Everything fixed on the left: the row-number column plus the pinned columns. */
  fixedWidth: number;
  resize: (col: number, width: number) => void;
  autofit: (col: number) => void;
  pin: (count: number) => void;
}

const fresh = (signature: string): UserLayout => ({ signature, widths: new Map(), pinnedCount: 0 });

/**
 * Column widths and pinning of one mounted grid. Widths come from `estimateWidths` over the first rows and are
 * re-estimated only while those rows arrive (and for each new result); later pages change nothing. What the user
 * changed belongs to the column `signature` (`columnSignature`): a re-run with the same columns keeps it, other
 * columns start over.
 */
export function useColumnLayout(
  columns: ColumnMeta[],
  signature: string,
  rows: unknown[][],
  sortable: boolean,
): ColumnLayout {
  const measure = canvasMeasure();
  const sampled = sampledRows(rows);
  const digits = String(rows.length).length;
  // `rows` is left out of the deps on purpose: only its first `sampled` rows are read, and pages are only appended.
  const estimated = useMemo(
    () => estimateWidths(columns, rows, measure, sortable),
    [columns, sampled, measure, sortable],
  );
  const numbersWidth = useMemo(() => rowNumberWidth(digits, measure), [digits, measure]);

  const [user, setUser] = useState(() => fresh(signature));
  const own = user.signature === signature ? user : null; // changes made for other columns do not apply
  const widths = useMemo(() => estimated.map((w, c) => own?.widths.get(c) ?? w), [estimated, own]);
  const pinnedCount = Math.min(own?.pinnedCount ?? 0, columns.length);
  const { pinnedLeft, fixedWidth } = useMemo(() => {
    const lefts: number[] = [];
    let left = numbersWidth;
    for (let c = 0; c < pinnedCount; c += 1) {
      lefts.push(left);
      left += widths[c];
    }
    return { pinnedLeft: lefts, fixedWidth: left };
  }, [widths, pinnedCount, numbersWidth]);

  const change = useCallback(
    (fn: (current: UserLayout) => Partial<UserLayout>) =>
      setUser((prev) => {
        const base = prev.signature === signature ? prev : fresh(signature);
        return { ...base, ...fn(base) };
      }),
    [signature],
  );
  const resize = useCallback(
    (col: number, width: number) =>
      change((u) => ({ widths: new Map(u.widths).set(col, Math.max(MIN_WIDTH, Math.round(width))) })),
    [change],
  );
  const autofit = useCallback(
    (col: number) => resize(col, autofitWidth(columns[col], rows, col, measure, sortable)),
    [columns, rows, measure, sortable, resize],
  );
  const pin = useCallback(
    (count: number) => change(() => ({ pinnedCount: Math.max(0, Math.min(count, columns.length)) })),
    [change, columns.length],
  );

  return { widths, pinnedCount, rowNumberWidth: numbersWidth, pinnedLeft, fixedWidth, resize, autofit, pin };
}

/** One column drawn by the header and by every row. */
export interface DrawnColumn {
  col: number;
  width: number;
  /** Sticky `left` of a pinned column. */
  left?: number;
  /** Blank space before the first unpinned column drawn: the unpinned columns scrolled out on the left. */
  offset?: number;
  /** The last pinned column draws the freeze line. */
  lastPinned?: boolean;
}

/** The horizontal layout of one render, shared by the header and every row (rows re-render only when it changes). */
export interface ColumnStrip {
  columns: ColumnMeta[];
  rowNumberWidth: number;
  totalWidth: number;
  drawn: DrawnColumn[];
}

/** Unpinned columns in view, `first..last` (absolute indices, empty when `last < first`), and the space before them. */
export interface ColumnRange {
  first: number;
  last: number;
  offset: number;
}

/** Width, sticky offset and leading gap of a drawn column (header and body cells alike). */
export const columnStyle = (column: DrawnColumn): CSSProperties => ({
  width: column.width,
  left: column.left,
  marginLeft: column.offset,
});

/** The `selected` tint drawn over a cell's own background, which stays opaque for sticky cells. */
export const SELECTED_CLASS = "bg-[image:linear-gradient(var(--color-selected),var(--color-selected))]";

/** Placement and right edge of a column: pinned ones stick next to the row numbers, the last draws the freeze line. */
export const columnClass = (column: DrawnColumn): string =>
  column.left === undefined
    ? "border-r"
    : column.lastPinned
      ? "sticky z-10 border-r shadow-[2px_0_0_var(--color-border)]" // a wider border would shrink the content box
      : "sticky z-10 border-r";

/** The pinned columns, then the unpinned columns of `range`. */
export function useColumnStrip(
  columns: ColumnMeta[],
  layout: ColumnLayout,
  range: ColumnRange,
  totalWidth: number,
): ColumnStrip {
  const { widths, pinnedCount, pinnedLeft, rowNumberWidth: numbersWidth } = layout;
  const { first, last, offset } = range;
  return useMemo(() => {
    const drawn: DrawnColumn[] = [];
    for (let col = 0; col < pinnedCount; col += 1) {
      drawn.push({ col, width: widths[col], left: pinnedLeft[col], lastPinned: col === pinnedCount - 1 });
    }
    for (let col = first; col <= last; col += 1) {
      drawn.push({ col, width: widths[col], offset: col === first && offset > 0 ? offset : undefined });
    }
    return { columns, rowNumberWidth: numbersWidth, totalWidth, drawn };
  }, [columns, widths, pinnedCount, pinnedLeft, numbersWidth, first, last, offset, totalWidth]);
}
