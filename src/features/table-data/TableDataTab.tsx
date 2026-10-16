import { ChevronDown, ChevronRight, Loader2, RefreshCw, SquareTerminal } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { Button } from "@/components/ui/Button";
import type { MenuItem } from "@/components/ui/ContextMenu";
import { Tabs } from "@/components/ui/Tabs";
import { cn } from "@/lib/cn";
import { plural } from "@/lib/format";
import { useWorkspace, type ObjectTab } from "@/stores/workspace";
import { useDialect } from "../connections/hooks";
import { ColumnsView, DdlView } from "../explorer/ObjectDetailView";
import { kindLabel } from "../explorer/kinds";
import { DataGrid } from "../grid/DataGrid";
import type { GridSort } from "../grid/GridHeader";
import { CONNECTION_LOST } from "../session/execution";
import { SessionError } from "../session/SessionError";
import { FilterBar } from "./FilterBar";
import { addCondition, filterForValue, type Relation } from "./filterValue";
import { useTableData, type TableDataState } from "./useTableData";

type View = "data" | "columns" | "ddl";
const VIEWS: { value: View; label: string }[] = [
  { value: "data", label: "Data" },
  { value: "columns", label: "Columns" },
  { value: "ddl", label: "DDL" },
];

/** Count rows, then "N rows" (or the error) beside it; a spinner and Cancel while it runs. */
function CountControl({
  count,
  disabled,
  onCount,
  onCancel,
}: {
  count: TableDataState["count"];
  disabled: boolean;
  onCount: () => void;
  onCancel: () => void;
}) {
  if (count.status === "running") {
    return (
      <div className="flex shrink-0 items-center gap-1.5 text-[12px] text-muted">
        <Loader2 size={12} className="animate-spin" />
        Counting…
        <Button size="sm" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    );
  }
  return (
    <div className="flex min-w-0 shrink-0 items-center gap-1.5 text-[12px]">
      <Button size="sm" onClick={onCount} disabled={disabled}>
        Count rows
      </Button>
      {count.status === "done" && count.value !== undefined && (
        <span className="text-muted" data-selectable>
          {plural(count.value, "row")}
        </span>
      )}
      {count.status === "error" && (
        <span className="max-w-[260px] truncate text-danger" title={count.error} data-selectable>
          {count.error}
        </span>
      )}
    </div>
  );
}

/** The generated SELECT: one line, or all of it when expanded; "Open in query tab" runs it from a query tab. */
function SqlRow({ sql, onOpen }: { sql: string; onOpen: () => void }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="flex shrink-0 items-start gap-1 border-b border-border px-2 py-1 text-[12px]">
      <button
        type="button"
        className="mt-px rounded text-muted hover:bg-hover hover:text-fg"
        aria-expanded={expanded}
        title={expanded ? "Collapse the SQL" : "Show the whole SQL"}
        onClick={() => setExpanded((open) => !open)}
      >
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>
      {expanded ? (
        <pre className="max-h-40 min-w-0 flex-1 overflow-auto font-mono whitespace-pre-wrap" data-selectable>
          {sql}
        </pre>
      ) : (
        <code className="min-w-0 flex-1 truncate font-mono text-muted" title={sql} data-selectable>
          {sql.replace(/\s+/g, " ")}
        </code>
      )}
      <Button size="sm" variant="ghost" className="-my-0.5 shrink-0" onClick={onOpen}>
        <SquareTerminal size={12} /> Open in query tab
      </Button>
    </div>
  );
}

