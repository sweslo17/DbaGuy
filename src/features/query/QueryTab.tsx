import { Prec, type Extension } from "@codemirror/state";
import { keymap, placeholder, type EditorView } from "@codemirror/view";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Group, Panel } from "react-resizable-panels";
import { PanelHandle } from "@/components/ui/PanelHandle";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import { useAppSettings } from "@/stores/settings";
import { useReportTabStatus } from "@/stores/tabStatus";
import { useWorkspace, type QueryTabState } from "@/stores/workspace";
import { useDialect, useProfiles } from "../connections/hooks";
import { profileColor } from "../connections/profileColors";
import { CodeEditor, type CodeEditorHandle } from "../editor/CodeEditor";
import { errorMarksExtension } from "../editor/diagnostics";
import { currentStatementHighlight, refreshStatements, statementsExtension } from "../editor/statements";
import { ExportDialog } from "../grid/ExportDialog";
import type { ExecutionState } from "../session/execution";
import { busy, isExporting, type SessionPhase } from "../session/SessionController";
import { SessionError } from "../session/SessionError";
import { useSessionGuards } from "../session/SessionGuards";
import { useSession } from "../session/useSession";
import { CLOSE_GUARD_ORDER, useCloseGuard } from "../workspace/closeGuards";
import { ConfirmRunDialog } from "./ConfirmRunDialog";
import { QueryToolbar } from "./QueryToolbar";
import { ResultsArea } from "./ResultsArea";
import { beginRunMarks, errorLocation, flashStatement, jumpToError, markStatementError } from "./runMarks";
import { planRun, type PlannedStatement, type RunMode } from "./runPlan";
import { useQueryRunner, type RunListener } from "./useQueryRunner";
import { useTransactionControls } from "./useTransactionControls";

const NO_EXECUTIONS: Record<string, ExecutionState> = {};

/**
 * A query tab (spec §3.5, §4.1, §4.2): toolbar, then the editor above the results. Its session opens the first time
 * the tab is shown and closes with the tab, or reopens when the connection picker changes it; the editor lives as long
 * as the tab, so undo history survives tab switches. ⌘↵ runs the selection or (by `run.default`) the statement under
 * the cursor; ⌘⇧↵ runs every statement. Closing the tab or switching its connection first stops a running export and
 * ends an open transaction, asking each time; the tab hosts the export dialog, so it outlives the result grids.
 */
