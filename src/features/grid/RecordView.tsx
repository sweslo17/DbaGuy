import { ChevronDown, ChevronUp } from "lucide-react";
import type { KeyboardEvent } from "react";
import { Button } from "@/components/ui/Button";
import type { ColumnMeta } from "@/lib/api/types";
import { describeCell } from "@/lib/cells";
import { cn } from "@/lib/cn";
import { DrawerFrame } from "./DrawerFrame";

export interface RecordViewProps {
  columns: ColumnMeta[];
  row: unknown[] | undefined;
  rowIndex: number;
  /** Rows loaded now; `hasMore` when the result has more. */
  rowCount: number;
  hasMore?: boolean;
  /** Moves the grid's active row (the drawer follows it). */
  onMove: (delta: -1 | 1) => void;
  onClose: () => void;
}

/** A value longer than this is cut (the value viewer shows all of it), so one big field cannot bury the rest. */
const FIELD_CHARS = 2000;

function RecordField({ column, value }: { column: ColumnMeta; value: unknown }) {
  const cell = describeCell(value, column.category, FIELD_CHARS);
  return (
    <>
      <dt className="min-w-0 border-b border-border px-3 py-1">
        <div className="font-mono font-semibold break-all" data-selectable>
          {column.name}
        </div>
        <div className="text-[11px] text-muted">{column.type_name}</div>
      </dt>
      <dd
        className="m-0 min-w-0 border-b border-border px-3 py-1 font-mono break-words whitespace-pre-wrap"
        data-selectable
      >
        <span className={cn(cell.kind === "null" && "text-muted italic")}>
          {cell.text}
          {cell.clipped && "…"}
        </span>
        {cell.detail && <span className="text-muted">{cell.detail}</span>}
        {cell.kind === "empty" && <span className="font-sans text-muted italic">empty string</span>}
      </dd>
    </>
  );
}

/**
 * The record view drawer: the active row as name | value pairs. ↑/↓ (while the drawer has focus) and the header
 * buttons move the grid's active row.
 */
export function RecordView({ columns, row, rowIndex, rowCount, hasMore, onMove, onClose }: RecordViewProps) {
  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    e.preventDefault();
    onMove(e.key === "ArrowUp" ? -1 : 1);
  };
  return (
    <DrawerFrame
      label="Record view"
      title={
        <span className="truncate font-semibold">
          Row {(rowIndex + 1).toLocaleString()} of {rowCount.toLocaleString()}
          {hasMore ? "+" : ""}
        </span>
      }
      actions={
        <>
          <Button
            variant="ghost"
            size="sm"
            className="px-1"
            aria-label="Previous row"
            title="Previous row (↑)"
            disabled={rowIndex <= 0}
            onClick={() => onMove(-1)}
          >
            <ChevronUp size={14} />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="px-1"
            aria-label="Next row"
            title="Next row (↓)"
            disabled={rowIndex >= rowCount - 1}
            onClick={() => onMove(1)}
          >
            <ChevronDown size={14} />
          </Button>
        </>
      }
      onKeyDown={onKeyDown}
      onClose={onClose}
    >
      {row && (
        <dl className="m-0 grid grid-cols-[fit-content(40%)_minmax(0,1fr)]">
          {columns.map((column, i) => (
            <RecordField key={i} column={column} value={row[i]} />
          ))}
        </dl>
      )}
    </DrawerFrame>
  );
}
