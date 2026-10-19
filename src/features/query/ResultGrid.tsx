import { memo, useCallback } from "react";
import type { MenuItem } from "@/components/ui/ContextMenu";
import type { Dialect } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { askOpenDocument, idPrefill, prefillKeyspace } from "../documents/openDocument";
import { DataGrid } from "../grid/DataGrid";
import type { ResultSetState } from "../session/execution";
import type { RequestOptions, SessionController } from "../session/SessionController";

export interface ResultGridProps {
  set: ResultSetState;
  /** The execute request the set belongs to (Load more). */
  requestId: string;
  /** Its statement (Export runs it again). */
  sql: string;
  /** What the statement was sent with (its query context and parameters): Export sends the same. */
  request: RequestOptions;
  /** Rows of this set are still arriving. */
  loadingMore: boolean;
  /** The rows belong to the previous run (shown dimmed until the new run delivers a result). */
  stale: boolean;
  /** The run is over and the session open: a paused set may load its next page. */
  canLoadMore: boolean;
  /** The run is over: an export would not have to wait behind its statements. */
  exportable: boolean;
  /** Opens the tab's export dialog for this statement and what it was sent with; stable across renders. */
  onExport: (sql: string, request: RequestOptions) => void;
  controller: SessionController | null;
  pageSize: number;
  dialect: Dialect | null;
  /** The connection when its engine opens documents by ID (Couchbase): a cell's menu then offers "Open document…". */
  documentsProfileId: string | null;
  hidden: boolean;
}

/**
 * One result tab's grid. Memoised on primitive or stable props: a session event re-renders only the grid whose set
 * changed (the reducer keeps the identity of every untouched execution and set), not every grid of the run.
 */
export const ResultGrid = memo(function ResultGrid(props: ResultGridProps) {
  const { set, requestId, sql, request, controller, pageSize, canLoadMore, exportable, onExport } = props;
  const { documentsProfileId } = props;
  const loadMore = useCallback(() => controller?.fetchMore(requestId, pageSize), [controller, requestId, pageSize]);
  const exportThis = useCallback(() => onExport(sql, request), [onExport, sql, request]);
  // "Open document…": the ID is the row's `id` column or the clicked text, the collection the one the statement reads
  // (when it reads exactly one), else the run's context and the collection opened last (ruling C44).
  const openDocumentItems = useCallback(
    (row: number, col: number): MenuItem[] => [
      {
        label: "Open document…",
        onSelect: () => {
          if (documentsProfileId === null) return;
          const id = idPrefill(set.columns, set.rows[row] ?? [], col); // as the cell is now: more rows may arrive
          void prefillKeyspace(sql, request.queryContext).then((keyspace) =>
            askOpenDocument({ profileId: documentsProfileId, mode: "open", keyspace, id }),
          );
        },
      },
    ],
    [documentsProfileId, sql, request, set.columns, set.rows],
  );
  return (
    <div className={cn("h-full", props.hidden && "hidden")}>
      <DataGrid
        columns={set.columns}
        rows={set.rows}
        hasMore={set.hasMore}
        partial={set.partial}
        loadingMore={props.loadingMore}
        onLoadMore={canLoadMore ? loadMore : undefined}
        stale={props.stale}
        dialect={props.dialect}
        extraCellMenu={documentsProfileId === null ? undefined : openDocumentItems}
        onExport={controller && exportable ? exportThis : undefined}
      />
    </div>
  );
});
