import { useState, type MouseEvent, type ReactNode } from "react";
import { ContextMenu, type MenuItem } from "@/components/ui/ContextMenu";
import type { ColumnMeta } from "@/lib/api/types";
import { copyColumnNames, COPY_FORMAT_LABELS, COPY_FORMATS, type CopyFormat } from "./copy";
import type { GridHit } from "./hitTest";
import type { CellPos, Rect } from "./selection";

/** What the grid's menus act on: the current selection, plus the cell or header the context click landed on. */
export interface GridMenuActions {
  columns: ColumnMeta[];
  rect: Rect | null;
  /** The active cell (rows and select-all menus open the value viewer / record view on it). */
  focus: CellPos | null;
  pinnedCount: number;
  copy: (format: CopyFormat) => void;
  pin: (count: number) => void;
  /** Absent: the item shows disabled. */
  openValue?: (pos: CellPos) => void;
  openRecord?: (pos: CellPos) => void;
  /** Header "Sort ascending / descending" (shown only when set). */
  sortBy?: (column: number, descending: boolean) => void;
  /** Items appended to a cell's menu (table data: "Filter by this value"). */
  extraCellMenu?: (row: number, column: number) => MenuItem[];
}

/** The group starts with a separator. */
const group = (items: MenuItem[]): MenuItem[] =>
  items.map((item, i) => (i === 0 ? { ...item, separatorBefore: true } : item));

function pinItems(col: number, a: GridMenuActions): MenuItem[] {
  const items: MenuItem[] = [
    { label: "Pin columns up to here", disabled: col + 1 === a.pinnedCount, onSelect: () => a.pin(col + 1) },
  ];
  if (a.pinnedCount > 0) items.push({ label: "Unpin columns", onSelect: () => a.pin(0) });
  return group(items);
}

/** Copy column name(s): the selected columns when the header is one of them, else just this one. */
function headerItems(col: number, a: GridMenuActions): MenuItem[] {
  const { rect } = a;
  const [left, right] = rect && col >= rect.left && col <= rect.right ? [rect.left, rect.right] : [col, col];
  const items: MenuItem[] = [
    {
      label: left === right ? "Copy column name" : "Copy column names",
      onSelect: () => void copyColumnNames(a.columns.slice(left, right + 1)),
    },
    ...pinItems(col, a),
  ];
  const { sortBy } = a;
  if (sortBy) {
    items.push(
      ...group([
        { label: "Sort ascending", onSelect: () => sortBy(col, false) },
        { label: "Sort descending", onSelect: () => sortBy(col, true) },
      ]),
    );
  }
  return items;
}

/** Copy, Copy as, the drawers; for a cell (`cell` set) also pinning and the caller's extra items. */
function selectionItems(pos: CellPos | null, cell: boolean, a: GridMenuActions): MenuItem[] {
  const { openValue, openRecord } = a;
  const items: MenuItem[] = [
    { label: "Copy", shortcut: "⌘C", onSelect: () => a.copy("tsv") },
    {
      label: "Copy as",
      children: COPY_FORMATS.map((format) => ({ label: COPY_FORMAT_LABELS[format], onSelect: () => a.copy(format) })),
    },
    {
      label: "View value",
      shortcut: "↵",
      separatorBefore: true,
      disabled: !openValue || !pos,
      onSelect: () => pos && openValue?.(pos),
    },
    { label: "Record view", shortcut: "Space", disabled: !openRecord || !pos, onSelect: () => pos && openRecord?.(pos) },
  ];
  if (cell && pos) {
    items.push(...pinItems(pos.col, a));
    if (a.extraCellMenu) items.push(...group(a.extraCellMenu(pos.row, pos.col)));
  }
  return items;
}

function gridMenuItems(target: GridHit, a: GridMenuActions): MenuItem[] {
  switch (target.kind) {
    case "header":
      return headerItems(target.col, a);
    case "cell":
      return selectionItems({ row: target.row, col: target.col }, true, a);
    case "rowNumber":
    case "corner":
      return selectionItems(a.focus, false, a);
  }
}

interface GridMenuProps extends GridMenuActions {
  /** What a context click at this event landed on; null: no menu (and no native one). */
  hitAt: (e: MouseEvent) => GridHit | null;
  /** The grid's scroll area: exactly one element. */
  children: ReactNode;
}

/**
 * The grid's context menus. The press before a context click has already updated the selection (kept when the click
 * lands inside it, see usePointerSelection); this only records where the click landed, and builds the items while
 * the menu is open.
 */
export function GridMenu({ hitAt, children, ...actions }: GridMenuProps) {
  const [target, setTarget] = useState<GridHit | null>(null);
  const onContextMenu = (e: MouseEvent) => {
    const hit = hitAt(e);
    if (!hit) e.preventDefault(); // keeps the menu closed
    setTarget(hit);
  };
  return (
    <ContextMenu
      items={target ? gridMenuItems(target, actions) : []}
      onContextMenu={onContextMenu}
      onOpenChange={(open) => !open && setTarget(null)}
    >
      {children}
    </ContextMenu>
  );
}
