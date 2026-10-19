import { RangeSetBuilder, type Extension } from "@codemirror/state";
import { Decoration, EditorView, gutter, GutterMarker } from "@codemirror/view";
import { Loader2 } from "lucide-react";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { useStore } from "zustand";
import type { StoreApi } from "zustand/vanilla";
import { Button } from "@/components/ui/Button";
import { CopyButton } from "@/components/ui/CopyButton";
import { Dialog } from "@/components/ui/Dialog";
import type { Dialect, EditPreview, Profile } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { useNow } from "@/lib/useNow";
import { profileColor } from "../connections/profileColors";
import { RecoverableError } from "../connections/RecoverableError";
import { isPasswordRequired } from "../connections/UnlockDialog";
import { CodeEditor, type CodeEditorHandle } from "../editor/CodeEditor";
import { STABLE_SCROLLBAR } from "../editor/extensions";
import type { SessionSnapshot } from "../session/SessionController";
import {
  applyingView,
  BUILDING,
  countParts,
  failedPlace,
  hasPreview,
  moreText,
  NON_TRANSACTIONAL_NOTE,
  PROBLEMS_INTRO,
  previewLists,
  rowLabel,
  statementLayout,
  TRANSACTIONAL_NOTE,
  type CommitDialogState,
  type FailedPlace,
  type StatementLayout,
} from "./commitDialog";
import { CLOSE_UNKNOWN_LABEL, type CommitAction } from "./commitFlow";

export type { CommitDialogState } from "./commitDialog";

/*
 * The commit dialog (spec ⑤ §3.6.5): the exact statements the backend will send, the transaction's frame included, with
 * the connection, the counts (deletes first) and what is not checked. Commit applies this preview's plan and no other
 * (05a M7). While applying it shows the progress and Cancel until every statement's count is in (then COMMIT is on its
 * way); then the outcome, the server's text, the failed statement marked, and what can be done next.
 *
 * It renders again only when its step changes (it is memoized, and its lists are built once per preview: pre-flight
 * H2); the progress and the applying controls follow the apply's events on their own.
 */

const LIST = "max-h-28 overflow-y-auto rounded-md border border-border [scrollbar-gutter:stable]";
const TONES = { success: "text-success", warning: "text-warning", danger: "text-danger" } as const;
const ACTION_LABELS: Record<CommitAction, string> = {
  show_in_grid: "Show in grid",
  preview_again: "Preview again",
  rollback: "Roll back",
  reconnect: "Reconnect",
  refresh: "Refresh",
};
/** How often the applying controls look again whether the outcome may never come (K11). */
const WAIT_TICK_MS = 1000;

/** U, I or D in the gutter of each statement's first line. */
class KindMarker extends GutterMarker {
  constructor(readonly letter: "U" | "I" | "D") {
    super();
  }
  override eq(other: GutterMarker): boolean {
    return other instanceof KindMarker && other.letter === this.letter;
  }
  override toDOM(): Node {
    const mark = document.createElement("span");
    mark.textContent = this.letter;
    mark.className = `cm-edit-kind-${this.letter}`;
    return mark;
  }
}
const LETTERS = { update: "U", insert: "I", delete: "D" } as const;
const MARKERS = { U: new KindMarker("U"), I: new KindMarker("I"), D: new KindMarker("D") };
const KIND_THEME = EditorView.theme({
  ".cm-edit-kinds .cm-gutterElement": { padding: "0 4px" },
  ".cm-edit-kind-U": { color: "var(--color-warning)", fontWeight: "600" },
  ".cm-edit-kind-I": { color: "var(--color-success)", fontWeight: "600" },
  ".cm-edit-kind-D": { color: "var(--color-danger)", fontWeight: "600" },
  ".cm-failed-statement": { backgroundColor: "color-mix(in oklab, var(--color-danger) 16%, transparent)" },
});
const FAILED_LINE = Decoration.line({ class: "cm-failed-statement" });

