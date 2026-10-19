import type { ReactNode } from "react";
import { Panel } from "react-resizable-panels";
import { PanelHandle } from "@/components/ui/PanelHandle";
import type { ColumnMeta } from "@/lib/api/types";
import { DrawerFrame } from "./DrawerFrame";
import { RecordView } from "./RecordView";
import type { CellPos } from "./selection";
import { ValueViewer } from "./ValueViewer";

/** The grid's right-side drawer: the value viewer (Enter) or the record view (Space), or none. */
export type GridDrawer = { kind: "value" } | { kind: "record" } | null;

const MIN_WIDTH = 240;

export interface GridDrawerPanelProps {
  drawer: NonNullable<GridDrawer>;
  columns: ColumnMeta[];
  rows: unknown[][];
  hasMore: boolean;
  /** The grid's active cell; null when nothing is selected. */
  focus: CellPos | null;
  /** Initial width in px. */
  width: number;
  onWidthChange: (width: number) => void;
  /** Moves the grid's active row (record view). */
  onMove: (delta: -1 | 1) => void;
  onClose: () => void;
}

function DrawerContent({ drawer, columns, rows, hasMore, focus, onMove, onClose }: GridDrawerPanelProps) {
  if (!focus) {
    return (
      <DrawerFrame
        label={drawer.kind === "value" ? "Value viewer" : "Record view"}
        title={<span className="text-muted">No cell selected</span>}
        onClose={onClose}
      >
        <p className="p-3 text-muted">Select a cell to see its {drawer.kind === "value" ? "value" : "row"}.</p>
      </DrawerFrame>
    );
  }
  if (drawer.kind === "record") {
    return (
      <RecordView
        columns={columns}
        row={rows[focus.row]}
        rowIndex={focus.row}
        rowCount={rows.length}
        hasMore={hasMore}
        onMove={onMove}
        onClose={onClose}
      />
    );
  }
  // Keyed by cell: a new cell starts with a fresh tree (expansion state) and its own parse.
  return (
    <ValueViewer
      key={`${focus.row}:${focus.col}`}
      value={rows[focus.row]?.[focus.col]}
      column={columns[focus.col]}
      onClose={onClose}
    />
  );
}

/**
 * The drawer's divider and panel, rendered inside the grid's horizontal Group. Keeps its pixel width when the window
 * resizes. The value viewer, the record view and the value editor all sit in it.
 */
export function DrawerPanel({
  width,
  onWidthChange,
  children,
}: {
  /** Initial width in px. */
  width: number;
  onWidthChange: (width: number) => void;
  children: ReactNode;
}) {
  return (
    <>
      <PanelHandle orientation="horizontal" />
      <Panel
        defaultSize={width}
        minSize={MIN_WIDTH}
        groupResizeBehavior="preserve-pixel-size"
        onResize={(size) => onWidthChange(size.inPixels)}
      >
        {children}
      </Panel>
    </>
  );
}

/** The value viewer or the record view in the drawer: it follows the grid's active cell (or row). */
export function GridDrawerPanel(props: GridDrawerPanelProps) {
  return (
    <DrawerPanel width={props.width} onWidthChange={props.onWidthChange}>
      <DrawerContent {...props} />
    </DrawerPanel>
  );
}
