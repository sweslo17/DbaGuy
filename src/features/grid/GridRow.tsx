import { memo } from "react";
import type { ColumnCategory } from "@/lib/api/types";
import { describeCell } from "@/lib/cells";
import { cn } from "@/lib/cn";
import { ROW_HEIGHT } from "./columns";
import { columnClass, columnStyle, type ColumnStrip, type DrawnColumn } from "./layout";

/** A cell shows one line, so characters beyond this never fit and are not laid out. */
const CELL_CHARS = 1000;

/** Fonts must match `canvasMeasure` in columns.ts (cells: font-mono 12 px, set on the grid's scroll area). */
const CELL = "flex shrink-0 items-center overflow-hidden whitespace-nowrap border-b border-border px-2";

interface GridCellProps extends DrawnColumn {
  value: unknown;
  category: ColumnCategory;
}

/** Memoized on primitives, so a row re-rendered for a horizontal scroll only renders the cells that came into view. */
const GridCell = memo(function GridCell({ value, category, ...column }: GridCellProps) {
  const cell = describeCell(value, category, CELL_CHARS);
  return (
    <div
      className={cn(CELL, columnClass(column), column.left !== undefined && "bg-bg", cell.alignRight && "justify-end")}
      style={columnStyle(column)}
      title={cell.title}
    >
      <span className={cn("truncate", cell.kind === "null" && "italic text-muted")}>{cell.text}</span>
      {cell.detail && <span className="shrink-0 whitespace-pre text-muted">{cell.detail}</span>}
    </div>
  );
});

interface GridRowProps {
  index: number;
  row: unknown[];
  top: number;
  strip: ColumnStrip;
}

/** One body row: the fixed row number, then the strip's columns. */
export const GridRow = memo(function GridRow({ index, row, top, strip }: GridRowProps) {
  return (
    <div className="absolute left-0 flex" style={{ top, height: ROW_HEIGHT, width: strip.totalWidth }}>
      <div
        className={cn(CELL, "sticky left-0 z-20 justify-end border-r bg-panel text-muted")}
        style={{ width: strip.rowNumberWidth }}
      >
        {index + 1}
      </div>
      {strip.drawn.map((column) => (
        <GridCell
          key={column.col}
          {...column}
          value={row[column.col]}
          category={strip.columns[column.col].category}
        />
      ))}
    </div>
  );
});
