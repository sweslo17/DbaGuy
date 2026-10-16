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
  useState,
  type KeyboardEvent,
  type MouseEvent,
  type Ref,
} from "react";
import { Group, Panel } from "react-resizable-panels";
import type { MenuItem } from "@/components/ui/ContextMenu";
import { Button } from "@/components/ui/Button";
import type { ColumnMeta, Dialect } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { aggregate } from "./aggregate";
import { columnSignature, HEADER_HEIGHT, ROW_HEIGHT } from "./columns";
import { copyDefault, copySelection, type CopyFormat, type CopyInput } from "./copy";
import { ExportDialog, type ExportSource } from "./ExportDialog";
import { GridDrawerPanel, type GridDrawer } from "./GridDrawer";
import { GridHeader, type GridSort } from "./GridHeader";
import { GridMenu } from "./GridMenu";
import { GridRow } from "./GridRow";
import { hitTest, useGridGeometry, viewportPoint } from "./hitTest";
import { useColumnLayout, useColumnStrip } from "./layout";
import { cellCount, move, selectCell, type CellPos, type GridSelection, type Rect } from "./selection";
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
  /** The result ended before all its rows were loaded (it was discarded while paused): the count says "(partial)". */
  partial?: boolean;
  /** Rows are being fetched (the next page, or the first one). */
  loadingMore: boolean;
  onLoadMore?: () => void;
  /**
   * The rows belong to the previous request: they stay on screen, dimmed with a "Loading…" note, until the next one
   * delivers its first page (`useHeldResult`); nothing more is loaded for them.
   */
  stale?: boolean;
  /** Dialect of the copy formats that need one (INSERT, IN list); null while unknown, and those copies refuse. */
  dialect: Dialect | null;
  /** INSERT copies target this table (table data); else a `table_name` placeholder. */
  tableName?: { schema: string | null; name: string } | null;
  sort?: GridSort[]; // table data view (Task 7)
  onHeaderClick?: (column: number, additive: boolean) => void;
  /** Header menu "Sort ascending / descending": the sort becomes this column alone (shown only when set). */
  onSortBy?: (column: number, descending: boolean) => void;
  /** Items appended to a cell's context menu. */
  extraCellMenu?: (row: number, column: number) => MenuItem[];
  onSelectionChange?: (state: GridSelectionState | null) => void;
  /** The statement behind the rows, for "Export…" (context menu and status bar); none: no export. */
  exportSource?: ExportSource | null;
  ref?: Ref<DataGridHandle>;
}

const ROW_OVERSCAN = 10;
const COLUMN_OVERSCAN = 3;
/** The next page is requested once a row this close to the end is rendered. */
const LOAD_AHEAD = 50;
const FOOTER_HEIGHT = 32;
/** Default width (px) of the value viewer / record view drawer. */
const DRAWER_WIDTH = 360;
/** The grid keeps at least this much (px) beside the drawer. */
const GRID_MIN_WIDTH = 160;

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
 * Enter / Space open the value viewer / record view in a resizable drawer on the right, which follows the active cell.
 */
