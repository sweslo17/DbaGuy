import { Prec, type Extension } from "@codemirror/state";
import { keymap, placeholder, type EditorView } from "@codemirror/view";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Group, Panel } from "react-resizable-panels";
import { Button } from "@/components/ui/Button";
import { PanelHandle } from "@/components/ui/PanelHandle";
import type { Dialect } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { plural } from "@/lib/format";
import { useAppSettings } from "@/stores/settings";
import type { QueryTabState } from "@/stores/workspace";
import { useDialect, useProfiles } from "../connections/hooks";
import { CodeEditor, type CodeEditorHandle } from "../editor/CodeEditor";
import { DataGrid } from "../grid/DataGrid";
import type { ExportSource } from "../grid/ExportDialog";
import { useHeldResult } from "../grid/useHeldResult";
import { CONNECTION_LOST, type ExecutionState } from "../session/execution";
import { isExporting, type SessionPhase } from "../session/SessionController";
import { SessionError } from "../session/SessionError";
import { useSession } from "../session/useSession";
import { QueryToolbar } from "./QueryToolbar";

/** ⌘↵ runs the selection (or the whole text); ⌘⇧↵ the whole text. Task 2 makes both statement-aware. */
type RunScope = "default" | "all";

/** Where the executed text starts in the editor: 0-based line and 0-based column in UTF-16 units. */
interface Origin {
  line: number;
  column: number;
}
const DOC_START: Origin = { line: 0, column: 0 };

function originAt(view: EditorView, offset: number): Origin {
  const line = view.state.doc.lineAt(offset);
  return { line: line.number - 1, column: offset - line.from };
}

interface ResultViewProps {
  execution: ExecutionState;
  origin: Origin;
  phase: SessionPhase;
  dialect: Dialect | null;
  exportSource: ExportSource | null;
  onLoadMore: () => void;
  onReconnect: () => void;
}

