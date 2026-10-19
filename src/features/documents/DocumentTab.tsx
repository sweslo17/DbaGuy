import { Prec, type Extension } from "@codemirror/state";
import { keymap } from "@codemirror/view";
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import type { Keyspace } from "@/lib/api/types";
import { plural } from "@/lib/format";
import { cn } from "@/lib/cn";
import { isDialogOpen } from "@/lib/dialogs";
import { isImeKeyEvent } from "@/lib/ime";
import { writeClipboard } from "@/lib/native";
import { isCommandKey } from "@/lib/shortcuts";
import { readEditorBuffer, registerEditor, rememberEditorBuffer } from "@/stores/editorBuffers";
import { useReportTabStatus } from "@/stores/tabStatus";
import type { DocumentTabState } from "@/stores/workspace";
import { useReadOnlyProfile } from "../connections/hooks";
import { RecoverableError } from "../connections/RecoverableError";
import { CodeEditor, type CodeEditorHandle } from "../editor/CodeEditor";
import { keyspaceLabel } from "../explorer/fieldSamples";
import { formatJson, parseJson } from "../grid/json";
import { CLOSE_GUARD_ORDER, useCloseGuard } from "../workspace/closeGuards";
import { ConflictDialog } from "./ConflictDialog";
import { documentRef, problemMessage } from "./documentApi";
import { canEdit, panelOf, type DocumentLoad, type Panel } from "./documentState";
import { DocumentBar } from "./DocumentBar";
import { SavePreviewDialog } from "./SavePreviewDialog";
import { factsOf, useDocument } from "./useDocument";
import { useDocumentSave } from "./useDocumentSave";

const READ_ONLY_NOTICE = "Read-only connection: documents open for reading";
const CHANGED_NOTICE = "The document changed on the server after these edits were made: saving will report a conflict";
const MISSING_WITH_EDITS_NOTICE =
  "The document no longer exists: Create it saves these edits as a new document";

const NOTICE_TONES = { muted: "text-muted", warning: "text-warning", danger: "text-danger" } as const;

/**
 * One line under the bar: what the user should know before editing or saving, or why a write did not go through, with an
 * `action` at its right when there is one. A long text (the paths of a blocked save) scrolls inside its own area, which
 * reserves its scrollbar (the system's classic scrollbars take room).
 */
function Notice({
  children,
  tone = "muted",
  action,
}: {
  children: ReactNode;
  tone?: keyof typeof NOTICE_TONES;
  action?: ReactNode;
}) {
  return (
    <div className={cn("flex shrink-0 items-center gap-3 border-b border-border px-3 py-1.5 text-[12px]", NOTICE_TONES[tone])}>
      <p
        className="max-h-24 min-w-0 flex-1 overflow-y-auto break-words whitespace-pre-wrap [scrollbar-gutter:stable]"
        data-selectable
      >
        {children}
      </p>
      {action}
    </div>
  );
}

/** What shows in place of the editor while it cannot be used (the editor stays mounted behind it, hidden). */
function PanelView({
  panel,
  load,
  profileId,
  id,
  keyspace,
  onRetry,
  onReload,
  onCreate,
}: {
  panel: Panel;
  load: DocumentLoad;
  profileId: string;
  id: string;
  keyspace: Keyspace;
  onRetry: () => void;
  onReload: () => void;
  /** "Create it": null on a read-only connection. */
  onCreate: (() => void) | null;
}) {
  switch (panel) {
    case "loading":
      return <p className="animate-delayed-show p-4 text-[12px] text-muted">Loading…</p>;
    case "error":
      return <RecoverableError profileId={profileId} error={load.status === "error" ? load.error : null} onRetry={onRetry} />;
    case "missing":
      return (
        <div className="flex items-center gap-3 p-4 text-[13px]" data-selectable>
          <span>
            No document <span className="font-mono">{id}</span> in {keyspaceLabel(keyspace)}
          </span>
          <Button size="sm" onClick={onReload}>
            Reload
          </Button>
          {onCreate && (
            <Button size="sm" onClick={onCreate}>
              Create it
            </Button>
          )}
        </div>
      );
    case "binary": {
      const size = load.status === "ready" ? load.meta.size : 0;
      return (
        <p className="p-4 text-[13px] text-muted" data-selectable>
          Binary document ({plural(size, "byte")}) — view only
        </p>
      );
    }
  }
}

