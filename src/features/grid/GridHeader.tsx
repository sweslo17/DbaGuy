import { memo, type PointerEvent } from "react";
import { cn } from "@/lib/cn";
import { HEADER_HEIGHT } from "./columns";
import { columnClass, columnStyle, SELECTED_CLASS, type ColumnStrip } from "./layout";

export interface GridSort {
  column: number;
  descending: boolean;
}

interface GridHeaderProps {
  strip: ColumnStrip;
  sort?: GridSort[];
  /** Sorting: each header gets a sort control that calls this (shift: additive). */
  onHeaderClick?: (column: number, additive: boolean) => void;
  onResize: (col: number, width: number) => void;
  onAutofit: (col: number) => void;
  /** Selected column range (-1 when nothing is selected). Selecting is done by the grid's pointer handler. */
  selLeft: number;
  selRight: number;
}

interface ResizeHandleProps {
  width: number;
  onResize: (width: number) => void;
  onAutofit: () => void;
}

/** Drag to resize (the layout enforces the minimum width), double-click to fit the loaded rows. */
function ResizeHandle({ width, onResize, onAutofit }: ResizeHandleProps) {
  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const handle = e.currentTarget;
    const startX = e.clientX;
    const move = (ev: globalThis.PointerEvent) => onResize(width + ev.clientX - startX);
    const end = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", end);
      handle.removeEventListener("pointercancel", end);
    };
    handle.setPointerCapture(e.pointerId);
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
  };
  return (
    <div
      className="absolute top-0 right-0 z-10 h-full w-1.5 cursor-col-resize hover:bg-accent/50"
      onPointerDown={onPointerDown}
      onClick={(e) => e.stopPropagation()}
      onDoubleClick={(e) => {
        e.stopPropagation();
        onAutofit();
      }}
    />
  );
}

function sortMark(sort: GridSort[] | undefined, col: number): string | null {
  const index = sort?.findIndex((key) => key.column === col) ?? -1;
  if (!sort || index < 0) return null;
  return `${sort[index].descending ? "▼" : "▲"}${sort.length > 1 ? index + 1 : ""}`;
}

interface SortControlProps {
  mark: string | null;
  onSort: (additive: boolean) => void;
}

/** The sort control at a header's right edge (ruling R-02b-6): sorts without touching the selection. */
function SortControl({ mark, onSort }: SortControlProps) {
  return (
    <button
      type="button"
      tabIndex={-1}
      className={cn(
        "ml-auto min-w-4 shrink-0 rounded px-0.5 font-sans text-[10px] leading-4 hover:bg-hover",
        mark ? "text-accent" : "text-muted opacity-60 hover:opacity-100",
      )}
      title="Sort (shift-click adds to the sort)"
      onPointerDown={(e) => e.stopPropagation()} // not a column selection
      onMouseDown={(e) => e.preventDefault()} // the grid keeps the keyboard focus
      onClick={(e) => onSort(e.shiftKey)}
    >
      {mark ?? "↕"}
    </button>
  );
}

/**
 * Sticky header: name (semibold) over type (muted). Fonts must match `canvasMeasure` in columns.ts (name font-mono
 * 12 px semibold, type font-sans 11 px).
 */
export const GridHeader = memo(function GridHeader(props: GridHeaderProps) {
  const { strip, sort, onHeaderClick, onResize, onAutofit, selLeft, selRight } = props;
  return (
    <div
      role="row"
      aria-rowindex={1}
      className="sticky top-0 z-30 flex"
      style={{ height: HEADER_HEIGHT, width: strip.totalWidth }}
    >
      <div
        role="columnheader"
        aria-colindex={1}
        className="sticky left-0 z-20 shrink-0 border-r border-b border-border bg-panel"
        style={{ width: strip.rowNumberWidth }}
        title="Select all"
      />
      {strip.drawn.map((column) => {
        const meta = strip.columns[column.col];
        return (
          <div
            key={column.col}
            role="columnheader"
            aria-colindex={column.col + 2}
            className={cn(
              "flex shrink-0 flex-col justify-center border-b border-border bg-panel px-2",
              columnClass(column),
              // Pinned cells are sticky, which also anchors the handle. Unpinned ones are their own stacking context,
              // so their resize handle (z-10) stays under the pinned headers it scrolls beneath.
              column.left === undefined && "relative isolate",
              column.col >= selLeft && column.col <= selRight && SELECTED_CLASS,
            )}
            style={columnStyle(column)}
            title={`${meta.name}\n${meta.type_name}`}
          >
            <div className="flex min-w-0 items-center gap-1">
              <span className="truncate font-semibold">{meta.name}</span>
              {onHeaderClick && (
                <SortControl
                  mark={sortMark(sort, column.col)}
                  onSort={(additive) => onHeaderClick(column.col, additive)}
                />
              )}
            </div>
            <div className="truncate font-sans text-[11px] text-muted">{meta.type_name}</div>
            <ResizeHandle
              width={column.width}
              onResize={(width) => onResize(column.col, width)}
              onAutofit={() => onAutofit(column.col)}
            />
          </div>
        );
      })}
    </div>
  );
});
