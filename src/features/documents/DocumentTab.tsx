import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
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
import { useProfiles } from "../connections/hooks";
import { RecoverableError } from "../connections/RecoverableError";
import { CodeEditor, type CodeEditorHandle } from "../editor/CodeEditor";
import { keyspaceLabel } from "../explorer/fieldSamples";
import { formatJson, parseJson } from "../grid/json";
import { documentRef } from "./documentApi";
import { canEdit, panelOf, type DocumentLoad, type Panel } from "./documentState";
import { DocumentBar } from "./DocumentBar";
import { useDocument } from "./useDocument";

const READ_ONLY_NOTICE = "Read-only connection: documents open for reading";
const CHANGED_NOTICE = "The document changed on the server after these edits were made: saving will report a conflict";
const MISSING_WITH_EDITS_NOTICE =
  "The document no longer exists: Create it saves these edits as a new document";

/** One line under the bar: what the user should know before editing or saving. */
function Notice({ children, tone = "muted" }: { children: ReactNode; tone?: "muted" | "warning" }) {
  return (
    <p
      className={cn(
        "shrink-0 border-b border-border px-3 py-1.5 text-[12px]",
        tone === "warning" ? "text-warning" : "text-muted",
      )}
      data-selectable
    >
      {children}
    </p>
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
}: {
  panel: Panel;
  load: DocumentLoad;
  profileId: string;
  id: string;
  keyspace: Keyspace;
  onRetry: () => void;
  onReload: () => void;
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

/**
 * A Couchbase document (spec §3.13): its stored text, exactly as the server holds it, in a JSON editor under a bar with
 * its ID, collection and META. It is mounted for every document tab, shown or not, as a query tab is: only the first
 * read waits for the first time it is shown (ruling C28). Its editor is mounted in every state: loading, an error, a
 * missing or a binary document show their panel in its place by hiding the editor, never unmounting it, because the
 * loaded text is put in through the editor and a remount would drop the undo history. A restored tab with unsaved text
 * also has its editor, its dirty dot and (Task 7) its close guard before it is ever shown. A missing document whose
 * tab holds unsaved text keeps the editor in view (ruling C30). Format (⇧⌘F) pretty-prints a valid text; Reload reads
 * the document again (asking first when the tab has unsaved changes); Copy puts the text on the clipboard. The state
 * changes are `documentState`'s; `useDocument` runs them.
 */
export function DocumentTab({ tab, active }: { tab: DocumentTabState; active: boolean }) {
  const [shown, setShown] = useState(active);
  if (active && !shown) setShown(true);
  const editor = useRef<CodeEditorHandle>(null);
  const doc = useDocument(tab, shown, editor);
  const { load } = doc;
  const profiles = useProfiles();
  const readOnlyProfile =
    profiles.data?.find((entry) => entry.profile.id === tab.profileId)?.profile.read_only === true;
  const [confirmingReload, setConfirmingReload] = useState(false);
  const ref = documentRef(tab);

  // The workspace writer reads the editor when it saves; what it held when destroyed reaches it through `onUnmount`.
  useLayoutEffect(() => registerEditor(tab.id, () => editor.current), [tab.id]);
  // Mounted again ("Try again" after this tab crashed): the editor starts from what it held, not from the tab's start.
  const [remembered] = useState(() => readEditorBuffer(tab.id));
  useReportTabStatus(tab.id, { busy: load.status === "loading", dirty: tab.dirty });

  const panel = panelOf(load, tab.dirty);
  const editable = canEdit(load, readOnlyProfile);

  // The editor takes the focus whenever the tab is shown, and again once it comes back after a panel (a frame later:
  // a menu that opened the tab hands the focus back to its trigger once it closes).
  const editorShown = panel === null;
  useEffect(() => {
    if (!active || !editorShown) return;
    const frame = requestAnimationFrame(() => editor.current?.focus());
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

  const copy = async () => {
    const handle = editor.current;
    if (!handle) return;
    try {
      await writeClipboard(handle.getText());
      toast("success", "Copied the document");
    } catch (error) {
      console.warn("Could not copy the document", error);
      toast("error", `Could not copy the document: ${errorMessage(error)}`);
    }
  };

  /** Reload asks first when there is something to lose. */
  const reload = () => (tab.dirty ? setConfirmingReload(true) : doc.reload());

  // ⇧⌘F while this tab is shown, wherever the focus is in it (the editor has no binding of its own for it).
  const formatRef = useRef(format);
  useLayoutEffect(() => {
    formatRef.current = format;
  });
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (isImeKeyEvent(e) || e.defaultPrevented || !isCommandKey(e, "f", { shift: true }) || isDialogOpen()) return;
      e.preventDefault();
      formatRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active]);

  const missingWithEdits = load.status === "missing" && tab.dirty;

  return (
    <div className="flex h-full flex-col">
      <DocumentBar id={ref.id} keyspace={ref.keyspace} load={load} isNew={tab.isNew}>
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
      </DocumentBar>
      {readOnlyProfile && <Notice>{READ_ONLY_NOTICE}</Notice>}
      {missingWithEdits && <Notice tone="warning">{MISSING_WITH_EDITS_NOTICE}</Notice>}
      {doc.changedOnServer && <Notice tone="warning">{CHANGED_NOTICE}</Notice>}
      <div className="relative min-h-0 flex-1">
        <div className={cn("absolute inset-0", !editorShown && "hidden")}>
          <CodeEditor
            ref={editor}
            initialText={remembered?.text ?? tab.initialText ?? ""}
            initialSelection={remembered?.selection}
            dialect={null}
            language="json"
            readOnly={!editable}
            onChange={doc.edited}
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
            />
          </div>
        )}
      </div>
      {confirmingReload && (
        <ConfirmDialog
          open
          title="Reload document"
          message="Discard your changes and read the document again?"
          confirmLabel="Reload"
          onOpenChange={(open) => !open && setConfirmingReload(false)}
          onConfirm={() => {
            setConfirmingReload(false);
            doc.reload();
          }}
        />
      )}
    </div>
  );
}
