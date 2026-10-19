import { memo } from "react";
import type { ColumnCategory } from "@/lib/api/types";
import { describeCell } from "@/lib/cells";
import { cn } from "@/lib/cn";
import { ROW_HEIGHT } from "./columns";
import { columnClass, columnStyle, SELECTED_CLASS, type ColumnStrip, type DrawnColumn } from "./layout";
import { cellBackground, cellTitle, PROBLEM_CELL, rowTint, rowTitle, type CellMark, type RowMark } from "./marks";
import { countRowRender } from "./renderCount";

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
  /** The cell's staged change, if any (an object kept while it does not change). */
  mark?: CellMark;
  /** The row's tint (a new row, a row marked for deletion, a row that cannot be edited), if any. */
  tint?: string;
}

/**
 * Memoized on primitives (and on its mark, an object kept while unchanged), so a row re-rendered for a horizontal
 * scroll or a selection change only renders the cells that came into view or changed state.
 */
const GridCell = memo(function GridCell({ value, category, selected, id, mark, tint, ...column }: GridCellProps) {
  const cell = describeCell(value, category, CELL_CHARS);
  const isDefault = mark?.isDefault === true;
  return (
    <div
      id={id}
      role="gridcell"
      aria-colindex={column.col + 2}
      aria-selected={selected}
      className={cn(
        CELL,
        columnClass(column),
        cellBackground(mark, tint, column.left !== undefined),
        selected && SELECTED_CLASS,
        mark?.problem && !id && PROBLEM_CELL,
        id && FOCUSED,
        mark?.conflict && column.left === undefined && "relative",
        cell.alignRight && !isDefault && "justify-end",
      )}
      style={columnStyle(column)}
      title={cellTitle(cell.title, mark)}
    >
      {isDefault ? (
        <span className="truncate text-muted italic">DEFAULT</span>
      ) : (
        <>
          <span className={cn("truncate", (cell.kind === "null" || cell.kind === "missing") && "italic text-muted")}>
            {cell.text}
          </span>
          {cell.detail && <span className="shrink-0 whitespace-pre text-muted">{cell.detail}</span>}
        </>
      )}
      {mark?.conflict && (
        <span
          aria-hidden
          className="absolute top-0 right-0 border-t-[6px] border-l-[6px] border-t-danger border-l-transparent"
        />
      )}
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
  /** What the row-number column shows: the row's number, `+` (a new row) or `−` (marked for deletion). */
  label: string;
  /** The row's staged change, if any (an object kept while it does not change). */
  mark?: RowMark;
}

/** One body row: the fixed row number, then the strip's columns. */
export const GridRow = memo(function GridRow(props: GridRowProps) {
  const { index, row, top, strip, selLeft, selRight, focusCol, focusId, label, mark } = props;
  countRowRender(index);
  const tint = rowTint(mark);
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
          "sticky left-0 z-20 justify-end border-r bg-panel",
          mark?.problem ? "font-semibold text-danger" : "text-muted",
          selLeft >= 0 && SELECTED_CLASS,
        )}
        style={{ width: strip.rowNumberWidth }}
        title={rowTitle(mark)}
      >
        {label}
      </div>
      {strip.drawn.map((column) => (
        <GridCell
          key={column.col}
          {...column}
          value={row[column.col]}
          category={strip.columns[column.col].category}
          selected={column.col >= selLeft && column.col <= selRight}
          id={column.col === focusCol ? focusId : undefined}
          mark={mark?.cells?.[column.col]}
          tint={tint}
        />
      ))}
    </div>
  );
});