/** The Data view: filter, sort, count and the paged rows of the relation, on the tab's own session. */
function DataView({ tab, active }: { tab: ObjectTab; active: boolean }) {
  const table = useTableData(tab, active);
  const { state, execution, snapshot, relation } = table;
  const { applyFilter, toggleSort, sortBy } = table;
  const dialect = useDialect(tab.profileId);
  const openQuery = useWorkspace((s) => s.openQuery);
  const phase = snapshot?.phase ?? "opening";
  const set = execution?.resultSets[0] ?? null;
  const columns = set?.columns;
  const rows = set?.rows;
  const firstPagePending = execution?.status === "running" && !set;

  const gridSort = useMemo<GridSort[]>(
    () =>
      state.sorts.flatMap((key) => {
        const column = columns?.findIndex((c) => c.name === key.column) ?? -1;
        return column < 0 ? [] : [{ column, descending: key.descending }];
      }),
    [state.sorts, columns],
  );
  const tableName = useMemo(() => relation && { schema: relation.schema, name: relation.name }, [relation]);
  const onHeaderClick = useCallback(
    (col: number, additive: boolean) => columns && toggleSort(columns[col].name, additive),
    [columns, toggleSort],
  );
  const onSortBy = useCallback(
    (col: number, descending: boolean) => columns && sortBy(columns[col].name, descending),
    [columns, sortBy],
  );
  const extraCellMenu = useCallback(
    (row: number, col: number): MenuItem[] => {
      const column = columns?.[col];
      const condition =
        column && rows && row < rows.length && dialect ? filterForValue(column, rows[row][col], dialect) : null;
      return [
        {
          label: "Filter by this value",
          disabled: !condition,
          onSelect: () => condition && applyFilter(addCondition(state.filter, condition)),
        },
      ];
    },
    [columns, rows, dialect, applyFilter, state.filter],
  );

  const openInQueryTab = () => {
    if (state.sql) openQuery(tab.profileId, relation?.database ?? null, tab.subtitle, state.sql);
  };
  const placeholder = (() => {
    if (phase === "failed" || state.error) return null; // shown above
    if (phase === "opening") return "Connecting…";
    if (execution?.status === "done") return execution.cancelled ? "Cancelled" : null;
    return "Loading…";
  })();

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-border px-2">
        <FilterBar
          applied={state.filter}
          history={table.history}
          invalid={!!state.error}
          onApply={applyFilter}
          onRevert={table.clearFilterError}
          onClearHistory={table.clearHistory}
        />
        {firstPagePending ? (
          <Button size="sm" onClick={table.cancel} disabled={phase !== "open"}>
            Cancel
          </Button>
        ) : (
          <Button size="sm" onClick={table.refresh} disabled={phase !== "open"} title="Run the query again">
            <RefreshCw size={12} /> Refresh
          </Button>
        )}
        <CountControl
          count={state.count}
          disabled={!state.sql}
          onCount={table.countRows}
          onCancel={table.cancelCount}
        />
      </div>
      <SessionError profileId={tab.profileId} error={snapshot?.error ?? null} onRetry={table.retry} />
      {state.error && (
        <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-1.5 text-[12px] text-danger">
          <span className="min-w-0 flex-1 whitespace-pre-wrap" data-selectable>
            {state.error}
          </span>
          {execution?.error?.code === CONNECTION_LOST && (
            <Button size="sm" onClick={table.retry}>
              Reconnect
            </Button>
          )}
        </div>
      )}
      {state.sql && <SqlRow sql={state.sql} onOpen={openInQueryTab} />}
      <div className="min-h-0 flex-1">
        {set ? (
          <DataGrid
            columns={set.columns}
            rows={set.rows}
            hasMore={set.hasMore}
            loadingMore={execution?.status === "running"}
            onLoadMore={phase === "open" ? table.loadMore : undefined}
            dialect={dialect}
            tableName={tableName}
            sort={gridSort}
            onHeaderClick={onHeaderClick}
            onSortBy={onSortBy}
            extraCellMenu={extraCellMenu}
          />
        ) : (
          placeholder && <p className="p-4 text-[12px] text-muted">{placeholder}</p>
        )}
      </div>
    </div>
  );
}

function qualifiedName(relation: Relation): string {
  return relation.schema ? `${relation.schema}.${relation.name}` : relation.name;
}

/**
 * A table, view, materialized view or partition (spec §4.5): Data (default) | Columns | DDL. The Data view stays
 * mounted while another view is shown, so its rows, sort and scroll position survive; its session opens the first
 * time it is shown (`active`: this workspace tab is the current one) and closes with the tab.
 */
export function TableDataTab({ tab, relation, active }: { tab: ObjectTab; relation: Relation; active: boolean }) {
  const [view, setView] = useState<View>("data");
  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-end">
        <Tabs value={view} onValueChange={setView} items={VIEWS} className="flex-1 pl-2" />
        <div className="flex min-w-0 items-center gap-2 border-b border-border pr-3 pb-1.5 pl-3 text-[12px]">
          <span className="shrink-0 text-[11px] tracking-wide text-muted uppercase">{kindLabel(relation.kind)}</span>
          <span className="truncate font-medium" data-selectable>
            {qualifiedName(relation)}
          </span>
        </div>
      </div>
      <div className={cn("min-h-0 flex-1", view !== "data" && "hidden")}>
        <DataView tab={tab} active={active && view === "data"} />
      </div>
      {view === "columns" && (
        <div className="min-h-0 flex-1">
          <ColumnsView tab={tab} />
        </div>
      )}
      {view === "ddl" && (
        <div className="min-h-0 flex-1">
          <DdlView tab={tab} />
        </div>
      )}
    </div>
  );
}
