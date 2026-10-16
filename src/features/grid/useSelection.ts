import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
  type RefObject,
} from "react";
import type { ColumnMeta } from "@/lib/api/types";
import { HEADER_HEIGHT, ROW_HEIGHT } from "./columns";
import { cellNear, firstCellInView, hitTest, viewportPoint, type GridGeometry, type GridHit } from "./hitTest";
import {
  cellCount,
  clampSelection,
  extendTo,
  move,
  rectOf,
  selectAll,
  selectCell,
  selectColumns,
  selectRows,
  type CellPos,
  type Dims,
  type GridSelection,
  type MoveKey,
  type Rect,
  type SelectionMode,
} from "./selection";

/** The selection of one column list: a new result (new `columns`) starts without one; more rows only clamp it. */
export function useSelectionState(columns: ColumnMeta[], rowCount: number) {
  const [stored, setStored] = useState<{ columns: ColumnMeta[]; selection: GridSelection } | null>(null);
  const dims = useMemo<Dims>(() => ({ rows: rowCount, cols: columns.length }), [rowCount, columns.length]);
  const selection = useMemo(
    () => (stored?.columns === columns ? clampSelection(stored.selection, dims) : null),
    [stored, columns, dims],
  );
  const rect = useMemo(() => (selection ? rectOf(selection, dims) : null), [selection, dims]);
  const select = useCallback(
    (next: GridSelection | null) => setStored(next ? { columns, selection: next } : null),
    [columns],
  );
  return { dims, selection, rect, select };
}

/** The selection a press on `hit` makes; `from` is the selection a shift-press extends, `active` the active cell. */
function selectionFor(hit: GridHit, from: GridSelection | null, active: CellPos): GridSelection {
  switch (hit.kind) {
    case "corner":
      return selectAll(active);
    case "header":
      return selectColumns(from ? from.anchor.col : hit.col, hit.col);
    case "rowNumber":
      return selectRows(from ? from.anchor.row : hit.row, hit.row);
    case "cell": {
      const pos = { row: hit.row, col: hit.col };
      return from ? extendTo(from, pos) : selectCell(pos);
    }
  }
}

function hitInside(hit: GridHit, rect: Rect): boolean {
  const rowIn = (row: number) => row >= rect.top && row <= rect.bottom;
  const colIn = (col: number) => col >= rect.left && col <= rect.right;
  switch (hit.kind) {
    case "corner":
      return true;
    case "header":
      return colIn(hit.col);
    case "rowNumber":
      return rowIn(hit.row);
    case "cell":
      return rowIn(hit.row) && colIn(hit.col);
  }
}

/** Auto-scroll starts this close (px) to an edge of the body and speeds up past it. */
const EDGE = 12;
const MAX_STEP = 40;
/** A press becomes a drag (and may auto-scroll) once the pointer moves this far: a click near an edge never scrolls. */
const DRAG_SLOP = 4;

/** Pixels to scroll this frame for a pointer at `pos` on an axis whose body spans `start..end`. */
function edgeStep(pos: number, start: number, end: number): number {
  if (pos < start + EDGE) return -Math.min(MAX_STEP, Math.ceil((start + EDGE - pos) / 2));
  if (pos > end - EDGE) return Math.min(MAX_STEP, Math.ceil((pos - end + EDGE) / 2));
  return 0;
}

interface Drag {
  pointerId: number;
  mode: SelectionMode;
  /** The selection this drag has made so far (state lags one render behind). */
  selection: GridSelection;
  startX: number;
  startY: number;
  clientX: number;
  clientY: number;
  /** The pointer has moved past DRAG_SLOP: the auto-scroll loop runs. */
  moved: boolean;
  frame: number;
}

export interface PointerSelectionInput {
  scrollRef: RefObject<HTMLDivElement | null>;
  /** A new column list ends a drag in progress. */
  columns: ColumnMeta[];
  geometry: GridGeometry;
  selection: GridSelection | null;
  rect: Rect | null;
  select: (selection: GridSelection | null) => void;
  focusGrid: () => void;
}

/**
 * Mouse selection on the scroll area: press selects (shift extends), dragging extends with pointer capture and
 * auto-scrolls near the edges (one rAF loop per drag), a context click keeps a selection it lands in. A drag ends on
 * pointerup, on a new column list, when the window loses focus and on unmount.
 */
