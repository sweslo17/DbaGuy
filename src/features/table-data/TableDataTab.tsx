import { ChevronDown, ChevronRight, Loader2, MoreHorizontal, RefreshCw, SquareTerminal } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { DropdownMenu, type MenuItem } from "@/components/ui/ContextMenu";
import { Tabs } from "@/components/ui/Tabs";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import type { ColumnMeta, Dialect } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { isDialogOpen } from "@/lib/dialogs";
import { plural } from "@/lib/format";
import { isImeKeyEvent } from "@/lib/ime";
import { isCommandKey } from "@/lib/shortcuts";
import { readStagedEdits } from "@/stores/stagedEdits";
import { useReportTabStatus } from "@/stores/tabStatus";
import { useWorkspace, type TableTabState } from "@/stores/workspace";
import { useEngineInfo, useProfiles } from "../connections/hooks";
import { RecoverableError } from "../connections/RecoverableError";
import { openCopyDialog, openDumpDialog } from "../data-tools/dataDialogs";
import { copyEntryForTable, dumpEntryForTable } from "../data-tools/entryPoints";
import { COMMIT_RUNNING } from "../edits/commitDialog";
import { tableEditActions, useTableEditActions } from "../edits/editActions";
import { EditBar } from "../edits/EditBar";
import { resultColumns } from "../edits/overlay";
import { changeCount } from "../edits/staging";
import {
  asksBeforeDiscard,
  CLOSE_WHILE_COMMITTING,
  closeQuestion,
  discardQuestion,
  NOT_IN_EXPORT,
} from "../edits/tableEditing";
import { useCommit, useCommitState } from "../edits/useCommit";
import { useTableEdits } from "../edits/useTableEdits";
import { kindLabel } from "../explorer/kinds";
import { ColumnsView, DdlView } from "../explorer/ObjectDetailView";
import { DataGrid, type DataGridHandle } from "../grid/DataGrid";
import { ExportDialog } from "../grid/ExportDialog";
import type { GridSort } from "../grid/GridHeader";
import { useHeldResult } from "../grid/useHeldResult";
import { CONNECTION_LOST, EXPORT_RUNNING } from "../session/execution";
import { isExporting } from "../session/SessionController";
import { useSessionGuards } from "../session/SessionGuards";
import { CLOSE_GUARD_ORDER, useCloseGuard, useDiscardQuestion } from "../workspace/closeGuards";
import { browseView, keptNotice } from "./browseView";
import { FilterBar } from "./FilterBar";
import { addCondition, valueCondition, type Relation } from "./filterValue";
import { useTableTabActions } from "./tableTabActions";
import { useAnchoredRerun } from "./useAnchoredRerun";
import { useTableData, type TableDataState } from "./useTableData";

type View = "data" | "columns" | "ddl";
const FILTER_BY_VALUE = "Filter by this value";
const CUT_VALUE_MESSAGE = "This value cannot be matched exactly: the grid holds only its beginning";
/** Dump table…'s and Copy rows to…'s tooltip while a filter is applied: the dialog offers its rows only, or all of
 * them. */
const WITH_FILTER = "With the rows matching the filter, or all of them";
const NO_DUMP = "This table cannot be dumped from here";
const NO_COPY = "This table cannot be copied from here"; // an engine that does not copy (06c)
const VIEWS: { value: View; label: string }[] = [
  { value: "data", label: "Data" },
  { value: "columns", label: "Columns" },
  { value: "ddl", label: "DDL" },
];

/**
 * How long a Cancel that takes Refresh's place ignores clicks (05b K12): the second click of a double click on Refresh
 * lands on it, and must not cancel the re-run the first one started.
 */
const CANCEL_ARMS_AFTER_MS = 400;

