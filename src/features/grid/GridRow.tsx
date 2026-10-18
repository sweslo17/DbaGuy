import { memo } from "react";
import type { ColumnCategory } from "@/lib/api/types";
import { describeCell } from "@/lib/cells";
import { cn } from "@/lib/cn";
import { ROW_HEIGHT } from "./columns";
import { columnClass, columnStyle, SELECTED_CLASS, type ColumnStrip, type DrawnColumn } from "./layout";

/** A cell shows one line, so characters beyond this never fit and are not laid out. */
const CELL_CHARS = 1000;

/** Fonts must match `canvasMeasure` in columns.ts (cells: font-mono 12 px, set on the grid's scroll area). */
const CELL = "flex shrink-0 items-center overflow-hidden whitespace-nowrap border-b border-border px-2";
/** The active cell: an inset 1 px accent outline. */
const FOCUSED = "outline-1 -outline-offset-1 outline-accent";

interface GridCellProps extends DrawnColumn {
  value: unknown;
  category: ColumnCategory;
  selected: boolean;
  /** Set on the active cell only (the grid's `aria-activedescendant`). */
  id?: string;
}

/**
 * Memoized on primitives, so a row re-rendered for a horizontal scroll or a selection change only renders the cells
 * that came into view or changed state.
 */
const GridCell = memo(function GridCell({ value, category, selected, id, ...column }: GridCellProps) {
  const cell = describeCell(value, category, CELL_CHARS);
  return (
    <div
      id={id}
      role="gridcell"
      aria-colindex={column.col + 2}
      aria-selected={selected}
      className={cn(
        CELL,
        columnClass(column),
        column.left !== undefined && "bg-bg",
        selected && SELECTED_CLASS,
        id && FOCUSED,
        cell.alignRight && "justify-end",
      )}
      style={columnStyle(column)}
      title={cell.title}
    >
      <span className={cn("truncate", (cell.kind === "null" || cell.kind === "missing") && "italic text-muted")}>
        {cell.text}
      </span>
      {cell.detail && <span className="shrink-0 whitespace-pre text-muted">{cell.detail}</span>}
    </div>
  );
});

interface GridRowProps {
  index: number;
  row: unknown[];
  top: number;
  strip: ColumnStrip;
  /**
   * The selected column range in this row, -1/-1 when the selection does not touch it. Primitives, so moving the
   * selection re-renders only the rows whose part of it changed.
   */
  selLeft: number;
  selRight: number;
  /** Column of the active cell when it is in this row, else -1. */
  focusCol: number;
  /** Element id of the active cell. */
  focusId: string;
}

/** One body row: the fixed row number, then the strip's columns. */
export const GridRow = memo(function GridRow(props: GridRowProps) {
  const { index, row, top, strip, selLeft, selRight, focusCol, focusId } = props;
  return (
    <div
      role="row"
      aria-rowindex={index + 2}
      className="absolute left-0 flex"
      style={{ top, height: ROW_HEIGHT, width: strip.totalWidth }}
    >
      <div
        role="rowheader"
        aria-colindex={1}
        className={cn(
          CELL,
          "sticky left-0 z-20 justify-end border-r bg-panel text-muted",
          selLeft >= 0 && SELECTED_CLASS,
        )}
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
          selected={column.col >= selLeft && column.col <= selRight}
          id={column.col === focusCol ? focusId : undefined}
        />
      ))}
    </div>
  );
});