export function usePointerSelection(input: PointerSelectionInput) {
  const latest = useRef(input);
  useLayoutEffect(() => {
    latest.current = input;
  });
  const drag = useRef<Drag | null>(null);

  const stop = useCallback(() => {
    const d = drag.current;
    if (!d) return;
    drag.current = null;
    cancelAnimationFrame(d.frame);
    const el = latest.current.scrollRef.current;
    if (el?.hasPointerCapture(d.pointerId)) el.releasePointerCapture(d.pointerId);
  }, []);
  useEffect(() => stop, [stop, input.columns]);
  useEffect(() => {
    window.addEventListener("blur", stop);
    return () => window.removeEventListener("blur", stop);
  }, [stop]);

  /** Extends the drag's selection to the cell under the pointer (only the axis its mode can change). */
  const follow = useCallback((d: Drag, el: HTMLElement) => {
    const pos = cellNear(latest.current.geometry, viewportPoint(el, d.clientX, d.clientY));
    const { focus } = d.selection;
    const next: CellPos =
      d.mode === "columns"
        ? { row: focus.row, col: pos.col }
        : d.mode === "rows"
          ? { row: pos.row, col: focus.col }
          : pos;
    if (next.row === focus.row && next.col === focus.col) return;
    d.selection = extendTo(d.selection, next);
    latest.current.select(d.selection);
  }, []);

  const tick = useCallback(() => {
    const d = drag.current;
    const el = latest.current.scrollRef.current;
    if (!d || !el) return;
    const p = viewportPoint(el, d.clientX, d.clientY);
    const dx = d.mode === "rows" ? 0 : edgeStep(p.x, latest.current.geometry.fixedWidth, p.width);
    const dy = d.mode === "columns" ? 0 : edgeStep(p.y, HEADER_HEIGHT, p.height);
    if (dx || dy) el.scrollBy(dx, dy);
    follow(d, el); // also after a wheel scroll during the drag
    d.frame = requestAnimationFrame(tick);
  }, [follow]);

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    const { scrollRef, geometry, selection, rect, select, focusGrid } = latest.current;
    const el = scrollRef.current;
    // The scroll container itself is only hit through its scrollbars (classic or overlay).
    if (!el || e.target === el || (e.button !== 0 && e.button !== 2)) return;
    const hit = hitTest(geometry, viewportPoint(el, e.clientX, e.clientY));
    if (!hit) return;
    focusGrid();
    const active = selection?.focus ?? firstCellInView(geometry, el);
    if (e.button === 2 || e.ctrlKey) {
      // Context click: the menu (Task 5) acts on the selection, so keep it when the click is inside.
      if (!rect || !hitInside(hit, rect)) select(selectionFor(hit, null, active));
      return;
    }
    const next = selectionFor(hit, e.shiftKey ? selection : null, active);
    select(next);
    if (hit.kind === "corner") return;
    stop();
    el.setPointerCapture(e.pointerId);
    const { clientX, clientY } = e;
    drag.current = {
      pointerId: e.pointerId,
      mode: next.mode,
      selection: next,
      startX: clientX,
      startY: clientY,
      clientX,
      clientY,
      moved: false,
      frame: 0,
    };
  };
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    const el = latest.current.scrollRef.current;
    if (!d || !el) return;
    d.clientX = e.clientX;
    d.clientY = e.clientY;
    if (!d.moved) {
      if (Math.hypot(d.clientX - d.startX, d.clientY - d.startY) <= DRAG_SLOP) return;
      d.moved = true;
      d.frame = requestAnimationFrame(tick);
    }
    follow(d, el);
  };

  return { onPointerDown, onPointerMove, onPointerUp: stop, onPointerCancel: stop, onLostPointerCapture: stop };
}

const MOVE_KEYS: ReadonlySet<string> = new Set<MoveKey>([
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "Home",
  "End",
  "PageUp",
  "PageDown",
]);

export interface KeyboardSelectionInput {
  scrollRef: RefObject<HTMLDivElement | null>;
  geometry: GridGeometry;
  dims: Dims;
  selection: GridSelection | null;
  rect: Rect | null;
  select: (selection: GridSelection | null) => void;
  /** Scrolls a cell into view; a negative index leaves that axis alone. */
  scrollToCell: (row: number, col: number) => void;
  /** ⌘C: copies the selection as TSV. */
  onCopy?: () => void;
  onOpenValue?: (pos: CellPos) => void;
  onOpenRecord?: (pos: CellPos) => void;
}

/** ⌘ + `letter` (lower case), without shift. */
const isShortcut = (e: KeyboardEvent, letter: string) =>
  e.metaKey && !e.shiftKey && (e.code === `Key${letter.toUpperCase()}` || e.key.toLowerCase() === letter);

/**
 * Keyboard handler of the focused grid (spec §4.3). Keys typed into a control inside the grid and IME composition
 * are left alone; shortcut letters match the physical key (`code`: Caps Lock and non-Latin layouts) or the character
 * (`key`: Dvorak, AZERTY).
 */
export function gridKeyDown(e: KeyboardEvent<HTMLDivElement>, input: KeyboardSelectionInput): void {
  if (e.target !== e.currentTarget || e.altKey || e.ctrlKey) return;
  if (e.nativeEvent.isComposing || e.key === "Process") return;
  const { scrollRef, geometry, dims, selection, rect, select, scrollToCell } = input;
  const el = scrollRef.current;
  if (!el || dims.rows === 0 || dims.cols === 0) return;
  const key = e.key;

  if (MOVE_KEYS.has(key)) {
    e.preventDefault();
    const page = Math.max(1, Math.floor((el.clientHeight - HEADER_HEIGHT) / ROW_HEIGHT));
    const next = selection
      ? move(selection, key as MoveKey, { extend: e.shiftKey, toEdge: e.metaKey, page }, dims)
      : selectCell(firstCellInView(geometry, el)); // the first key selects the top-left cell in view
    select(next);
    // Whole rows / columns scroll along their own axis only; the focus is their first cell.
    scrollToCell(next.mode === "columns" ? -1 : next.focus.row, next.mode === "rows" ? -1 : next.focus.col);
    return;
  }
  if (isShortcut(e, "a")) {
    e.preventDefault();
    select(selectAll(selection?.focus ?? firstCellInView(geometry, el))); // the active cell stays where it was
    return;
  }
  if (!selection || !rect) return;
  if (isShortcut(e, "c")) {
    if (!input.onCopy) return;
    e.preventDefault();
    input.onCopy();
  } else if (e.metaKey || e.shiftKey) {
    // Other shortcuts belong to the app.
  } else if (key === "Escape") {
    if (cellCount(rect) <= 1) return; // nothing to collapse: let a parent use Escape
    e.preventDefault();
    select(selectCell(selection.focus));
    scrollToCell(selection.focus.row, selection.focus.col);
  } else if (key === "Enter") {
    e.preventDefault();
    input.onOpenValue?.(selection.focus);
  } else if (key === " ") {
    e.preventDefault();
    input.onOpenRecord?.(selection.focus);
  }
}
