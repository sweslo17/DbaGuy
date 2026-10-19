import { Prec, type Extension } from "@codemirror/state";
import { keymap, placeholder, type EditorView } from "@codemirror/view";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Group, Panel } from "react-resizable-panels";
import { PanelHandle } from "@/components/ui/PanelHandle";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import { isImeKeyEvent } from "@/lib/ime";
import { isCommandKey } from "@/lib/shortcuts";
import { markEdited, readEditorBuffer, registerEditor, rememberEditorBuffer } from "@/stores/editorBuffers";
import { useAppSettings } from "@/stores/settings";
import { useReportTabStatus } from "@/stores/tabStatus";
import { startSelection, useWorkspace, type QueryTabState } from "@/stores/workspace";
import { useEngineInfo, useProfiles } from "../connections/hooks";
import { profileColor } from "../connections/profileColors";
import { CodeEditor, type CodeEditorHandle } from "../editor/CodeEditor";
import { sqlCompletion, type CompletionTarget } from "../editor/completion";
import { describeOnDemand } from "../editor/describe";
import { errorMarksExtension } from "../editor/diagnostics";
import { formatSql } from "../editor/format";
import { currentStatementHighlight, refreshStatements, statementsExtension } from "../editor/statements";
import { ExportDialog } from "../grid/ExportDialog";
import { useLongQueryNotifications } from "../notifications/useLongQueryNotifications";
import type { ExecutionState } from "../session/execution";
import { busy, isExporting, type RequestOptions, type SessionPhase } from "../session/SessionController";
import { SessionError } from "../session/SessionError";
import { useSessionGuards } from "../session/SessionGuards";
import { useSession } from "../session/useSession";
import { useScriptBinding } from "../scripts/useScriptBinding";
import { CLOSE_GUARD_ORDER, useCloseGuard } from "../workspace/closeGuards";
import { ConfirmRunDialog } from "./ConfirmRunDialog";
import { NO_SLOTS, paramsWatcher, problemMessage, type ParamProblem } from "./params";
import { ParamsPanel } from "./ParamsPanel";
import { QueryToolbar } from "./QueryToolbar";
import { ResultsArea } from "./ResultsArea";
import { beginRunMarks, errorLocation, flashStatement, jumpToError, markStatementError } from "./runMarks";
import { effectiveContext, seenFieldsOf } from "./queryContext";
import { bindParams, planRun, type PlannedStatement, type RunMode } from "./runPlan";
import { useQueryTabActions } from "./tabActions";
import { useQueryRunner, type RunListener } from "./useQueryRunner";
import { useTransactionControls } from "./useTransactionControls";

const NO_EXECUTIONS: Record<string, ExecutionState> = {};
const NO_FIELDS: string[] = [];
const NO_DRAFTS: Record<string, string> = {};

/**
 * A query tab (spec §3.5, §4.1, §4.2): toolbar, then the editor above the results. Its session opens the first time
 * the tab is shown and closes with the tab, or reopens when the connection picker changes it; the editor lives as long
 * as the tab, so undo history survives tab switches. ⌘↵ runs the selection or (by `run.default`) the statement under
 * the cursor; ⌘⇧↵ runs every statement; ⌘⇧F formats. ⌘S saves the tab to its script file (⌘⇧S: Save As). Closing the
 * tab or switching its connection first stops a running export and ends an open transaction, asking each time;
 * closing also asks about unsaved script changes. The tab hosts the export dialog, so it outlives the result grids. A
 * long run that ends while DbaGuy is in the background notifies. For Couchbase the tab has a query context (picked in
 * the toolbar, or by a click on the tree, or the one a browse tab or History entry opened with), which every run, export
 * and completion request carries; without a choice it follows the profile's default. For an engine that takes
 * parameters (Couchbase) a bar at the bottom of the editor pane holds one field per parameter the statements use
 * (JSON literals, kept with the tab in the workspace): a run needs a valid value for every parameter of the statements
 * it sends, and each statement is sent with its own.
 */