export function QueryTab({ tab, active }: { tab: QueryTabState; active: boolean }) {
  const { settings } = useAppSettings();
  const [shown, setShown] = useState(active);
  if (active && !shown) setShown(true);
  const { controller, snapshot, retry } = useSession(tab.profileId, tab.database, { enabled: shown });
  const dialect = useDialect(tab.profileId);
  const profiles = useProfiles();
  const updateTab = useWorkspace((s) => s.updateTab);
  const editor = useRef<CodeEditorHandle>(null);
  const view = () => editor.current?.view ?? null;
  const withView = (fn: (v: EditorView) => void) => {
    const current = view();
    if (current) fn(current);
  };

  // What a run shows in the editor; built once, it reaches the editor through its ref.
  const [listener] = useState<RunListener>(() => ({
    onStart: (planned) => withView((v) => beginRunMarks(v, planned)),
    onStatement: (index) => withView((v) => flashStatement(v, index)),
    onSettled: (index, execution) => withView((v) => markStatementError(v, index, execution)),
  }));
  const timeoutS = settings.run.statement_timeout_s > 0 ? settings.run.statement_timeout_s : null;
  const runner = useQueryRunner({ controller, profileId: tab.profileId, settings, timeoutS, listener });
  const { run } = runner;

  const profile = profiles.data?.find((entry) => entry.profile.id === tab.profileId)?.profile ?? null;
  const profileName = tab.profileId === null ? null : (profile?.name ?? "…");
  /** The tab and its connection, as dialogs and toasts name it. */
  const subject = profileName ? `${tab.title} · ${profileName}` : tab.title;
  const phase: SessionPhase = snapshot?.phase ?? "opening";
  const exporting = isExporting(snapshot);
  const running = !!run?.active;
  // After the run, the last result may still be loading a page ("Load more"): Cancel stops that too.
  const lastRequestId = run?.steps.findLast((step) => step.requestId !== null)?.requestId ?? null;
  const loadingMore = !running && !!lastRequestId && snapshot?.executions[lastRequestId]?.status === "running";
  const cancel = () => {
    if (running) runner.cancel();
    else if (lastRequestId) controller?.cancel(lastRequestId);
  };
  const sessionBusy = busy(snapshot);
  // Nothing runs before the session has reported its state and taken the tab's auto-commit mode: a statement sent
  // earlier would run in the wrong mode.
  const modeApplied = !!snapshot?.state && snapshot.state.autocommit === tab.autocommit;
  const canRun = !!controller && phase === "open" && modeApplied && dialect !== null && !sessionBusy && !running;
  const runBlockedReason =
    tab.profileId === null
      ? "Choose a connection first"
      : exporting
        ? "An export is running on this session"
        : phase === "open" && !modeApplied
          ? "Setting the auto-commit mode…"
          : undefined;
  const runningReason = running || sessionBusy ? "Wait for the run to finish, or cancel it" : undefined;

  const transaction = useTransactionControls({
    tabId: tab.id,
    controller,
    snapshot,
    autocommit: tab.autocommit,
    subject,
  });
  const inTransaction = !!snapshot?.state?.inTransaction;
  const canEnd = inTransaction && phase === "open" && !sessionBusy;
  const endBlockedReason = !inTransaction
    ? "No open transaction"
    : sessionBusy
      ? "Wait for the running statement or export"
      : undefined;

  // Closing the tab: a running export, then the run (nothing more is sent), then an open transaction (Task 6 adds the
  // unsaved script).
  const guards = useSessionGuards(controller, subject);
  useCloseGuard(tab.id, CLOSE_GUARD_ORDER.export, (intent) => guards.exportStep(intent));
  useCloseGuard(tab.id, CLOSE_GUARD_ORDER.run, () => {
    runner.stop();
    return true;
  });
  useCloseGuard(tab.id, CLOSE_GUARD_ORDER.transaction, (intent) => guards.transactionStep(intent));
  useReportTabStatus(tab.id, { busy: running || sessionBusy });

  const switching = useRef(false);
  const changeConnection = async (profileId: string, database: string | null) => {
    if (switching.current) return;
    switching.current = true;
    try {
      if (!(await guards.exportStep("switch")) || !(await guards.transactionStep("switch"))) return;
      updateTab(tab.id, { profileId, database }); // useSession closes the old session and opens the new one
    } finally {
      switching.current = false;
    }
  };

  // The export dialog of the result whose grid asked; it stays mounted (with its settings) once opened.
  const [exportRequest, setExportRequest] = useState<{ sql: string; open: boolean } | null>(null);
  const openExport = useCallback((sql: string) => setExportRequest({ sql, open: true }), []);

  const planning = useRef(false);
  const start = async (mode: RunMode) => {
    const current = view();
    if (!canRun || !current || dialect === null || planning.current) return;
    planning.current = true;
    try {
      const plannedDoc = current.state.doc; // planRun reads the text before its first await
      let planned: PlannedStatement[];
      try {
        planned = await planRun(current, mode, settings.run.default, dialect);
      } catch (error) {
        toast("error", `Could not split the text into statements: ${errorMessage(error)}`);
        return;
      }
      if (planned.length) await runner.start(planned, { unchanged: () => view()?.state.doc === plannedDoc });
    } finally {
      planning.current = false;
    }
  };

  // The editor's keymap is built once: it calls the latest `start` through a ref.
  const startRef = useRef(start);
  useLayoutEffect(() => {
    startRef.current = start;
  });
  const dialectRef = useRef(dialect);
  useLayoutEffect(() => {
    dialectRef.current = dialect;
  });
  // Handled even when nothing can run, so ⌘↵ never falls through to "insert blank line".
  const runFromKey = (mode: RunMode) => {
    void startRef.current(mode);
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
    statementsExtension({ dialect: () => dialectRef.current }),
    currentStatementHighlight(),
    errorMarksExtension(),
  ]);

  // The split depends on the dialect, which is known only once the profiles have loaded (or a connection is chosen).
  useEffect(() => {
    const current = view();
    if (current && dialect !== null) refreshStatements(current);
  }, [dialect]); // `view` only reads the editor ref

  // The editor takes focus whenever the tab is shown. A frame later: a menu that opened the tab (New Query) hands
  // focus back to its trigger in a timeout once it closes, and would otherwise take it from the editor.
  useEffect(() => {
    if (!active) return;
    const frame = requestAnimationFrame(() => editor.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [active]);

  const locate = (index: number, execution: ExecutionState) => {
    const current = view();
    return current ? errorLocation(current, index, execution) : null;
  };
  const jump = (index: number, execution: ExecutionState) => withView((v) => jumpToError(v, index, execution));
  const initialSelection =
    tab.initialSelection ??
    (tab.initialCursor !== undefined ? { anchor: tab.initialCursor, head: tab.initialCursor } : undefined);
  const tint = profileColor(profile?.color);

  return (
    <div className="flex h-full flex-col">
      <QueryToolbar
        connection={{
          profileId: tab.profileId,
          database: tab.database,
          dialect,
          phase,
          session: snapshot?.info ?? null,
          sessionFailed: !!snapshot?.error,
          blockedReason: running ? "Wait for the run to finish, or cancel it" : undefined,
          onChange: (profileId, database) => void changeConnection(profileId, database),
        }}
        state={snapshot?.state ?? null}
        visible={active}
        pulse={transaction.pulse}
        runDefault={settings.run.default}
        canRun={canRun}
        runBlockedReason={runBlockedReason}
        canCancel={phase === "open" && (running || loadingMore)}
        onRun={() => void start("default")}
        onRunAll={() => void start("all")}
        onCancel={cancel}
        autocommit={tab.autocommit}
        autocommitBlockedReason={inTransaction ? "Commit or roll back first" : runningReason}
        onAutocommitChange={transaction.setAutocommit}
        canEnd={canEnd}
        endBlockedReason={endBlockedReason}
        onCommit={transaction.commit}
        onRollback={transaction.rollback}
      />
      {tab.profileId !== null && (
        <SessionError profileId={tab.profileId} error={snapshot?.error ?? null} onRetry={retry} />
      )}
      <div className="min-h-0 flex-1">
        <Group orientation="vertical">
          <Panel id="editor" defaultSize="55%" minSize="15%">
            {/* The connection's colour as the editor's top border. */}
            <div className="h-full" style={tint ? { borderTop: `2px solid ${tint}` } : undefined}>
              <CodeEditor
                ref={editor}
                initialText={tab.initialText}
                initialSelection={initialSelection}
                dialect={dialect}
                extensions={extensions}
              />
            </div>
          </Panel>
          <PanelHandle orientation="vertical" />
          <Panel id="results" minSize="15%">
            <ResultsArea
              run={run}
              executions={snapshot?.executions ?? NO_EXECUTIONS}
              controller={controller}
              phase={phase}
              dialect={dialect}
              pageSize={settings.run.page_size}
              onExport={openExport}
              locate={locate}
              onJump={jump}
              onReconnect={retry}
            />
          </Panel>
        </Group>
      </div>
      <ConfirmRunDialog prompt={runner.prompt} context={subject} onClosed={() => editor.current?.focus()} />
      {controller && exportRequest && (
        <ExportDialog
          source={{ controller, sql: exportRequest.sql, title: tab.title }}
          open={exportRequest.open}
          onOpenChange={(open) => setExportRequest((current) => current && { ...current, open })}
        />
      )}
      {guards.dialog}
    </div>
  );
}
