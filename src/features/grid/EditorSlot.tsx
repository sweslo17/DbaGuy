import type { KeyboardEvent, ReactNode } from "react";
import type { MenuItem } from "@/components/ui/ContextMenu";
import { cn } from "@/lib/cn";
import { HEADER_HEIGHT, ROW_HEIGHT } from "./columns";
import type { GridGeometry } from "./hitTest";
import type { ColumnLayout } from "./layout";
import type { HeaderMark, RowMark } from "./marks";
import type { CellPos, GridSelection, Rect } from "./selection";

/** The cell being edited and its editor (spec ⑤ §3.6.3): over the cell (inline) or in the drawer. */
export interface GridEditor {
  pos: CellPos;
  mode: "inline" | "drawer";
  /** The editor; `width` is the cell's width (inline) or the drawer's. Keyed by the caller per edited cell. */
  render: (width: number) => ReactNode;
}

/**
 * What an editing table's grid gets (spec ⑤ §3.6): marks to draw, and the editing it hands back to its owner. Without
 * it the grid is read-only, as every query tab's is.
 */
export interface GridEditing {
  /**
   * Whether cells can be edited now: Return and a double click edit, and View value loses its ↵. When false the grid
   * draws the marks (staged changes of a table that cannot be edited now) and Return opens the value viewer; `onEdit`
   * and `editor` are not used then.
   */
  editable: boolean;
  /** By display index (sparse): new, changed and deleted rows. */
  marks: readonly (RowMark | undefined)[];
  /** New rows drawn first: the loaded rows' numbers count from after them. */
  leading: number;
  /** By column index. */
  headerMarks: readonly (HeaderMark | undefined)[];
  /** A double click on a cell, while `editable`. */
  onEdit: (pos: CellPos) => void;
  /**
   * A key pressed while the grid itself has the focus, before the grid handles it: true when it was an editing key
   * that was handled (the grid then does nothing more with it). Asked also while `editable` is false, so that what is
   * staged can still be undone (⌘Z): then it must leave Return and F2 alone (false: Return opens the value viewer).
   */
  onKey: (e: KeyboardEvent<HTMLDivElement>, selection: GridSelection | null, rect: Rect | null) => boolean;
  /**
   * Items leading a cell's menu (`cell`) or the row numbers' menu (not `cell`), for the selection `rect`. Asked also
   * while `editable` is false: then only the items that act on what is staged (Revert, Restore) belong in it.
   */
  menu: (pos: CellPos | null, cell: boolean, rect: Rect | null) => MenuItem[];
  /** The open editor, while `editable`. */
  editor: GridEditor | null;
}

/**
 * An inline editor laid over its cell inside the grid's scroll area, so it scrolls with the rows: a strip as wide as
 * the rows at the cell's row, holding the editor at the cell's left, sticky for a pinned column as its cells are. Above
 * the rows (and their sticky cells), under the sticky header. Only the editor itself takes the pointer.
 */
export function InlineEditorSlot({
  editor,
  layout,
  geometry,
  totalWidth,
}: {
  editor: GridEditor;
  layout: ColumnLayout;
  geometry: GridGeometry;
  totalWidth: number;
}) {
  const { row, col } = editor.pos;
  const width = layout.widths[col];
  if (width === undefined) return null; // a column the grid no longer has
  const pinned = col < layout.pinnedCount;
  const left = pinned ? layout.pinnedLeft[col] : geometry.unpinnedEnds[col - layout.pinnedCount] - width;
  return (
    <div
      className="pointer-events-none absolute left-0 z-[25] flex"
      style={{ top: HEADER_HEIGHT + row * ROW_HEIGHT, height: ROW_HEIGHT, width: totalWidth }}
    >
      <div className={cn("pointer-events-auto", pinned && "sticky")} style={pinned ? { left } : { marginLeft: left }}>
        {editor.render(width)}
      </div>
    </div>
  );
}