export function QueryTab({ tab, active }: { tab: QueryTabState; active: boolean }) {
  const { settings } = useAppSettings();
  const [shown, setShown] = useState(active);
  if (active && !shown) setShown(true);
  const { controller, snapshot, retry } = useSession(tab.profileId, tab.database, { enabled: shown });
  const engine = useEngineInfo(tab.profileId);
  const dialect = engine?.dialect ?? null;
  /** Couchbase: where the tab's unqualified names resolve; undefined for engines without a query context. */
  const takesContext = !!engine?.query_context;
  const sessionDefault = snapshot?.info?.server.default_query_context ?? null;
  const context = takesContext ? effectiveContext(tab.queryContext, sessionDefault) : undefined;
  /** Whether the engine has transactions; undefined while the engines load. Couchbase has none (spec §3.7.5). */
  const transactions = engine?.transactions;
  const profiles = useProfiles();
  const updateTab = useWorkspace((s) => s.updateTab);
  // SQL++ parameters: the fields come from the editor's split (`paramsWatcher`), the values are the tab's drafts.
  const takesParams = !!engine?.parameters;
  const drafts = tab.params ?? NO_DRAFTS;
  const draftsRef = useRef(drafts);
  useLayoutEffect(() => {
    draftsRef.current = drafts;
  });
  const [slots, setSlots] = useState(NO_SLOTS);
  const [paramsOpen, setParamsOpen] = useState(true);
  /** The field a refused run asks the panel to focus; cleared once the panel has had it (the same one may repeat). */
  const [focusParam, setFocusParam] = useState<string | null>(null);
  useEffect(() => {
    if (focusParam !== null) setFocusParam(null);
  }, [focusParam]);
  const changeParam = useCallback(
    (name: string, text: string) => updateTab(tab.id, "query", { params: { ...draftsRef.current, [name]: text } }),
    [tab.id, updateTab],
  );
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
  // The tab's own statement timeout (ruling R-02c-20), in memory only (R-02c-1); null follows the setting, also when
  // it changes later.
  const [ownTimeout, setOwnTimeout] = useState<number | null>(null);
  const timeout = ownTimeout ?? settings.run.statement_timeout_s;
  const timeoutS = timeout > 0 ? timeout : null;
  const runner = useQueryRunner({
    controller,
    profileId: tab.profileId,
    settings,
    timeoutS,
    queryContext: context,
    listener,
  });
  const { run } = runner;
  // The field names of the latest run's results: SQL++ completion offers them (as "seen in results").
  const executions = snapshot?.executions ?? NO_EXECUTIONS;
  const seenFields = useMemo(
    () => (takesContext ? seenFieldsOf(run, executions) : NO_FIELDS),
    [takesContext, run, executions],
  );
  /** The text is being split into statements for a run (a long script that was not split yet takes a moment). */
  const [splitting, setSplitting] = useState(false);
  /** Cancel was pressed while splitting: nothing is run. */
  const planCancelled = useRef(false);

  const profile = profiles.data?.find((entry) => entry.profile.id === tab.profileId)?.profile ?? null;
  const profileName = tab.profileId === null ? null : (profile?.name ?? "…");
  useLongQueryNotifications(run, executions, profile?.name ?? null);
  /** The tab and its connection, as dialogs and toasts name it. */
  const subject = profileName ? `${tab.title} · ${profileName}` : tab.title;
  const phase: SessionPhase = snapshot?.phase ?? "opening";
  const exporting = isExporting(snapshot);
  const running = !!run?.active;
  // After the run, the last result may still be loading a page ("Load more"): Cancel stops that too.
  const lastRequestId = run?.steps.findLast((step) => step.requestId !== null)?.requestId ?? null;
  const loadingMore = !running && !!lastRequestId && snapshot?.executions[lastRequestId]?.status === "running";
  const cancel = () => {
    if (splitting) planCancelled.current = true;
    else if (running || runner.checking) runner.cancel();
    else if (lastRequestId) controller?.cancel(lastRequestId);
  };
  /** Splitting or checking the statements before a run sends any (Cancel stops it). */
  const checking = splitting || runner.checking;
  const sessionBusy = busy(snapshot);
  // Nothing runs before the session has reported its state and taken the tab's auto-commit mode: a statement sent
  // earlier would run in the wrong mode. An engine without transactions has no mode to wait for.
  const modeApplied = !!snapshot?.state && (transactions === false || snapshot.state.autocommit === tab.autocommit);
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
  /** What disables the connection and context pickers: only a run (a statement of it may still be sent). */
  const pickerBlockedReason = running ? "Wait for the run to finish, or cancel it" : undefined;

  const transaction = useTransactionControls({
    tabId: tab.id,
    controller,
    snapshot,
    autocommit: tab.autocommit,
    transactions,
    subject,
  });
  const inTransaction = !!snapshot?.state?.inTransaction;
  const canEnd = inTransaction && phase === "open" && !sessionBusy;
  const endBlockedReason = !inTransaction
    ? "No open transaction"
    : sessionBusy
      ? "Wait for the running statement or export"
      : undefined;
  const autocommitBlockedReason = inTransaction ? "Commit or roll back first" : runningReason;

  // Closing the tab: the unsaved script first (registered by the script binding below), then a running export, then
  // the run (nothing more is sent), then an open transaction.
  const guards = useSessionGuards(controller, subject);
  useCloseGuard(tab.id, CLOSE_GUARD_ORDER.export, (intent) => guards.exportStep(intent));
  useCloseGuard(tab.id, CLOSE_GUARD_ORDER.run, () => {
    runner.stop();
    return true;
  });
  useCloseGuard(tab.id, CLOSE_GUARD_ORDER.transaction, (intent) => guards.transactionStep(intent));
  useReportTabStatus(tab.id, { busy: running || sessionBusy });
  const script = useScriptBinding({ tab, shown, active, editor });

  const switching = useRef(false);
  const changeConnection = async (profileId: string, database: string | null) => {
    if (switching.current) return;
    switching.current = true;
    try {
      if (!(await guards.exportStep("switch")) || !(await guards.transactionStep("switch"))) return;
      // useSession closes the old session and opens the new one; the new profile's own default context applies.
      updateTab(tab.id, "query", { profileId, database, queryContext: undefined });
    } finally {
      switching.current = false;
    }
  };

  // The export dialog of the result whose grid asked; it stays mounted (with its settings) once opened.
  const [exportRequest, setExportRequest] = useState<{ sql: string; request: RequestOptions; open: boolean } | null>(
    null,
  );
  const openExport = useCallback(
    (sql: string, request: RequestOptions) => setExportRequest({ sql, request, open: true }),
    [],
  );

  /** A run that cannot start for want of a parameter's value: says which, and opens the panel on its field. */
  const refuseRun = (problem: ParamProblem) => {
    toast("error", problemMessage(problem));
    setParamsOpen(true);
    setFocusParam(problem.name);
  };

  const planning = useRef(false);
  const start = async (mode: RunMode) => {
    const current = view();
    if (!canRun || !current || dialect === null || planning.current) return;
    planning.current = true;
    planCancelled.current = false;
    try {
      const plannedDoc = current.state.doc; // planRun reads the text before its first await
      let planned: PlannedStatement[];
      setSplitting(true);
      try {
        planned = await planRun(current, mode, settings.run.default, dialect);
      } catch (error) {
        toast("error", `Could not split the text into statements: ${errorMessage(error)}`);
        return;
      } finally {
        setSplitting(false);
      }
      if (planCancelled.current || !planned.length) return;
      let statements = planned;
      if (takesParams) {
        // Before anything is analysed or sent: a statement needs a valid value for each parameter it uses.
        const bound = bindParams(planned, draftsRef.current);
        if ("problem" in bound) {
          refuseRun(bound);
          return;
        }
        statements = bound.statements;
      }
      await runner.start(statements, { unchanged: () => view()?.state.doc === plannedDoc });
    } finally {
      planning.current = false;
    }
  };

  // ⌘⇧F: the selection, else the statement under the cursor, by the tab's dialect and the editor settings.
  const format = () => withView((v) => void formatSql(v, { dialect, editor: settings.editor }));

  // The command palette calls the same handlers as the toolbar and the keymap, enabled when their controls are
  // (ruling R-02c-15).
  useQueryTabActions(tab.id, {
    run: { enabled: canRun, run: () => void start("default") },
    runAll: { enabled: canRun, run: () => void start("all") },
    format: { enabled: true, run: format },
    toggleAutocommit: {
      enabled: transactions !== false && !autocommitBlockedReason,
      checked: tab.autocommit,
      run: () => transaction.setAutocommit(!tab.autocommit),
    },
    commit: { enabled: transactions !== false && canEnd, run: transaction.commit },
    rollback: { enabled: transactions !== false && canEnd, run: transaction.rollback },
  });

  // ⌘↵ and ⌘⇧↵ run from a parameter's field as they do in the editor (which handles its own keys and prevents them).
  const runFromParams = (e: KeyboardEvent<HTMLElement>) => {
    if (isImeKeyEvent(e) || e.defaultPrevented || !(e.target instanceof HTMLInputElement)) return;
    const mode = isCommandKey(e, "enter") ? "default" : isCommandKey(e, "enter", { shift: true }) ? "all" : null;
    if (!mode) return;
    e.preventDefault();
    void start(mode);
  };

  // The editor's keymap is built once: it calls the latest `start`, `format` and script commands through refs.
  const startRef = useRef(start);
  const formatRef = useRef(format);
  const scriptRef = useRef(script);
  useLayoutEffect(() => {
    startRef.current = start;
    formatRef.current = format;
    scriptRef.current = script;
  });
  // What the editor's extensions read at each use (split, completion, describe), so they never need reconfiguring.
  const target: CompletionTarget = {
    profileId: tab.profileId,
    database: tab.database,
    dialect,
    keywordCase: settings.editor.keyword_case,
    queryContext: context ?? null,
    seenFields,
  };
  const editorTarget = useRef(target);
  useLayoutEffect(() => {
    editorTarget.current = target;
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
        { key: "Mod-s", run: () => (scriptRef.current.save(), true) },
        { key: "Shift-Mod-s", run: () => (scriptRef.current.saveAs(), true) },
        { key: "Shift-Mod-f", run: () => (formatRef.current(), true) },
      ]),
    ),
    placeholder("SELECT …  (⌘↵ to run)"),
    statementsExtension({ dialect: () => editorTarget.current.dialect }),
    currentStatementHighlight(),
    errorMarksExtension(),
    sqlCompletion(() => editorTarget.current),
    describeOnDemand(() => editorTarget.current),
    paramsWatcher(setSlots),
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

  // The workspace writer reads the editor only when it saves: a keystroke just marks the workspace edited, so typing
  // never re-renders the tab tree (R-02c-4). The History panel inserts through it. What the editor held when it is
  // destroyed (the tab closing, or its content crashing) reaches the writer through `onUnmount` (R-02c-14).
  useLayoutEffect(() => registerEditor(tab.id, () => editor.current), [tab.id]);
  // Mounted again ("Try again" after this tab crashed): the editor starts from what it held, not from the tab's start.
  const [remembered] = useState(() => readEditorBuffer(tab.id));

  // Stable (they only read the editor's ref), so the Messages rows are not rendered again on every event.
  const locate = useCallback((index: number, execution: ExecutionState) => {
    const current = editor.current?.view;
    return current ? errorLocation(current, index, execution) : null;
  }, []);
  const jump = useCallback((index: number, execution: ExecutionState) => {
    const current = editor.current?.view;
    if (current) jumpToError(current, index, execution);
  }, []);
  const initialSelection = remembered?.selection ?? startSelection(tab);
  const tint = profileColor(profile?.color);

  return (
    <div className="flex h-full flex-col">
      <QueryToolbar
        connection={{
          profileId: tab.profileId,
          database: tab.database,
          phase,
          session: snapshot?.info ?? null,
          sessionFailed: !!snapshot?.error,
          blockedReason: pickerBlockedReason,
          onChange: (profileId, database) => void changeConnection(profileId, database),
          context: {
            choice: tab.queryContext,
            sessionDefault,
            loading: !snapshot?.info,
            blockedReason: pickerBlockedReason,
            onChange: (next) => updateTab(tab.id, "query", { queryContext: next }),
          },
        }}
        state={snapshot?.state ?? null}
        visible={active}
        pulse={transaction.pulse}
        runDefault={settings.run.default}
        canRun={canRun}
        runBlockedReason={runBlockedReason}
        canCancel={phase === "open" && (running || loadingMore || checking)}
        checking={checking}
        timeout={{
          seconds: timeout,
          setting: settings.run.statement_timeout_s,
          followsSetting: ownTimeout === null,
          onChange: setOwnTimeout,
          onDone: () => editor.current?.focus(),
        }}
        onRun={() => void start("default")}
        onRunAll={() => void start("all")}
        onCancel={cancel}
        showTransactions={transactions !== false}
        autocommit={tab.autocommit}
        autocommitBlockedReason={autocommitBlockedReason}
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
            {/* The connection's colour as the editor's top border; the parameters' bar sits under the editor. */}
            <div
              className="flex h-full flex-col"
              style={tint ? { borderTop: `2px solid ${tint}` } : undefined}
              onKeyDown={runFromParams}
            >
              <div className="relative min-h-0 flex-1">
                {script.loading && (
                  <span
                    className="pointer-events-none absolute top-1 right-3 z-10 animate-delayed-show text-[11px] text-muted"
                  >
                    Loading…
                  </span>
                )}
                <div className="absolute inset-0">
                  <CodeEditor
                    ref={editor}
                    initialText={remembered?.text ?? tab.initialText}
                    initialSelection={initialSelection}
                    dialect={dialect}
                    readOnly={script.loading}
                    extensions={extensions}
                    onChange={() => {
                      markEdited();
                      script.docChanged();
                    }}
                    onSelectionChange={markEdited}
                    onUnmount={(content) => rememberEditorBuffer(tab.id, content)}
                  />
                </div>
              </div>
              {takesParams && slots.named.length + slots.positional > 0 && (
                <ParamsPanel
                  slots={slots}
                  drafts={drafts}
                  open={paramsOpen}
                  focus={focusParam}
                  onToggle={() => setParamsOpen((open) => !open)}
                  onChange={changeParam}
                />
              )}
            </div>
          </Panel>
          <PanelHandle orientation="vertical" />
          <Panel id="results" minSize="15%">
            <ResultsArea
              run={run}
              executions={executions}
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
          source={{ controller, sql: exportRequest.sql, request: exportRequest.request, title: tab.title }}
          open={exportRequest.open}
          onOpenChange={(open) => setExportRequest((current) => current && { ...current, open })}
        />
      )}
      {guards.dialog}
      {script.dialogs}
    </div>
  );
}