/** Cancel, in Refresh's place while a browse's first page loads; a click in its first moments is not one on it. */
function FirstPageCancel({ disabled, onCancel }: { disabled: boolean; onCancel: () => void }) {
  const [shownAt] = useState(() => performance.now());
  return (
    <Button
      size="sm"
      onClick={() => {
        if (performance.now() - shownAt >= CANCEL_ARMS_AFTER_MS) onCancel();
      }}
      disabled={disabled}
    >
      Cancel
    </Button>
  );
}

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
  const engine = useEngineInfo(tab.profileId);
  const dialect = engine?.dialect ?? null;
  const openQuery = useWorkspace((s) => s.openQuery);
  const profiles = useProfiles();
  const profile = profiles.data?.find((entry) => entry.profile.id === tab.profileId)?.profile;
  const profileName = profile?.name;
  const guards = useSessionGuards(table.controller, profileName ? `${tab.title} · ${profileName}` : tab.title);
  useCloseGuard(tab.id, CLOSE_GUARD_ORDER.export, (intent) => guards.exportStep(intent));
  const [exportOpen, setExportOpen] = useState(false);
  const openExport = useCallback(() => setExportOpen(true), []);
  const phase = snapshot?.phase ?? "opening";
  // Until a sort, filter or refresh has its whole first page, the grid keeps the previous rows (and its layout),
  // dimmed: it never flashes empty, and a re-run that keeps the position finds it (A12). A Refresh that ends before
  // its first page is complete (cancelled, failed, interrupted) leaves those rows as they were.
  const view = browseView(execution, table.rerun);
  const shown = useHeldResult(view.set, table.pending, Object.is, view.keepPrevious);
  const set = shown.value;
  const columns = set?.columns;
  const rows = set?.rows;
  const firstPagePending = execution?.status === "running" && !view.set;
  // The rows on screen are those from before a Refresh that ended early: their request is gone, nothing more to load.
  const kept = view.keepPrevious && !!set;
  const notice = keptNotice(execution, view, !!set);
  const filterByValue = useFilterByValue(state.filter, applyFilter);
  // An export runs on this session: filter, sort and refresh would only wait behind it.
  const exporting = isExporting(snapshot);
  const gridRef = useRef<DataGridHandle>(null);
  // The commit's dialog and afterglow are held here, above the edits: a running commit locks the staging, and until
  // the re-read is in the grid draws the staging as it was before the commit (pre-flight M5), in the same render.
  const committing = useCommitState({ deliveredRequest: table.deliveredRequest, position: table.position });
  // A commit's re-read has been asked for and not delivered yet: its rows count as loading from the moment it is asked
  // (before it is sent, the rows shown are not stale yet), so nothing says "Refresh first" meanwhile.
  const rereading = table.pending && committing.afterglow !== null;
  const openAfterEdit = useRef<() => void>(() => undefined);
  const edits = useTableEdits({
    tab,
    columns,
    rows,
    rowEditing: engine?.row_editing === true,
    sessionOpen: phase === "open",
    stale: shown.stale || rereading,
    locked: committing.applying ? COMMIT_RUNNING : null,
    gridRef,
    onRequestCommit: () => openAfterEdit.current(),
    afterglow: committing.afterglow,
  });
  const [askingDiscard, setAskingDiscard] = useState(false);
  /** Discard…: more than one change asks first (⌘Z brings them back either way). */
  const requestDiscard = () => (asksBeforeDiscard(edits.changes) ? setAskingDiscard(true) : edits.discardAll());
  const refresh = () => {
    table.refresh();
    edits.refetchTarget(); // DDL since: the next edit uses the table as it is now (spec §3.2.1)
  };
  const commit = useCommit({
    tab,
    table: relation ? qualifiedName(relation) : tab.title,
    profile,
    dialect,
    controller: table.controller,
    snapshot,
    browse: {
      execution,
      // The rows are being read: held while a re-run loads them, or the first ones not there yet.
      pending: shown.stale || rereading || (table.pending && !set),
      deliveredRequest: table.deliveredRequest,
      position: table.position,
      reread: table.refresh,
      refresh,
    },
    edits,
    exporting,
    gridRef,
    retrySession: table.retry,
    holder: committing,
  });
  useLayoutEffect(() => {
    openAfterEdit.current = commit.openAfterEdit;
  });
  // Closing asks while changes are staged (R-05-11: Discard / Cancel, no Commit: a commit needs its preview), and is
  // refused while a commit runs. Registered for every table tab, shown or not: a restored tab's staging is in the store
  // (C28). An open editor's text is staged first, so it is counted and asked about (L8; never during render or an
  // effect: flushSync). Losing the connection does not ask (close-only), nor does quitting (the staging is saved).
  /** The question ended the shown tab's open editor: its input had the focus, so Cancel gives it to the grid. */
  const refocusOnKeep = useRef(false);
  const closing = useDiscardQuestion(tab.id, CLOSE_GUARD_ORDER.edits, () => {
    if (committing.read()?.step === "applying") return { refuse: CLOSE_WHILE_COMMITTING };
    refocusOnKeep.current = edits.flushEditor() && active;
    const staged = readStagedEdits(tab.id);
    return staged !== null && changeCount(staged) > 0;
  });
  const keepTab = () => {
    closing.answer(false);
    if (!refocusOnKeep.current) return;
    refocusOnKeep.current = false;
    // Once the dialog has closed: it gives the focus back to what had it when it opened, and that was the page. Never
    // over a focus something else took meanwhile.
    requestAnimationFrame(() => {
      const lost = !document.activeElement || document.activeElement === document.body;
      if (lost && !isDialogOpen()) gridRef.current?.focus();
    });
  };
  // The tab strip's dot: staged changes are unsaved work.
  useReportTabStatus(tab.id, { dirty: edits.changes > 0 });
  // ⌘⇧P's edit commands (spec ⑤ §3.6.4), through the same handlers as the grid's keys and the edit bar (H11).
  useTableEditActions(
    tab.id,
    tableEditActions(
      {
        editable: edits.editable,
        shown: active,
        changes: edits.changes,
        commitBlocked: commit.disabledReason,
        applying: commit.applying,
        undoable: (edits.staged?.past.length ?? 0) > 0,
        redoable: (edits.staged?.future.length ?? 0) > 0,
      },
      {
        addRow: edits.addRow,
        deleteRows: edits.deleteSelected,
        setNull: edits.setNullSelected,
        commit: commit.open,
        discardAll: requestDiscard,
        undo: edits.undo,
        redo: edits.redo,
      },
    ),
  );
  // Dump table… (spec ⑥ §3.6), with the filter applied now; the dialog says when the tab has staged changes (L8)
  const dumpEntry = useMemo(() => {
    const entry = dumpEntryForTable(tab.profileId, engine, tab.path, state.filter);
    return entry && { ...entry, tabId: tab.id };
  }, [tab.profileId, tab.id, tab.path, engine, state.filter]);
  const openDump = useCallback(() => {
    if (dumpEntry) openDumpDialog(dumpEntry);
  }, [dumpEntry]);
  // Copy rows to… (spec ⑥ §3.6, 06c), with the filter applied now; the dialog says when the tab has staged changes
  const copyEntry = useMemo(
    () => copyEntryForTable(tab.profileId, engine, tab.path, state.filter, tab.id),
    [tab.profileId, tab.id, tab.path, engine, state.filter],
  );
  const openCopy = useCallback(() => {
    if (copyEntry) openCopyDialog(copyEntry);
  }, [copyEntry]);
  useTableTabActions(tab.id, {
    dumpTable: { enabled: dumpEntry !== null, run: openDump },
    copyRows: { enabled: copyEntry !== null, run: openCopy },
  });
  const moreItems: MenuItem[] = [
    {
      label: "Dump table…",
      disabled: !dumpEntry,
      title: !dumpEntry
        ? engine
          ? NO_DUMP
          : "Reading the connection's details…"
        : dumpEntry.filter !== null
          ? WITH_FILTER
          : undefined,
      onSelect: openDump,
    },
    {
      label: "Copy rows to…",
      disabled: !copyEntry,
      title: !copyEntry
        ? engine
          ? NO_COPY
          : "Reading the connection's details…"
        : copyEntry.source.kind === "relation" && copyEntry.source.filter !== null
          ? WITH_FILTER
          : undefined,
      onSelect: openCopy,
    },
  ];
  // ⌘S while this tab's Data view is shown, wherever the focus is in it (an editor stages first and asks itself):
  // never the browser's Save page, nothing while a dialog has the keyboard or an input method composes.
  const openCommit = useRef(commit.open);
  useLayoutEffect(() => {
    openCommit.current = commit.open;
  });
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (isImeKeyEvent(e) || e.defaultPrevented || !isCommandKey(e, "s")) return;
      e.preventDefault();
      if (!isDialogOpen()) openCommit.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active]);
  const tableKey = table.target?.key;
  const keyIndexes = useMemo(
    () => (columns && tableKey ? resultColumns(columns, tableKey).keyIndexes : null),
    [columns, tableKey],
  );
  useAnchoredRerun(
    gridRef,
    { request: state.requestId, position: table.position, view },
    columns,
    rows,
    keyIndexes,
    edits.display.leading, // the new rows staged on top: the selection was made over them
  );

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
  const { resultRowAt } = edits;
  const { leading } = edits.display;
  const extraCellMenu = useCallback(
    (row: number, col: number): MenuItem[] => {
      if (row < leading) return []; // a new row has no value on the server to filter by (§3.6.6)
      const column = columns?.[col];
      // The loaded row under the display row: a changed cell filters by the server's value, not the staged one.
      const source = resultRowAt(row);
      if (!column || !source || !dialect || exporting) return [{ label: FILTER_BY_VALUE, disabled: true }];
      const value = source[col];
      return [{ label: FILTER_BY_VALUE, onSelect: () => void filterByValue(dialect, column, value) }];
    },
    [columns, resultRowAt, leading, dialect, exporting, filterByValue],
  );
  // Export writes the current browse SELECT (filter, sort and refresh wait while it runs): what the server holds.
  const exportNote = edits.changes > 0 ? NOT_IN_EXPORT : null;
  const exportSource = useMemo(
    () =>
      table.controller && execution && relation
        ? { controller: table.controller, sql: execution.sql, title: relation.name, tableName, note: exportNote }
        : null,
    [table.controller, execution, relation, tableName, exportNote],
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
          <FirstPageCancel onCancel={table.cancel} disabled={phase !== "open"} />
        ) : (
          <Button
            size="sm"
            onClick={refresh}
            disabled={phase !== "open" || exporting}
            title={exporting ? EXPORT_RUNNING : "Run the query again"}
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
        <div className="flex max-w-[60%] min-w-0 shrink justify-end">
          <EditBar
            editable={edits.tableEditable}
            paused={edits.paused}
            checking={edits.checking}
            readOnlyReason={edits.readOnlyReason}
            targetError={edits.targetError}
            onRetryTarget={edits.refetchTarget}
            changes={edits.changes}
            notShown={edits.display.notShown}
            notKept={edits.notKept}
            keyChanged={edits.keyChanged}
            unknownOutcome={edits.staged?.unknownOutcome === "pending"}
            onAddRow={edits.addRow}
            onDiscard={requestDiscard}
            onCommit={commit.open}
            commitDisabled={commit.disabledReason}
          />
        </div>
        <DropdownMenu items={moreItems} align="end">
          <Button variant="ghost" size="sm" className="shrink-0 px-1.5" aria-label="More" title="More">
            <MoreHorizontal size={14} />
          </Button>
        </DropdownMenu>
      </div>
      <RecoverableError profileId={tab.profileId} error={snapshot?.error ?? null} onRetry={table.retry} />
      {/* The edit target still wants a password after the session opened (L13): an unlock, then it is read again. */}
      <RecoverableError profileId={tab.profileId} error={edits.targetUnlock} onRetry={edits.refetchTarget} />
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
      {notice && <div className="shrink-0 border-b border-border px-3 py-1.5 text-[12px] text-muted">{notice}</div>}
      {state.sql && <SqlRow sql={state.sql} onOpen={openInQueryTab} />}
      <div className="min-h-0 flex-1">
        {set ? (
          <DataGrid
            ref={gridRef}
            columns={set.columns}
            rows={edits.display.rows}
            hasMore={set.hasMore && !kept}
            partial={set.partial || view.partial || (kept && set.hasMore)}
            loadingMore={execution?.status === "running"}
            onLoadMore={phase === "open" && !kept ? table.loadMore : undefined}
            stale={shown.stale}
            resetScrollOn={table.position}
            dialect={dialect}
            tableName={tableName}
            sort={gridSort}
            onHeaderClick={onHeaderClick}
            onSortBy={exporting ? undefined : onSortBy}
            extraCellMenu={extraCellMenu}
            onExport={exportSource ? openExport : undefined}
            edits={edits.grid}
            onSelectionChange={edits.onSelectionChange}
          />
        ) : (
          placeholder && <p className="p-4 text-[12px] text-muted">{placeholder}</p>
        )}
      </div>
      {exportSource && <ExportDialog source={exportSource} open={exportOpen} onOpenChange={setExportOpen} />}
      <ConfirmDialog
        open={askingDiscard}
        onOpenChange={setAskingDiscard}
        title="Discard staged changes"
        message={discardQuestion(edits.changes, relation?.name ?? tab.title)}
        confirmLabel="Discard"
        onConfirm={() => {
          setAskingDiscard(false);
          edits.discardAll();
        }}
      />
      <ConfirmDialog
        open={closing.asking}
        onOpenChange={(open) => !open && keepTab()}
        title="Close tab"
        message={closeQuestion(edits.changes, relation?.name ?? tab.title)}
        confirmLabel="Discard"
        onConfirm={() => closing.answer(true)}
      />
      {commit.dialog}
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