/** Puts the focus in the editor when there is one (it throws once its view is gone). */
function focusEditor(editor: RefObject<CodeEditorHandle | null>): void {
  try {
    editor.current?.focus();
  } catch {
    // The tab is going away.
  }
}

/**
 * A Couchbase document (spec §3.13): its stored text, exactly as the server holds it, in a JSON editor under a bar with
 * its ID, collection and META. It is mounted for every document tab, shown or not, as a query tab is: only the first
 * read waits for the first time it is shown (ruling C28). Its editor is mounted in every state: loading, an error, a
 * missing or a binary document show their panel in its place by hiding the editor, never unmounting it, because the
 * loaded text is put in through the editor and a remount would drop the undo history. A restored tab with unsaved text
 * also has its editor, its dirty dot and its close guard before it is ever shown (ruling C28). A missing document whose
 * tab holds unsaved text keeps the editor in view, read-only, until it is created or saved as new (rulings C30, C47).
 *
 * Save (⌘S) and Delete… send the writes (`useDocumentSave`: the preview, conflict and unlock dialogs, the problem line);
 * Format (⇧⌘F) pretty-prints a valid text; Reload reads the document again (asking first when the tab has unsaved
 * changes); Copy puts the text on the clipboard. Closing a tab with unsaved text asks first (ruling C20). The state
 * changes are `documentState`'s; `useDocument` runs them.
 */
