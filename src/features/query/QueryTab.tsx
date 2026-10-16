import { Prec, type Extension } from "@codemirror/state";
import { keymap, placeholder, type EditorView } from "@codemirror/view";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Group, Panel } from "react-resizable-panels";
import { PanelHandle } from "@/components/ui/PanelHandle";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import { useAppSettings } from "@/stores/settings";
import type { QueryTabState } from "@/stores/workspace";
import { useDialect, useProfiles } from "../connections/hooks";
import { CodeEditor, type CodeEditorHandle } from "../editor/CodeEditor";
import { errorMarksExtension } from "../editor/diagnostics";
import { currentStatementHighlight, refreshStatements, statementsExtension } from "../editor/statements";
import type { ExecutionState } from "../session/execution";
import { busy, isExporting, type SessionPhase } from "../session/SessionController";
import { SessionError } from "../session/SessionError";
import { useSession } from "../session/useSession";
import { ConfirmRunDialog } from "./ConfirmRunDialog";
import { QueryToolbar } from "./QueryToolbar";
import { ResultsArea } from "./ResultsArea";
import { beginRunMarks, errorLocation, flashStatement, jumpToError, markStatementError } from "./runMarks";
import { planRun, type PlannedStatement, type RunMode } from "./runPlan";
import { useQueryRunner, type RunListener } from "./useQueryRunner";

const NO_EXECUTIONS: Record<string, ExecutionState> = {};

/**
 * A query tab (spec §3.5, §4.1): toolbar, then the editor above the results. Its session opens the first time the tab
 * is shown and closes with the tab; the editor lives as long as the tab, so undo history survives tab switches.
 * ⌘↵ runs the selection or (by `run.default`) the statement under the cursor; ⌘⇧↵ runs every statement.
 */
export function QueryTab({ tab, active }: { tab: QueryTabState; active: boolean }) {
  const { settings } = useAppSettings();
  const [shown, setShown] = useState(active);
  if (active && !shown) setShown(true);
  const { controller, snapshot, retry } = useSession(tab.profileId, tab.database, { enabled: shown });
  const dialect = useDialect(tab.profileId);
  const profiles = useProfiles();
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

  const profileName =
    tab.profileId === null
      ? null
      : (profiles.data?.find((entry) => entry.profile.id === tab.profileId)?.profile.name ?? "…");
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
  const canRun = !!controller && phase === "open" && dialect !== null && !busy(snapshot) && !running;
  const runBlockedReason =
    tab.profileId === null
      ? "Choose a connection first"
      : exporting
        ? "An export is running on this session"
        : undefined;

  const planning = useRef(false);
  const start = async (mode: RunMode) => {
    const current = view();
    if (!canRun || !current || dialect === null || planning.current) return;
    planning.current = true;
    try {
      let planned: PlannedStatement[];
      try {
        planned = await planRun(current, mode, settings.run.default, dialect);
      } catch (error) {
        toast("error", `Could not split the text into statements: ${errorMessage(error)}`);
        return;
      }
      if (planned.length) await runner.start(planned);
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

  return (
    <div className="flex h-full flex-col">
      <QueryToolbar
        profileName={profileName}
        phase={phase}
        session={snapshot?.info ?? null}
        sessionFailed={!!snapshot?.error}
        state={snapshot?.state ?? null}
        runDefault={settings.run.default}
        canRun={canRun}
        runBlockedReason={runBlockedReason}
        canCancel={phase === "open" && (running || loadingMore)}
        onRun={() => void start("default")}
        onRunAll={() => void start("all")}
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
            <ResultsArea
              run={run}
              executions={snapshot?.executions ?? NO_EXECUTIONS}
              controller={controller}
              phase={phase}
              dialect={dialect}
              pageSize={settings.run.page_size}
              title={tab.title}
              locate={locate}
              onJump={jump}
              onReconnect={retry}
            />
          </Panel>
        </Group>
      </div>
      <ConfirmRunDialog prompt={runner.prompt} onClosed={() => editor.current?.focus()} />
    </div>
  );
}
