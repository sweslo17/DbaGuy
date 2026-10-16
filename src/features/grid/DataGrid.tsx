import { useVirtualizer } from "@tanstack/react-virtual";
import {
  useCallback,
  useEffect,
  useEffectEvent,
  useId,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  type KeyboardEvent,
  type Ref,
} from "react";
import type { MenuItem } from "@/components/ui/ContextMenu";
import { Button } from "@/components/ui/Button";
import type { ColumnMeta, Dialect } from "@/lib/api/types";
import { aggregate } from "./aggregate";
import { HEADER_HEIGHT, ROW_HEIGHT } from "./columns";
import { GridHeader, type GridSort } from "./GridHeader";
import { GridRow } from "./GridRow";
import { useGridGeometry } from "./hitTest";
import { useColumnLayout, useColumnStrip } from "./layout";
import { cellCount, type CellPos, type GridSelection, type Rect } from "./selection";
import { StatusBar } from "./StatusBar";
import { gridKeyDown, usePointerSelection, useSelectionState } from "./useSelection";

export interface DataGridHandle {
  /**
   * Scrolls just enough to show the cell (below the header, right of the pinned columns); a negative index leaves
   * that axis alone.
   */
  scrollToCell(row: number, col: number): void;
  focus(): void;
}

/** The current selection: the active cell is `selection.focus`; `rect` is what copy and the aggregates cover. */
export interface GridSelectionState {
  selection: GridSelection;
  rect: Rect;
}

export interface DataGridProps {
  columns: ColumnMeta[];
  rows: unknown[][];
  hasMore: boolean;
  /** Rows are being fetched (the next page, or the first one). */
  loadingMore: boolean;
  onLoadMore?: () => void;
  dialect: Dialect; // copy/INSERT formats (Task 5)
  tableName?: { schema: string | null; name: string } | null;
  sort?: GridSort[]; // table data view (Task 7)
  onHeaderClick?: (column: number, additive: boolean) => void;
  extraCellMenu?: (row: number, column: number) => MenuItem[];
  /** ⌘C (Task 5). */
  onCopy?: (rect: Rect, format: "tsv") => void;
  /** Enter: the value viewer (Task 6). */
  onOpenValue?: (pos: CellPos) => void;
  /** Space: the record view (Task 6). */
  onOpenRecord?: (pos: CellPos) => void;
  onSelectionChange?: (state: GridSelectionState | null) => void;
  ref?: Ref<DataGridHandle>;
}

const ROW_OVERSCAN = 10;
const COLUMN_OVERSCAN = 3;
/** The next page is requested once a row this close to the end is rendered. */
const LOAD_AHEAD = 50;
const FOOTER_HEIGHT = 32;

/**
 * Scroll offset that shows the span [start, end) inside a view of `size` whose first `covered` px are hidden under
 * sticky content, moving as little as possible. Uses the client size: the virtualizer's size includes scrollbars.
 */
function reveal(offset: number, size: number, covered: number, start: number, end: number): number {
  if (start < offset + covered) return start - covered;
  if (end > offset + size) return Math.min(start - covered, end - size);
  return offset;
}

interface LoadMoreFooterProps {
  rowCount: number;
  hasMore: boolean;
  loading: boolean;
  onLoadMore?: () => void;
}

/** Last line of the grid: the state of incremental loading, or "No rows". */
function LoadMoreFooter({ rowCount, hasMore, loading, onLoadMore }: LoadMoreFooterProps) {
  if (loading) return rowCount > 0 ? "Loading more…" : "Loading…";
  if (!hasMore) return "No rows";
  if (!onLoadMore) return "More rows available";
  return (
    <Button size="sm" onClick={onLoadMore}>
      Load more
    </Button>
  );
}