export function DocumentTab({ tab, active }: { tab: DocumentTabState; active: boolean }) {
  const [shown, setShown] = useState(active);
  if (active && !shown) setShown(true);
  const editor = useRef<CodeEditorHandle>(null);
  const doc = useDocument(tab, shown, editor);
  const saving = useDocumentSave(tab, editor, doc);
  const { load } = doc;
  const readOnlyProfile = useReadOnlyProfile(tab.profileId);
  const [confirmingReload, setConfirmingReload] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [confirmingExisting, setConfirmingExisting] = useState(false);
  const ref = documentRef(tab);

  // The workspace writer reads the editor when it saves; what it held when destroyed reaches it through `onUnmount`.
  useLayoutEffect(() => registerEditor(tab.id, () => editor.current), [tab.id]);
  // Mounted again ("Try again" after this tab crashed): the editor starts from what it held, not from the tab's start.
  const [remembered] = useState(() => readEditorBuffer(tab.id));
  useReportTabStatus(tab.id, { busy: load.status === "loading" || saving.busy, dirty: tab.dirty });

  const panel = panelOf(load, tab.dirty);
  // Never while a write is in flight or waits for the user (ruling C14): its answer replaces the baseline.
  const editable = canEdit(load, readOnlyProfile, doc.saving);
  const binary = load.status === "ready" && load.meta.type === "base64";
  // Save and Delete… exist for documents that can be written (never a binary one, never on a read-only connection).
  const writable = !readOnlyProfile && !binary;

  // The editor takes the focus whenever the tab is shown, and again once it comes back after a panel (a frame later:
  // a menu that opened the tab hands the focus back to its trigger once it closes).
  const editorShown = panel === null;
  useEffect(() => {
    if (!active || !editorShown) return;
    const frame = requestAnimationFrame(() => focusEditor(editor));
    return () => cancelAnimationFrame(frame);
  }, [active, editorShown]);

  /** Format: the text pretty-printed, numbers and key order as they are, as one undoable change. */
  const format = () => {
    const handle = editor.current;
    if (!handle || !editorShown || !editable) return;
    const parsed = parseJson(handle.getText());
    if (!parsed) {
      toast("warning", "Not valid JSON: nothing to format");
      return;
    }
    handle.replaceAll(formatJson(parsed.root));
  };

  const copy = async (message = "Copied the document") => {
    const handle = editor.current;
    if (!handle) return;
    try {
      await writeClipboard(handle.getText());
      toast("success", message);
    } catch (error) {
      console.warn("Could not copy the document", error);
      toast("error", `Could not copy the document: ${errorMessage(error)}`);
    }
  };

  /** Reads the document again, replacing the text: of a new document, that is opening the one that has its ID. */
  const readAgain = () => {
    saving.clearProblem();
    if (tab.isNew) doc.openExisting();
    else doc.reload();
  };
  /** Reload asks first when there is something to lose. */
  const reload = () => (tab.dirty ? setConfirmingReload(true) : readAgain());

  /** "Create it": the missing document's tab becomes a new document (its unsaved text stays, ruling C30). */
  const create = () => {
    doc.created();
    requestAnimationFrame(() => focusEditor(editor));
  };

  // Closing a tab with unsaved text asks first (ruling C20): Discard or Cancel, never Save (a save can need a preview or
  // a conflict decision, which a close prompt cannot host). Decided from the store, not from the last render.
  const discardAnswer = useRef<((discard: boolean) => void) | null>(null);
  const [askingDiscard, setAskingDiscard] = useState(false);
  const answerDiscard = (discard: boolean) => {
    const answer = discardAnswer.current;
    discardAnswer.current = null;
    setAskingDiscard(false);
    answer?.(discard);
  };
  useCloseGuard(tab.id, CLOSE_GUARD_ORDER.document, () => {
    if (!factsOf(tab.id)?.dirty) return true;
    discardAnswer.current?.(false);
    return new Promise<boolean>((resolve) => {
      discardAnswer.current = resolve;
      setAskingDiscard(true);
    });
  });
  // A tab that goes while it is asking keeps its text: the answer is no.
  useEffect(
    () => () => {
      discardAnswer.current?.(false);
      discardAnswer.current = null;
    },
    [],
  );

  // ⌘S and ⇧⌘F while this tab is shown, wherever the focus is in it (the editor's own keymap takes ⌘S inside it, and
  // has no binding for ⇧⌘F). ⌘S is never the browser's "Save page"; nothing happens while a dialog has the keyboard.
  const commands = useRef({ format, save: saving.save });
  useLayoutEffect(() => {
    commands.current = { format, save: saving.save };
  });
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (isImeKeyEvent(e) || e.defaultPrevented) return;
      if (isCommandKey(e, "s")) {
        e.preventDefault();
        if (!isDialogOpen()) commands.current.save();
      } else if (isCommandKey(e, "f", { shift: true }) && !isDialogOpen()) {
        e.preventDefault();
        commands.current.format();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active]);
  // Inside the editor ⌘S is CodeMirror's: it skips key events during IME composition on its own.
  const [extensions] = useState<Extension[]>(() => [
    Prec.highest(keymap.of([{ key: "Mod-s", run: () => (commands.current.save(), true) }])),
  ]);

  const missingWithEdits = load.status === "missing" && tab.dirty;
  const problem = saving.problem;

  return (
    <div className="flex h-full flex-col">
      <DocumentBar id={ref.id} keyspace={ref.keyspace} load={load} isNew={tab.isNew}>
        {writable && (
          <Button
            size="sm"
            variant={tab.dirty || tab.isNew ? "primary" : "secondary"}
            title="Save (⌘S)"
            disabled={!editable}
            onClick={saving.save}
          >
            {saving.busy ? "Saving…" : "Save"}
          </Button>
        )}
        <Button size="sm" title="Format (⇧⌘F)" disabled={!editorShown || !editable} onClick={format}>
          Format
        </Button>
        <Button
          size="sm"
          title="Read the document again"
          disabled={tab.isNew || load.status === "loading" || doc.saving}
          onClick={reload}
        >
          Reload
        </Button>
        <Button size="sm" title="Copy the text" disabled={!editorShown} onClick={() => void copy()}>
          Copy
        </Button>
        {writable && !tab.isNew && (
          <Button
            size="sm"
            className="text-danger"
            title="Delete the document"
            disabled={load.status !== "ready" || doc.saving}
            onClick={() => setConfirmingDelete(true)}
          >
            Delete…
          </Button>
        )}
      </DocumentBar>
      {readOnlyProfile && <Notice>{READ_ONLY_NOTICE}</Notice>}
      {missingWithEdits && (
        <Notice
          tone="warning"
          action={
            !readOnlyProfile && (
              <Button size="sm" onClick={create}>
                Create it
              </Button>
            )
          }
        >
          {MISSING_WITH_EDITS_NOTICE}
        </Notice>
      )}
      {doc.changedOnServer && <Notice tone="warning">{CHANGED_NOTICE}</Notice>}
      {problem && (
        <Notice
          tone="danger"
          action={
            problem.kind === "exists" ? (
              <Button size="sm" onClick={() => setConfirmingExisting(true)}>
                Open the existing document
              </Button>
            ) : problem.kind === "outcome_unknown" ? (
              <Button size="sm" onClick={reload}>
                Reload
              </Button>
            ) : undefined
          }
        >
          {problemMessage(problem)}
        </Notice>
      )}
      {saving.passwordNeeded && (
        <RecoverableError
          profileId={tab.profileId}
          error={saving.passwordNeeded.error}
          onRetry={saving.passwordNeeded.resend}
        />
      )}
      <div className="relative min-h-0 flex-1">
        <div className={cn("absolute inset-0", !editorShown && "hidden")}>
          <CodeEditor
            ref={editor}
            initialText={remembered?.text ?? tab.initialText ?? ""}
            initialSelection={remembered?.selection}
            dialect={null}
            language="json"
            readOnly={!editable}
            extensions={extensions}
            onChange={() => {
              doc.edited();
              saving.edited();
            }}
            onUnmount={(content) => rememberEditorBuffer(tab.id, content)}
          />
        </div>
        {panel && (
          <div className="absolute inset-0 overflow-auto [scrollbar-gutter:stable]">
            <PanelView
              panel={panel}
              load={load}
              profileId={tab.profileId}
              id={ref.id}
              keyspace={ref.keyspace}
              onRetry={doc.retry}
              onReload={reload}
              onCreate={readOnlyProfile ? null : create}
            />
          </div>
        )}
      </div>
      {saving.preview && (
        <SavePreviewDialog
          preview={saving.preview}
          documentId={ref.id}
          pending={saving.busy}
          onConfirm={saving.confirm}
          onCancel={saving.cancelPreview}
        />
      )}
      {saving.conflict && (
        <ConflictDialog
          kind={saving.conflict}
          documentId={ref.id}
          onReload={() => {
            // The dialog is the confirmation: its button says the edits are discarded.
            saving.dismissConflict();
            readAgain();
          }}
          onCopy={() => void copy("Copied your version")}
          onSaveAsNew={saving.saveAsNew}
          onCancel={saving.dismissConflict}
        />
      )}
      {confirmingReload && (
        <ConfirmDialog
          open
          title="Reload document"
          message="Discard your changes and read the document again?"
          confirmLabel="Reload"
          onOpenChange={(open) => !open && setConfirmingReload(false)}
          onConfirm={() => {
            setConfirmingReload(false);
            readAgain();
          }}
        />
      )}
      {confirmingExisting && (
        <ConfirmDialog
          open
          title="Open the existing document"
          message="Discard this draft and open the existing document?"
          confirmLabel="Discard draft"
          onOpenChange={(open) => !open && setConfirmingExisting(false)}
          onConfirm={() => {
            setConfirmingExisting(false);
            readAgain();
          }}
        />
      )}
      {confirmingDelete && (
        <ConfirmDialog
          open
          title="Delete document"
          message={`Delete document ${ref.id}? This cannot be undone.`}
          confirmLabel="Delete"
          pending={saving.busy}
          // The write is on its way once confirmed: the dialog stays until it has an answer.
          onOpenChange={(open) => !open && !saving.busy && setConfirmingDelete(false)}
          onConfirm={() => void saving.remove().then(() => setConfirmingDelete(false))}
        />
      )}
      {askingDiscard && (
        <ConfirmDialog
          open
          title="Unsaved changes"
          message={`Discard unsaved changes to ${ref.id}?`}
          confirmLabel="Discard"
          onOpenChange={(open) => !open && answerDiscard(false)}
          onConfirm={() => answerDiscard(true)}
        />
      )}
    </div>
  );
}