/** The gutter marks, the failed statement's lines (the text never changes: the set is made once), the scrollbar's room. */
function statementExtensions(preview: EditPreview, layout: StatementLayout, failed: FailedPlace | null): Extension[] {
  const kinds = new Map(layout.starts.map((start, i) => [start, MARKERS[LETTERS[preview.statements[i].kind]]]));
  const extensions: Extension[] = [
    gutter({
      class: "cm-edit-kinds",
      lineMarker: (_view, line) => kinds.get(line.from) ?? null,
      initialSpacer: () => MARKERS.U,
    }),
    KIND_THEME,
    STABLE_SCROLLBAR, // the user's Mac shows classic scrollbars: their room is kept (H23)
  ];
  if (failed) {
    const builder = new RangeSetBuilder<Decoration>();
    for (const start of failed.lineStarts) builder.add(start, start, FAILED_LINE);
    extensions.push(EditorView.decorations.of(builder.finish()));
  }
  return extensions;
}

function Counts({ preview }: { preview: EditPreview }) {
  const parts = countParts(preview.counts);
  return (
    <span className="text-[12px]">
      {parts.map((part, i) => (
        <span key={part.text} className={cn(part.danger && "font-medium text-danger")}>
          {i > 0 && <span className="font-normal text-fg"> · </span>}
          {part.text}
        </span>
      ))}
    </span>
  );
}

function More({ more }: { more: number }) {
  const text = moreText(more);
  return text ? <li className="px-2 py-1 text-muted">{text}</li> : null;
}

/** The apply's progress line: it alone follows each statement's count (`affected`), not the dialog. */
function ApplyProgress({
  store,
  requestId,
  statements,
}: {
  store: StoreApi<SessionSnapshot>;
  requestId: string;
  statements: number;
}) {
  const counted = useStore(store, (s) => s.executions[requestId]?.affected.length ?? 0);
  const { progress } = applyingView({ phase: "open", counted, statements, quietMs: 0, sinceCancelMs: null });
  return (
    <p className="flex items-center gap-2" aria-live="polite">
      <Loader2 size={12} className="animate-spin" />
      {progress}
    </p>
  );
}

/**
 * The applying dialog's footer (pre-flight M1, M2, ruling K11): Cancel on the left while a statement can still be
 * cancelled, a disabled "Committing…" where Commit was (the second click of a double click never lands on Cancel), and
 * "Close — outcome unknown" once the outcome may never come.
 */
function ApplyingFooter({
  store,
  requestId,
  statements,
  red,
  onCancel,
  onCloseUnknown,
}: {
  store: StoreApi<SessionSnapshot>;
  requestId: string;
  statements: number;
  red: boolean;
  onCancel: () => void;
  onCloseUnknown: () => void;
}) {
  const execution = useStore(store, (s) => s.executions[requestId]);
  const phase = useStore(store, (s) => s.phase);
  const [cancelAt, setCancelAt] = useState<number | null>(null);
  const [lastEventAt, setLastEventAt] = useState(() => Date.now());
  // Each event folds into a new execution object: the time since the last one is how long the apply has been quiet.
  useEffect(() => setLastEventAt(Date.now()), [execution]);
  const now = useNow(WAIT_TICK_MS);
  const view = applyingView({
    phase,
    counted: execution?.affected.length ?? 0,
    statements,
    quietMs: Math.max(0, now - lastEventAt),
    sinceCancelMs: cancelAt === null ? null : Math.max(0, now - cancelAt),
  });
  return (
    <>
      <div className="mr-auto flex items-center gap-2">
        <Button
          variant="ghost"
          onClick={() => {
            setCancelAt(Date.now());
            onCancel();
          }}
          disabled={!view.canCancel || cancelAt !== null}
        >
          {cancelAt === null ? "Cancel" : "Cancelling…"}
        </Button>
        {view.closeUnknown && <Button onClick={onCloseUnknown}>{CLOSE_UNKNOWN_LABEL}</Button>}
      </div>
      <Button variant={red ? "danger" : "primary"} disabled>
        Committing…
      </Button>
    </>
  );
}

