import { ChevronDown, ChevronRight, Loader2, RefreshCw, SquareTerminal } from "lucide-react";
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import type { MenuItem } from "@/components/ui/ContextMenu";
import { Tabs } from "@/components/ui/Tabs";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import type { ColumnMeta, Dialect } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { plural } from "@/lib/format";
import { useWorkspace, type TableTabState } from "@/stores/workspace";
import { useDialect, useProfiles } from "../connections/hooks";
import { RecoverableError } from "../connections/RecoverableError";
import { ColumnsView, DdlView } from "../explorer/ObjectDetailView";
import { kindLabel } from "../explorer/kinds";
import { DataGrid } from "../grid/DataGrid";
import { ExportDialog } from "../grid/ExportDialog";
import type { GridSort } from "../grid/GridHeader";
import { useHeldResult } from "../grid/useHeldResult";
import { CONNECTION_LOST } from "../session/execution";
import { isExporting } from "../session/SessionController";
import { useSessionGuards } from "../session/SessionGuards";
import { CLOSE_GUARD_ORDER, useCloseGuard } from "../workspace/closeGuards";
import { FilterBar } from "./FilterBar";
import { addCondition, valueCondition, type Relation } from "./filterValue";
import { useTableData, type TableDataState } from "./useTableData";

type View = "data" | "columns" | "ddl";
const FILTER_BY_VALUE = "Filter by this value";
const CUT_VALUE_MESSAGE = "This value cannot be matched exactly: the grid holds only its beginning";
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

/**
 * "Filter by this value" (spec §3.7): each activation asks the backend for its cell's condition. The newest activation
 * applies every condition still pending (its own and those of the activations it superseded), in click order, onto the
 * latest filter; a superseded activation's response is ignored, so overlapping clicks never drop a condition and never
 * apply out of order.
 */
function useFilterByValue(filter: string, applyFilter: (text: string) => void) {
  // The filter the next conditions go onto: the applied one, or the one just requested while its SELECT is being built
  // (the applied filter changes only once that build is done). Synced on change only, so a re-render in between
  // cannot put the older filter back.
  const latestFilter = useRef(filter);
  useLayoutEffect(() => {
    latestFilter.current = filter;
  }, [filter]);
  const apply = useRef(applyFilter);
  useLayoutEffect(() => {
    apply.current = applyFilter;
  });
  const pending = useRef<Promise<string | null>[]>([]);
  const activations = useRef(0);

  return useCallback(async (dialect: Dialect, column: ColumnMeta, value: unknown) => {
    const ticket = ++activations.current;
    pending.current.push(valueCondition(dialect, column, value));
    const outcomes = await Promise.allSettled(pending.current);
    if (ticket !== activations.current) return; // superseded: the newer activation applies this condition too
    pending.current = [];
    let next = latestFilter.current;
    let cut = false;
    for (const outcome of outcomes) {
      if (outcome.status === "rejected") toast("error", errorMessage(outcome.reason));
      else if (outcome.value === null) cut = true;
      else next = addCondition(next, outcome.value);
    }
    if (cut) toast("info", CUT_VALUE_MESSAGE);
    if (next === latestFilter.current) return;
    latestFilter.current = next;
    apply.current(next);
  }, []);
}

/** The Data view: filter, sort, count and the paged rows of the relation, on the tab's own session. */
function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
  const table = useTableData(tab, active);
  const { state, execution, snapshot, relation } = table;
  const { applyFilter, toggleSort, sortBy } = table;
  const dialect = useDialect(tab.profileId);
  const openQuery = useWorkspace((s) => s.openQuery);
  const profiles = useProfiles();
  const profileName = profiles.data?.find((entry) => entry.profile.id === tab.profileId)?.profile.name;
  const guards = useSessionGuards(table.controller, profileName ? `${tab.title} · ${profileName}` : tab.title);
  useCloseGuard(tab.id, CLOSE_GUARD_ORDER.export, (intent) => guards.exportStep(intent));
  const [exportOpen, setExportOpen] = useState(false);
  const openExport = useCallback(() => setExportOpen(true), []);
  const phase = snapshot?.phase ?? "opening";
  const current = execution?.resultSets[0] ?? null;
  // Until a sort, filter or refresh delivers its first page, the grid keeps the previous rows (and its layout), dimmed.
  const shown = useHeldResult(current, table.pending);
  const set = shown.value;
  const columns = set?.columns;
  const rows = set?.rows;
  const firstPagePending = execution?.status === "running" && !current;
  const filterByValue = useFilterByValue(state.filter, applyFilter);
  // An export runs on this session: filter, sort and refresh would only wait behind it.
  const exporting = isExporting(snapshot);

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
    (col: number, additive: boolean) => {
      if (columns && !exporting) toggleSort(columns[col].name, additive);
    },
    [columns, toggleSort, exporting],
  );
  const onSortBy = useCallback(
    (col: number, descending: boolean) => columns && sortBy(columns[col].name, descending),
    [columns, sortBy],
  );
  const extraCellMenu = useCallback(
    (row: number, col: number): MenuItem[] => {
      const column = columns?.[col];
      if (!column || !rows || row >= rows.length || !dialect || exporting) {
        return [{ label: FILTER_BY_VALUE, disabled: true }];
      }
      const value = rows[row][col];
      return [{ label: FILTER_BY_VALUE, onSelect: () => void filterByValue(dialect, column, value) }];
    },
    [columns, rows, dialect, exporting, filterByValue],
  );
  // Export writes the current browse SELECT (filter, sort and refresh wait while it runs).
  const exportSource = useMemo(
    () =>
      table.controller && execution && relation
        ? { controller: table.controller, sql: execution.sql, title: relation.name, tableName }
        : null,
    [table.controller, execution, relation, tableName],
  );

  const openInQueryTab = () => {
    if (state.sql) openQuery(tab.profileId, relation?.database ?? null, { text: state.sql });
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
          rejected={!!state.filterError}
          onApply={applyFilter}
          onClearError={table.clearFilterError}
          onClearHistory={table.clearHistory}
          disabled={exporting}
        />
        {firstPagePending ? (
          <Button size="sm" onClick={table.cancel} disabled={phase !== "open"}>
            Cancel
          </Button>
        ) : (
          <Button
            size="sm"
            onClick={table.refresh}
            disabled={phase !== "open" || exporting}
            title={exporting ? "An export is running on this session" : "Run the query again"}
          >
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
      <RecoverableError profileId={tab.profileId} error={snapshot?.error ?? null} onRetry={table.retry} />
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
            stale={shown.stale}
            dialect={dialect}
            tableName={tableName}
            sort={gridSort}
            onHeaderClick={onHeaderClick}
            onSortBy={exporting ? undefined : onSortBy}
            extraCellMenu={extraCellMenu}
            onExport={exportSource ? openExport : undefined}
          />
        ) : (
          placeholder && <p className="p-4 text-[12px] text-muted">{placeholder}</p>
        )}
      </div>
      {exportSource && <ExportDialog source={exportSource} open={exportOpen} onOpenChange={setExportOpen} />}
      {guards.dialog}
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
export function TableDataTab({ tab, relation, active }: { tab: TableTabState; relation: Relation; active: boolean }) {
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