/** The latest run's result sets, row counts, messages and error (Task 2 replaces this with per-statement tabs). */
function ResultView({ execution, origin, phase, dialect, exportSource, onLoadMore, onReconnect }: ResultViewProps) {
  // Until the new run delivers a result, the grids keep the previous one (and their layout), dimmed.
  const results = useHeldResult(
    execution.resultSets.length ? execution.resultSets : null,
    execution.status !== "done",
  );
  const sets = results.value ?? [];
  const errorPosition = execution.error?.position;
  const lastSetIndex = execution.resultSets.at(-1)?.index;
  // A paused result keeps `hasMore` while its next page loads: the grid shows that, the status line does not.
  const running = execution.status === "running" && !execution.resultSets.some((s) => s.hasMore);
  const ended = execution.cancelled || execution.timedOut;

  return (
    <div className="flex h-full flex-col text-[12px]">
      {execution.error && (
        <div className="m-3 mb-0 shrink-0 rounded-md border border-danger/40 bg-danger/10 p-2 text-danger" data-selectable>
          <div className="whitespace-pre-wrap">{execution.error.message}</div>
          {errorPosition && (
            <div className="mt-1 text-[11px]">
              Line {errorPosition.line + origin.line}, column{" "}
              {errorPosition.column + (errorPosition.line === 1 ? origin.column : 0)}
              {execution.error.code ? ` · ${execution.error.code}` : ""}
            </div>
          )}
          {execution.error.code === CONNECTION_LOST && (
            <Button size="sm" className="mt-2" onClick={onReconnect}>
              Reconnect
            </Button>
          )}
        </div>
      )}
      {sets.map((set) => (
        // Keyed by position only: running the query again keeps the grid, so its widths and pins stay.
        <div key={set.index} className="min-h-[120px] flex-1">
          <DataGrid
            columns={set.columns}
            rows={set.rows}
            hasMore={set.hasMore}
            loadingMore={execution.status === "running" && set.index === lastSetIndex}
            onLoadMore={phase === "open" ? onLoadMore : undefined}
            stale={results.stale}
            dialect={dialect}
            exportSource={exportSource}
          />
        </div>
      ))}
      <div
        className={cn(
          "flex flex-col gap-1 overflow-auto px-3 py-2 empty:hidden",
          sets.length > 0 ? "max-h-[35%] shrink-0" : "min-h-0 flex-1",
        )}
      >
        {execution.confirmation && (
          <div className="text-warning">Not run: this statement needs confirmation.</div>
        )}
        {execution.affected.map((a) => (
          <div key={a.index}>{plural(a.rows, "row")} affected</div>
        ))}
        {execution.messages.map((m, i) => (
          <div key={i} className={m.severity === "warning" ? "text-warning" : "text-muted"} data-selectable>
            {m.text}
          </div>
        ))}
        {(running || execution.elapsedMs !== null || ended) && (
          <div className="flex items-center gap-2 text-muted">
            {running && <span>Running…</span>}
            {execution.elapsedMs !== null && <span>{execution.elapsedMs} ms</span>}
            {execution.cancelled && <span>Cancelled</span>}
            {execution.timedOut && <span>Timed out</span>}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * A query tab (spec §3.5): toolbar, then the editor above the results. Its session opens the first time the tab is
 * shown and closes with the tab; the editor lives as long as the tab, so undo history survives tab switches.
 */
export function QueryTab({ tab, active }: { tab: QueryTabState; active: boolean }) {
  const { settings } = useAppSettings();
  const pageSize = settings.run.page_size;
  const [shown, setShown] = useState(active);
  if (active && !shown) setShown(true);
  const { controller, snapshot, retry } = useSession(tab.profileId, tab.database, { enabled: shown });
  const dialect = useDialect(tab.profileId);
  const profiles = useProfiles();
  const editor = useRef<CodeEditorHandle>(null);
  const [requestId, setRequestId] = useState<string | null>(null);
  const [origin, setOrigin] = useState<Origin>(DOC_START);

  const profileName =
    tab.profileId === null
      ? null
      : (profiles.data?.find((entry) => entry.profile.id === tab.profileId)?.profile.name ?? "…");
  const phase: SessionPhase = snapshot?.phase ?? "opening";
  // Results belong to the session that produced them: a new session knows none of the old request ids.
  const execution = requestId ? (snapshot?.executions[requestId] ?? null) : null;
  // An export runs on this session: a statement sent now would only wait behind it.
  const exporting = isExporting(snapshot);
  const canRun = !!controller && phase === "open" && execution?.status !== "running" && !exporting;
  const runBlockedReason =
    tab.profileId === null
      ? "Choose a connection first"
      : exporting
        ? "An export is running on this session"
        : undefined;

  const run = (scope: RunScope) => {
    const view = editor.current?.view;
    if (!canRun || !controller || !view) return;
    const selection = view.state.selection.main;
    const partial = scope === "default" && !selection.empty;
    const from = partial ? selection.from : 0;
    const text = partial ? view.state.sliceDoc(selection.from, selection.to) : view.state.doc.toString();
    if (!text.trim()) return;
    setOrigin(originAt(view, from));
    if (requestId) controller.forget(requestId); // only the latest result is shown: do not keep the old rows
    setRequestId(controller.execute(text, { pageSize }));
  };
  const cancel = () => {
    if (execution) controller?.cancel(execution.requestId);
  };
  const loadMore = () => {
    if (execution) controller?.fetchMore(execution.requestId, pageSize);
  };

  // The editor's keymap is built once: it calls the latest `run` through a ref.
  const runRef = useRef(run);
  useLayoutEffect(() => {
    runRef.current = run;
  });
  // Handled even when nothing can run, so ⌘↵ never falls through to "insert blank line".
  const runFromKey = (scope: RunScope) => {
    runRef.current(scope);
    return true;
  };
  const [extensions] = useState<Extension[]>(() => [
    // CodeMirror keymaps skip key events during IME composition, so ⌘↵ never runs half-composed text.
    Prec.highest(
      keymap.of([
        { key: "Mod-Enter", run: () => runFromKey("default") },
        { key: "Shift-Mod-Enter", run: () => runFromKey("all") },
      ]),
    ),
    placeholder("SELECT …  (⌘↵ to run)"),
  ]);

  // The editor takes focus whenever the tab is shown. A frame later: a menu that opened the tab (New Query) hands
  // focus back to its trigger in a timeout once it closes, and would otherwise take it from the editor.
  useEffect(() => {
    if (!active) return;
    const frame = requestAnimationFrame(() => editor.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [active]);

  const executedSql = execution?.sql ?? null;
  const exportSource = useMemo(
    () => (controller && executedSql !== null ? { controller, sql: executedSql, title: tab.title } : null),
    [controller, executedSql, tab.title],
  );
  const initialSelection =
    tab.initialSelection ??
    (tab.initialCursor !== undefined ? { anchor: tab.initialCursor, head: tab.initialCursor } : undefined);

  return (
    <div className="flex h-full flex-col">
      <QueryToolbar
        profileName={profileName}
        phase={phase}
        session={snapshot?.info ?? null}
        sessionFailed={!!snapshot?.error}
        state={snapshot?.state ?? null}
        canRun={canRun}
        runBlockedReason={runBlockedReason}
        canCancel={phase === "open" && execution?.status === "running"}
        onRun={() => run("default")}
        onRunAll={() => run("all")}
        onCancel={cancel}
      />
      {tab.profileId !== null && (
        <SessionError profileId={tab.profileId} error={snapshot?.error ?? null} onRetry={retry} />
      )}
      <div className="min-h-0 flex-1">
        <Group orientation="vertical">
          <Panel id="editor" defaultSize="55%" minSize="15%">
            <CodeEditor
              ref={editor}
              initialText={tab.initialText}
              initialSelection={initialSelection}
              dialect={dialect}
              extensions={extensions}
            />
          </Panel>
          <PanelHandle orientation="vertical" />
          <Panel id="results" minSize="15%">
            {execution && (
              <ResultView
                execution={execution}
                origin={origin}
                phase={phase}
                dialect={dialect}
                exportSource={exportSource}
                onLoadMore={loadMore}
                onReconnect={retry}
              />
            )}
          </Panel>
        </Group>
      </div>
    </div>
  );
}