export interface PreviewDialogProps {
  state: CommitDialogState;
  /** The table as the title names it (schema.name). */
  table: string;
  profile: Profile | undefined;
  dialect: Dialect | null;
  /** The session the apply was sent on, while applying: its progress comes from there. */
  store: StoreApi<SessionSnapshot> | null;
  onClose: () => void;
  onCommit: () => void;
  onCancelApply: () => void;
  onCloseUnknown: () => void;
  onAction: (action: CommitAction) => void;
  onShowInGrid: (changeId: string, column: string | null) => void;
  onPreviewAgain: () => void;
  /** The dialog has closed: `preventDefault` places the focus (the grid) instead of the opener (L9). */
  onClosed: (event: Event) => void;
}

function PreviewDialogView(props: PreviewDialogProps) {
  const { state, table, profile, dialect, store } = props;
  const editor = useRef<CodeEditorHandle>(null);
  const shown = hasPreview(state) ? state : null;
  const preview = shown?.preview ?? null;
  const staging = shown?.staging ?? null;
  const unknownBefore = shown?.unknownBefore ?? false;
  // Built once per preview (H2): never again for a step change or the apply's progress.
  const lists = useMemo(
    () => (preview && staging ? previewLists(preview, staging, unknownBefore) : null),
    [preview, staging, unknownBefore],
  );
  const layout = useMemo(() => (preview ? statementLayout(preview) : null), [preview]);
  const report = state.step === "result" ? state.report : null;
  const failed = useMemo(
    () => (preview && layout && report ? failedPlace(preview, layout, report) : null),
    [preview, layout, report],
  );
  const failedRow = useMemo(
    () => (preview && staging && report?.failedChange ? rowLabel(preview, staging, report.failedChange) : null),
    [preview, staging, report],
  );
  const extensions = useMemo(
    () => (preview && layout ? statementExtensions(preview, layout, failed) : []),
    [preview, layout, failed],
  );
  // The failed statement is scrolled to, the cursor where the server put its error (L12).
  useEffect(() => {
    if (failed) editor.current?.selectRange(failed.cursor, failed.cursor);
  }, [failed]);

  const applying = state.step === "applying";
  const red = profile?.color === "red";
  const tint = profileColor(profile?.color);
  const statements = preview?.statements.length ?? 0;

  const footer = (() => {
    switch (state.step) {
      case "previewing":
        return (
          <Button variant="ghost" onClick={props.onClose}>
            Cancel
          </Button>
        );
      case "failed":
        return (
          <>
            <Button variant="ghost" onClick={props.onClose}>
              Close
            </Button>
            <Button onClick={props.onPreviewAgain}>Try again</Button>
          </>
        );
      case "preview":
        return state.preview.plan_id ? (
          <>
            <span className="mr-auto">
              <CopyButton text={state.preview.script} label="Copy SQL" />
            </span>
            <Button variant="ghost" onClick={props.onClose}>
              Cancel
            </Button>
            <Button variant={red ? "danger" : "primary"} onClick={props.onCommit} autoFocus>
              Commit
            </Button>
          </>
        ) : (
          <Button variant="ghost" onClick={props.onClose} autoFocus>
            Close
          </Button>
        );
      case "applying":
        return store ? (
          <ApplyingFooter
            store={store}
            requestId={state.requestId}
            statements={statements}
            red={red}
            onCancel={props.onCancelApply}
            onCloseUnknown={props.onCloseUnknown}
          />
        ) : null;
      case "result":
        return (
          <>
            {state.report.actions.map((action) => (
              <Button key={action} onClick={() => props.onAction(action)}>
                {ACTION_LABELS[action]}
              </Button>
            ))}
            <Button variant="ghost" onClick={props.onClose} autoFocus>
              Close
            </Button>
          </>
        );
    }
  })();

  return (
    <Dialog
      open
      onOpenChange={(open) => !open && props.onClose()}
      onCloseAutoFocus={props.onClosed}
      title={`Commit changes to ${table}`}
      width={760}
      footer={footer}
    >
      <div className="flex flex-col gap-2.5 text-[12px]">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          {profile && (
            <span className={cn("flex items-center gap-1.5 font-medium", red && "text-danger")} data-selectable>
              {tint && <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: tint }} />}
              {profile.name}
            </span>
          )}
          {preview && <Counts preview={preview} />}
        </div>
        {state.step === "previewing" && (
          <p className="flex items-center gap-2 text-muted">
            <Loader2 size={12} className="animate-spin" /> {BUILDING}
          </p>
        )}
        {state.step === "failed" &&
          (isPasswordRequired(state.error) && profile ? (
            <RecoverableError profileId={profile.id} error={state.error} onRetry={props.onPreviewAgain} />
          ) : (
            <p className="whitespace-pre-wrap text-danger" data-selectable>
              {state.message}
            </p>
          ))}
        {lists && lists.problems.shown.length > 0 && (
          <>
            <p className="text-danger">{PROBLEMS_INTRO}</p>
            <ul className={LIST}>
              {lists.problems.shown.map((problem, i) => (
                <li key={i} className="flex items-center gap-2 border-b border-border px-2 py-1 last:border-b-0">
                  <span className="min-w-0 flex-1" data-selectable>
                    <span className="font-medium">{problem.label}</span>
                    {problem.column && <span className="font-mono"> · {problem.column}</span>}: {problem.message}
                  </span>
                  <Button size="sm" onClick={() => props.onShowInGrid(problem.changeId, problem.column)}>
                    Show in grid
                  </Button>
                </li>
              ))}
              <More more={lists.problems.more} />
            </ul>
          </>
        )}
        {lists && lists.warnings.shown.length > 0 && state.step === "preview" && (
          <ul className={cn(LIST, "text-warning")}>
            {lists.warnings.shown.map((warning, i) => (
              <li key={i} className="px-2 py-1" data-selectable>
                {warning.label && <span className="font-medium">{warning.label}: </span>}
                {warning.message}
                {warning.reasons && <span className="text-muted"> ({warning.reasons})</span>}
              </li>
            ))}
            <More more={lists.warnings.more} />
          </ul>
        )}
        {shown && layout && statements > 0 && (
          <div className="h-64 overflow-hidden rounded-md border border-border">
            <CodeEditor
              key={shown.key}
              ref={editor}
              initialText={layout.text}
              dialect={dialect}
              readOnly
              extensions={extensions}
            />
          </div>
        )}
        {state.step === "preview" && state.preview.plan_id && lists && (
          <p className="text-muted">{lists.transactional ? TRANSACTIONAL_NOTE : NON_TRANSACTIONAL_NOTE}</p>
        )}
        {applying && store && <ApplyProgress store={store} requestId={state.requestId} statements={statements} />}
        {state.step === "result" && (
          <div className="flex flex-col gap-1" data-selectable>
            <p className={cn("text-[13px] font-semibold", TONES[state.report.tone])}>{state.report.title}</p>
            {failedRow && <p className="font-medium">{failedRow}</p>}
            {state.report.detail && <p className="whitespace-pre-wrap">{state.report.detail}</p>}
            {state.report.notes.map((note) => (
              <p key={note} className="text-muted">
                {note}
              </p>
            ))}
            {state.messages.length > 0 && (
              <ul className={cn(LIST, "font-mono")}>
                {state.messages.map((message, i) => (
                  <li key={i} className={cn("px-2 py-0.5", message.severity === "warning" && "text-warning")}>
                    {message.text}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </Dialog>
  );
}

/** Memoized: it renders again when its step changes, not on every event of the session (H2). */
export const PreviewDialog = memo(PreviewDialogView);
