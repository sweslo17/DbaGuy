import { memo, type PointerEvent } from "react";
import { cn } from "@/lib/cn";
import { HEADER_HEIGHT } from "./columns";
import { columnClass, columnStyle, type ColumnStrip } from "./layout";

export interface GridSort {
  column: number;
  descending: boolean;
}

interface GridHeaderProps {
  strip: ColumnStrip;
  sort?: GridSort[];
  onHeaderClick?: (column: number, additive: boolean) => void;
  onResize: (col: number, width: number) => void;
  onAutofit: (col: number) => void;
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

/**
 * Sticky header: name (semibold) over type (muted). Fonts must match `canvasMeasure` in columns.ts (name font-mono
 * 12 px semibold, type font-sans 11 px).
 */
export const GridHeader = memo(function GridHeader(props: GridHeaderProps) {
  const { strip, sort, onHeaderClick, onResize, onAutofit } = props;
  return (
    <div className="sticky top-0 z-30 flex" style={{ height: HEADER_HEIGHT, width: strip.totalWidth }}>
      <div
        className="sticky left-0 z-20 shrink-0 border-r border-b border-border bg-panel"
        style={{ width: strip.rowNumberWidth }}
      />
      {strip.drawn.map((column) => {
        const meta = strip.columns[column.col];
        const mark = sortMark(sort, column.col);
        return (
          <div
            key={column.col}
            className={cn(
              "flex shrink-0 flex-col justify-center border-b border-border bg-panel px-2",
              columnClass(column),
              column.left === undefined && "relative", // pinned cells are sticky, which also anchors the handle
              onHeaderClick && "cursor-pointer",
            )}
            style={columnStyle(column)}
            title={`${meta.name}\n${meta.type_name}`}
            onClick={(e) => onHeaderClick?.(column.col, e.shiftKey)}
          >
            <div className="flex min-w-0 items-center gap-1">
              <span className="truncate font-semibold">{meta.name}</span>
              {mark && <span className="shrink-0 font-sans text-[10px] text-accent">{mark}</span>}
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