/**
 * The result grid: rows and unpinned columns virtualized (TanStack Virtual) in one scroll area, with a sticky header,
 * a sticky row-number column and optional pinned columns. Layout (widths, pinning, scroll) belongs to the mounted grid.
 * The selection is one rectangle (useSelection.ts); rows and the header get it as primitive props, so changing it
 * re-renders only the rows it enters or leaves. The status bar under the grid shows the result size and aggregates.
 */
export function DataGrid(props: DataGridProps) {
  const { columns, rows, hasMore, loadingMore, onLoadMore, sort, onHeaderClick, ref } = props;
  const { onCopy, onOpenValue, onOpenRecord, onSelectionChange } = props;
  const rootRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const focusId = useId();
  const layout = useColumnLayout(columns, rows, !!onHeaderClick);
  const geometry = useGridGeometry(layout, rows.length, columns.length);
  const { dims, selection, rect, select } = useSelectionState(columns, rows.length);
  const footer = hasMore || loadingMore || rows.length === 0;

  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: ROW_OVERSCAN,
    paddingStart: HEADER_HEIGHT,
    paddingEnd: footer ? FOOTER_HEIGHT : 0,
  });
  const columnVirtualizer = useVirtualizer({
    horizontal: true,
    count: columns.length - layout.pinnedCount,
    getScrollElement: () => scrollRef.current,
    estimateSize: (i) => layout.widths[layout.pinnedCount + i],
    overscan: COLUMN_OVERSCAN,
    paddingStart: layout.fixedWidth,
  });
  // The virtualizer caches sizes; widths changed by a drag or an autofit need a re-measure (before paint).
  useLayoutEffect(() => {
    columnVirtualizer.measure();
  }, [columnVirtualizer, layout.widths]);
  // Incremental loading asks once per row count (see below); a new result starts over, at the top-left.
  const requestedAt = useRef(-1);
  useLayoutEffect(() => {
    requestedAt.current = -1;
    scrollRef.current?.scrollTo({ top: 0, left: 0 });
  }, [columns]);

  const virtualRows = rowVirtualizer.getVirtualItems();
  const virtualColumns = columnVirtualizer.getVirtualItems();
  const firstColumn = virtualColumns[0];
  const strip = useColumnStrip(
    columns,
    layout,
    {
      first: firstColumn ? firstColumn.index + layout.pinnedCount : 0,
      last: firstColumn ? virtualColumns[virtualColumns.length - 1].index + layout.pinnedCount : -1,
      offset: firstColumn ? firstColumn.start - layout.fixedWidth : 0,
    },
    columnVirtualizer.getTotalSize(),
  );

  // Incremental loading: ask for the next page once a rendered row comes close to the end.
  const lastRendered = virtualRows.length > 0 ? virtualRows[virtualRows.length - 1].index : -1;
  useEffect(() => {
    if (!hasMore || loadingMore || !onLoadMore || lastRendered < rows.length - LOAD_AHEAD) return;
    if (requestedAt.current === rows.length) return;
    requestedAt.current = rows.length;
    onLoadMore();
  }, [hasMore, loadingMore, onLoadMore, lastRendered, rows.length]);

  const scrollToCell = useCallback(
    (row: number, col: number) => {
      const el = scrollRef.current;
      if (!el) return;
      const y = rowVirtualizer.measurementsCache[row];
      const x = col >= layout.pinnedCount ? columnVirtualizer.measurementsCache[col - layout.pinnedCount] : undefined;
      el.scrollTo({
        top: y ? reveal(el.scrollTop, el.clientHeight, HEADER_HEIGHT, y.start, y.end) : el.scrollTop,
        left: x ? reveal(el.scrollLeft, el.clientWidth, layout.fixedWidth, x.start, x.end) : el.scrollLeft,
      });
    },
    [rowVirtualizer, columnVirtualizer, layout.pinnedCount, layout.fixedWidth],
  );
  const focusGrid = useCallback(() => rootRef.current?.focus({ preventScroll: true }), []);
  useImperativeHandle(ref, () => ({ scrollToCell, focus: focusGrid }), [scrollToCell, focusGrid]);

  const pointer = usePointerSelection({ scrollRef, columns, geometry, selection, rect, select, focusGrid });
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) =>
    gridKeyDown(e, {
      scrollRef,
      geometry,
      dims,
      selection,
      rect,
      select,
      scrollToCell,
      onCopy,
      onOpenValue,
      onOpenRecord,
    });

  // The rect as numbers: a new page gives an equal rect a new identity, which must not count as a change.
  const top = rect?.top ?? -1;
  const left = rect?.left ?? -1;
  const bottom = rect?.bottom ?? -1;
  const right = rect?.right ?? -1;
  const emitSelection = useEffectEvent((): void => {
    onSelectionChange?.(selection && rect ? { selection, rect } : null);
  });
  useEffect(() => {
    emitSelection();
  }, [selection, top, left, bottom, right]);
  // Status-bar figures, only when the rect changes. `rows` is left out of the deps on purpose: pages are only
  // appended, so its length stands for it.
  const aggregates = useMemo(() => {
    const area = { top, left, bottom, right };
    return top >= 0 && cellCount(area) > 1 ? aggregate(area, rows, columns) : null;
  }, [top, left, bottom, right, rows.length, columns]);

  const focus = selection?.focus;
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        ref={rootRef}
        role="grid"
        tabIndex={0}
        aria-multiselectable
        aria-rowcount={hasMore ? -1 : rows.length + 1}
        aria-colcount={columns.length + 1}
        aria-activedescendant={focus ? focusId : undefined}
        className="group/grid relative isolate flex min-h-0 flex-1 flex-col outline-none"
        onKeyDown={onKeyDown}
      >
        <div
          ref={scrollRef}
          className="relative min-h-0 flex-1 overflow-auto font-mono text-[12px]"
          onPointerDown={pointer.onPointerDown}
          onPointerMove={pointer.onPointerMove}
          onPointerUp={pointer.onPointerUp}
          onPointerCancel={pointer.onPointerCancel}
          onLostPointerCapture={pointer.onLostPointerCapture}
        >
          <div className="relative isolate" style={{ width: strip.totalWidth, height: rowVirtualizer.getTotalSize() }}>
            <GridHeader
              strip={strip}
              sort={sort}
              onHeaderClick={onHeaderClick}
              onResize={layout.resize}
              onAutofit={layout.autofit}
              selLeft={left}
              selRight={right}
            />
            {virtualRows.map((item) => {
              const touched = item.index >= top && item.index <= bottom;
              return (
                <GridRow
                  key={item.index}
                  index={item.index}
                  row={rows[item.index]}
                  top={item.start}
                  strip={strip}
                  selLeft={touched ? left : -1}
                  selRight={touched ? right : -1}
                  focusCol={focus?.row === item.index ? focus.col : -1}
                  focusId={focusId}
                />
              );
            })}
            {footer && (
              <div
                className="absolute left-0 flex"
                style={{
                  top: HEADER_HEIGHT + rows.length * ROW_HEIGHT,
                  height: FOOTER_HEIGHT,
                  width: strip.totalWidth,
                }}
              >
                <div className="sticky left-0 flex items-center px-2 font-sans text-muted">
                  <LoadMoreFooter
                    rowCount={rows.length}
                    hasMore={hasMore}
                    loading={loadingMore}
                    onLoadMore={onLoadMore}
                  />
                </div>
              </div>
            )}
          </div>
        </div>
        {/* Keyboard focus ring, drawn over the sticky header and pinned columns. */}
        <div className="pointer-events-none absolute inset-0 z-10 hidden border border-accent group-focus-visible/grid:block" />
      </div>
      <StatusBar rowCount={rows.length} columnCount={columns.length} hasMore={hasMore} aggregates={aggregates} />
    </div>
  );
}
