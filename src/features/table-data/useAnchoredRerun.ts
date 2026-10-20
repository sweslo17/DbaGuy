import { useLayoutEffect, useRef, type RefObject } from "react";
import type { ColumnMeta } from "@/lib/api/types";
import { columnSignature, ROW_HEIGHT } from "../grid/columns";
import type { DataGridHandle } from "../grid/DataGrid";
import { captureAnchor, placeAnchor } from "./anchor";
import type { BrowseView } from "./browseView";

/**
 * The rows on screen: their request, the position and columns it was shown with, the loaded rows themselves, and the
 * new rows shown above them (as of the last render: what the selection and scroll were made over).
 */
interface ShownRows {
  request: string | null;
  position: number;
  signature: string | null;
  rows: unknown[][] | null;
  leading: number;
}

/**
 * K14: when a re-run of the same browse (a Refresh, the re-read after a commit) replaces the rows on screen with the
 * same columns, the grid's place is put back by key (`anchor.ts`): captured from the rows it replaces, before the new
 * ones are painted. A sort or filter starts at the top (its position changed), and so does an engine's Refresh that
 * does not keep the position (CR-1); a query tab never gets here; without the table's key in the result the grid keeps
 * its position as before (Task 6).
 *
 * `rows` are the loaded rows (never the display rows: a staged key cell is not the row's key), and `leading` the new
 * rows the grid shows above them now (Task 9's overlay; 0 until then). The place is captured with the `leading` of the
 * last render before the new rows (what the selection was made over) and put back with the current one: after a
 * commit, the committed new rows are no longer on top.
 */
export function useAnchoredRerun(
  grid: RefObject<DataGridHandle | null>,
  shown: { request: string | null; position: number; view: BrowseView },
  columns: ColumnMeta[] | undefined,
  rows: unknown[][] | undefined,
  keyIndexes: readonly number[] | null,
  leading: number,
) {
  const last = useRef<ShownRows>({ request: null, position: 0, signature: null, rows: null, leading: 0 });
  useLayoutEffect(() => {
    const before = last.current;
    // The grid shows the request's own rows once its first page is in; until then (or after one that ended early),
    // the rows of the request before it.
    const request = shown.view.set ? shown.request : before.request;
    if (request === before.request) {
      last.current = { ...before, rows: rows ?? null, leading }; // the same request's rows, a page longer or held
      return;
    }
    const signature = columns ? columnSignature(columns) : null;
    last.current = { request, position: shown.position, signature, rows: rows ?? null, leading };
    // Only the same browse again keeps its position (a sort or a filter is another browse), and only the same columns
    if (shown.position !== before.position || signature !== before.signature) return;
    const handle = grid.current;
    const where = handle?.view();
    if (!handle || !where || !keyIndexes || !rows || !before.rows) return;
    const anchor = captureAnchor({
      rows: before.rows,
      keyIndexes,
      selection: where.selection,
      scrollTop: where.scrollTop,
      viewportHeight: where.viewportHeight,
      rowHeight: ROW_HEIGHT,
      leading: before.leading,
    });
    if (anchor) {
      handle.restore(
        placeAnchor(anchor, { rows, keyIndexes, rowHeight: ROW_HEIGHT, leading, maxScrollTop: where.maxScrollTop }),
      );
    }
  });
}