export function DataGrid(props: DataGridProps) {
  const { columns, rows, hasMore, sort, onHeaderClick, stale = false, ref } = props;
  const { dialect, tableName, onSortBy, extraCellMenu, onSelectionChange, exportSource, partial = false } = props;
  // Stale rows are only shown: nothing is loaded for them.
  const loadingMore = props.loadingMore && !stale;
  const onLoadMore = stale ? undefined : props.onLoadMore;
  const rootRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const focusId = useId();
  const signature = useMemo(() => columnSignature(columns), [columns]);
  const layout = useColumnLayout(columns, signature, rows, !!onHeaderClick);
  const geometry = useGridGeometry(layout, rows.length, columns.length);
  const { dims, selection, rect, select } = useSelectionState(signature, columns.length, rows.length);
  const [drawer, setDrawer] = useState<GridDrawer>(null);
  const [drawerWidth, setDrawerWidth] = useState(DRAWER_WIDTH);
  /** The drawer's width while it is open; kept for the next opening when it closes. */
  const liveDrawerWidth = useRef(DRAWER_WIDTH);
  const [exportOpen, setExportOpen] = useState(false);
  const openExport = useCallback(() => setExportOpen(true), []);
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
  // Incremental loading asks once per row count (see below); a new result starts over, at the top. A result with
  // other columns also starts at the left; the same columns (a sort, a refresh) keep the horizontal position.
  const requestedAt = useRef(-1);
  useLayoutEffect(() => {
    requestedAt.current = -1;
    scrollRef.current?.scrollTo({ top: 0 });
  }, [columns]);
  useLayoutEffect(() => {
    scrollRef.current?.scrollTo({ left: 0 });
  }, [signature]);

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
  /** What a copy covers: the selection over the rows loaded now, whatever its mode. */
  const copyInput = (): CopyInput | null => {
    if (!selection || !rect) return null;
    const wholeColumns = selection.mode === "columns" || selection.mode === "all";
    return { rect, rows, columns, dialect, tableName, wholeColumns };
  };
  const copy = (format: CopyFormat) => {
    const input = copyInput();
    if (input) void copySelection(format, input);
  };
  /** ⌘C and the menu's Copy: one cell's plain value, or TSV. */
  const copyDefaultSelection = () => {
    const input = copyInput();
    if (input) void copyDefault(input);
  };

  const openValue = () => setDrawer({ kind: "value" });
  const openRecord = () => setDrawer({ kind: "record" });
  const closeDrawer = () => {
    setDrawerWidth(liveDrawerWidth.current);
    setDrawer(null);
    focusGrid();
  };
  /** Record view ↑/↓: moves the active cell a row, as the grid's own arrow keys do. */
  const moveRow = (delta: -1 | 1) => {
    if (!selection) return;
    const next = move(selection, delta < 0 ? "ArrowUp" : "ArrowDown", { extend: false, toEdge: false, page: 1 }, dims);
    select(next);
    scrollToCell(next.focus.row, -1);
  };
  /** A drawer opened from the menu shows the clicked cell: it becomes the active cell. */
  const openAt = (open: () => void) => (pos: CellPos) => {
    if (selection?.focus.row !== pos.row || selection.focus.col !== pos.col) select(selectCell(pos));
    open();
  };
  const menuHit = (e: MouseEvent) => {
    const el = scrollRef.current;
    // The scroll container itself is only hit through its scrollbars.
    return el && e.target !== el ? hitTest(geometry, viewportPoint(el, e.clientX, e.clientY)) : null;
  };
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    gridKeyDown(e, {
      scrollRef,
      geometry,
      dims,
      selection,
      rect,
      select,
      scrollToCell,
      onCopy: copyDefaultSelection,
      onOpenValue: openValue,
      onOpenRecord: openRecord,
    });
    // An Escape the selection did not use (nothing to collapse) closes the drawer.
    const plain = !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey;
    if (drawer && e.key === "Escape" && plain && !e.defaultPrevented && e.target === e.currentTarget) {
      e.preventDefault();
      closeDrawer();
    }
  };

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
      <div className="min-h-0 flex-1">
        <Group orientation="horizontal">
          {/* No panel ids: they become DOM ids, and a page can show several grids. */}
          <Panel minSize={GRID_MIN_WIDTH}>
            <div
              ref={rootRef}
              role="grid"
              tabIndex={0}
              aria-multiselectable
              aria-rowcount={hasMore ? -1 : rows.length + 1}
              aria-colcount={columns.length + 1}
              aria-activedescendant={focus ? focusId : undefined}
              aria-busy={stale}
              className="group/grid relative isolate flex h-full min-h-0 flex-col outline-none"
              onKeyDown={onKeyDown}
            >
              <GridMenu
                hitAt={menuHit}
                columns={columns}
                rect={rect}
                focus={selection?.focus ?? null}
                pinnedCount={layout.pinnedCount}
                copyDefault={copyDefaultSelection}
                copy={copy}
                pin={layout.pin}
                openValue={openAt(openValue)}
                openRecord={openAt(openRecord)}
                sortBy={onSortBy}
                extraCellMenu={extraCellMenu}
                openExport={exportSource ? openExport : undefined}
              >
                <div
                  ref={scrollRef}
                  className={cn(
                    "relative min-h-0 flex-1 overflow-auto font-mono text-[12px]",
                    stale && "opacity-50 transition-opacity delay-150",
                  )}
                  onPointerDown={pointer.onPointerDown}
                  onPointerMove={pointer.onPointerMove}
                  onPointerUp={pointer.onPointerUp}
                  onPointerCancel={pointer.onPointerCancel}
                  onLostPointerCapture={pointer.onLostPointerCapture}
                >
                  <div
                    className="relative isolate"
                    style={{ width: strip.totalWidth, height: rowVirtualizer.getTotalSize() }}
                  >
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
              </GridMenu>
              {stale && (
                <div
                  aria-hidden
                  className="pointer-events-none absolute top-11 right-3 z-20 animate-delayed-show rounded-md border border-border bg-elevated px-2 py-0.5 font-sans text-[12px] text-muted shadow"
                >
                  Loading…
                </div>
              )}
              {/* Keyboard focus ring, drawn over the sticky header and pinned columns. */}
              <div className="pointer-events-none absolute inset-0 z-10 hidden border border-accent group-focus-visible/grid:block" />
            </div>
          </Panel>
          {drawer && (
            <GridDrawerPanel
              drawer={drawer}
              columns={columns}
              rows={rows}
              hasMore={hasMore}
              focus={focus ?? null}
              width={drawerWidth}
              onWidthChange={(width) => {
                liveDrawerWidth.current = width;
              }}
              onMove={moveRow}
              onClose={closeDrawer}
            />
          )}
        </Group>
      </div>
      <StatusBar
        rowCount={rows.length}
        columnCount={columns.length}
        hasMore={hasMore}
        partial={partial}
        aggregates={aggregates}
        onExport={exportSource ? openExport : undefined}
      />
      {exportSource && <ExportDialog source={exportSource} open={exportOpen} onOpenChange={setExportOpen} />}
    </div>
  );
}
