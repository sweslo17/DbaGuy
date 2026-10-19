import type { Text } from "@codemirror/state";
import { useRef, useState, type RefObject } from "react";
import { toast } from "@/components/ui/Toast";
import { ApiError, errorMessage } from "@/lib/api/client";
import type { SaveAnswer, SavePreview } from "@/lib/api/types";
import { useWorkspace, type DocumentTabState } from "@/stores/workspace";
import { useReadOnlyProfile } from "../connections/hooks";
import type { CodeEditorHandle } from "../editor/CodeEditor";
import {
  classifyWriteError,
  documentRef,
  insertDocument,
  isPreview,
  prettyJson,
  removeDocument,
  saveDocument,
  type DocumentRef,
  type SaveProblem,
  type WriteFailure,
} from "./documentApi";
import { canEdit } from "./documentState";
import { factsOf, type DocumentState } from "./useDocument";

export type { SaveProblem };

export interface DocumentSaving {
  /** A save, insert or delete is in flight (the editor is read-only, ruling C14; the tab shows it as busy). */
  busy: boolean;
  /** What the server would store instead of the text: the user confirms it (`confirm`) or not (`cancelPreview`). */
  preview: SavePreview | null;
  /** The conflict dialog to show: the document changed, or was deleted, after it was opened. */
  conflict: "changed" | "deleted" | null;
  /** Why the last write did not go through, as one line under the bar. */
  problem: SaveProblem | null;
  save: () => void; // ⌘S and the Save button
  confirm: () => void; // the preview's "Save as shown"
  cancelPreview: () => void;
  dismissConflict: () => void;
  /** A deleted document's edits saved as a new document with the same ID (the conflict dialog). */
  saveAsNew: () => void;
  /** Delete…, after its confirmation: true when the document is gone (the tab then closes). */
  remove: () => Promise<boolean>;
  /**
   * A save, insert or delete answered `password_required` (ruling C31): the tab shows `RecoverableError` with `error`,
   * and `resend` (its onRetry, also run once the unlock dialog has the password) sends the same request.
   */
  passwordNeeded: { error: Error; resend: () => void } | null;
  /** The editor's text changed (the tab calls it from the editor's onChange): a problem about the text goes away. */
  edited: () => void;
  /** Takes the problem line away (the user reloaded, or opened the existing document). */
  clearProblem: () => void;
}

/** A write as it was asked for: what goes out again, unchanged, once the unlock dialog has the password (ruling C31). */
type TextWrite =
  | { kind: "save"; cas: string; text: string; sent: Text; confirmed: boolean; shown?: string }
  | { kind: "insert"; text: string; sent: Text; confirmed: boolean; shown?: string };
type Write = TextWrite | { kind: "remove"; cas: string };

/** The problems that are about the text: they go away once it is edited (the others are about the document or the ID). */
const TEXT_PROBLEMS: ReadonlySet<SaveProblem["kind"]> = new Set(["invalid_json", "numbers", "error"]);

/** The editor's handle when it is mounted (the handle throws once its view is gone), else null. */
function mounted(editor: RefObject<CodeEditorHandle | null>): CodeEditorHandle | null {
  const handle = editor.current;
  if (!handle) return null;
  try {
    void handle.view;
    return handle;
  } catch {
    return null;
  }
}

/** Where `offset` (UTF-16, as the editor counts) is in the editor's text: line and column, from 1. */
function lineColumn(editor: RefObject<CodeEditorHandle | null>, offset: number): { line: number; column: number } {
  const doc = mounted(editor)?.view.state.doc;
  if (!doc) return { line: 1, column: 1 };
  const at = Math.min(Math.max(offset, 0), doc.length);
  const line = doc.lineAt(at);
  return { line: line.number, column: at - line.from + 1 };
}

/** Sends the write (never twice, never with a signal or a timeout: it may reach the server whatever happens here). */
function sendWrite(ref: DocumentRef, write: Write): Promise<SaveAnswer | undefined> {
  switch (write.kind) {
    case "save":
      return saveDocument(ref, write.cas, write.text, write.confirmed);
    case "insert":
      return insertDocument(ref, write.text, write.confirmed);
    case "remove":
      return removeDocument(ref, write.cas).then(() => undefined);
  }
}

/**
 * Saving, creating and deleting a document tab's document (spec §3.9.3, §3.13). It sends the writes and shows what came
 * back; what the tab then is — saved, new, missing, with or without its edits — is `documentState`'s, through `loaded`'s
 * commands. Only this hook sends a write, only on the user's action (⌘S, Save, Save as shown, Delete…), and never again
 * by itself: a write that lost contact with the backend is reported as unknown (ruling C45), and the one retry there is
 * (after the unlock dialog, ruling C31) is of a request that never left the backend.
 *
 * The editor is read-only from the moment a write starts until it is over, previews and a pending unlock included
 * (`loaded.saving`), so the text a confirmation or a resend carries is the text the user was shown.
 */
export function useDocumentSave(
  tab: DocumentTabState,
  editor: RefObject<CodeEditorHandle | null>,
  loaded: DocumentState,
): DocumentSaving {
  const ref = documentRef(tab);
  const readOnly = useReadOnlyProfile(tab.profileId);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<SavePreview | null>(null);
  const [conflict, setConflict] = useState<DocumentSaving["conflict"]>(null);
  const [problem, setProblem] = useState<SaveProblem | null>(null);
  const [passwordNeeded, setPasswordNeeded] = useState<DocumentSaving["passwordNeeded"]>(null);
  /** A write is in flight: set at once, where `busy` waits for the next render (⌘S twice sends once). */
  const inFlight = useRef(false);
  /** The write whose preview is on screen, with that preview. */
  const previewed = useRef<{ write: TextWrite; preview: SavePreview } | null>(null);
  /** `problem` for the keystrokes: they never render the tab, so they must not set state unless it changes. */
  const problemShown = useRef<SaveProblem | null>(null);

  const showProblem = (next: SaveProblem | null) => {
    problemShown.current = next;
    setProblem(next);
  };

  /** The document is gone, as the user wanted (deleted) or already was (ruling C46): the tab closes without asking. */
  const closeGone = (kind: "success" | "info", message: string) => {
    toast(kind, message);
    useWorkspace.getState().closeTab(tab.id);
  };

  /** The write went through, or a preview came instead. True when the document is gone. */
  const applyAnswer = (write: Write, answer: SaveAnswer | undefined): boolean => {
    if (write.kind === "remove") {
      console.info("Deleted a document");
      closeGone("success", `Deleted ${ref.id}`);
      return true;
    }
    if (!answer) {
      // A save or an insert always answers: this is a bug, and the text is still in the editor.
      loaded.saveEnded();
      showProblem({ kind: "error", message: "The backend sent no answer to the save: Reload to see what is stored." });
      return false;
    }
    if (isPreview(answer)) {
      previewed.current = { write, preview: answer };
      setPreview(answer);
      return false;
    }
    previewed.current = null;
    setPreview(null);
    const meta = { cas: answer.cas, expiration: answer.expiration, type: "json", size: answer.size } as const;
    console.info(`${write.kind === "save" ? "Saved" : "Inserted"} a document: ${answer.size} bytes`);
    // The server's form replaces the text only while the text is still what was sent (the editor is read-only since the
    // write started, bar the moment before that reached it): a keystroke is never dropped (Q2).
    const unchanged = mounted(editor)?.view.state.doc.eq(write.sent) === true;
    if (write.shown !== undefined && unchanged) loaded.savedAsShown(meta, write.shown);
    else loaded.saved(meta, write.sent);
    return false;
  };

  /** The write did not go through. True when the document is gone anyway (a delete of a document that was). */
  const applyFailure = (write: Write, failure: WriteFailure): boolean => {
    previewed.current = null;
    setPreview(null);
    switch (failure.kind) {
      case "changed":
        loaded.conflict();
        setConflict("changed");
        return false;
      case "deleted":
        if (write.kind === "remove") {
          closeGone("info", `${ref.id} was already deleted`); // ruling C46: what the user wanted is so
          return true;
        }
        loaded.deleted();
        setConflict("deleted");
        return false;
      case "password":
        // Nothing was sent. The tab stays busy (editor read-only) until the same request goes out after the unlock.
        setPasswordNeeded({ error: failure.error, resend: () => void attempt(write) });
        return false;
      case "problem":
        loaded.saveEnded();
        showProblem(failure.problem);
        if (failure.problem.kind === "invalid_json") {
          const handle = mounted(editor);
          handle?.selectRange(failure.problem.offset, failure.problem.offset);
          handle?.focus();
        }
        return false;
    }
  };

  /** Runs one write from start to what it comes to; true when the document is gone. */
  const attempt = async (write: Write): Promise<boolean> => {
    if (inFlight.current) return false;
    inFlight.current = true;
    setBusy(true);
    showProblem(null);
    setPasswordNeeded(null);
    loaded.saveStarted();
    // Only the call is told apart from what is done with its answer: a failure after the write went through is ours.
    const outcome = await sendWrite(ref, write).then(
      (answer) => ({ answer }),
      (error: unknown) => ({ error }),
    );
    inFlight.current = false;
    setBusy(false);
    try {
      if ("error" in outcome) {
        console.warn("A document write failed", outcome.error instanceof ApiError ? outcome.error.code : outcome.error);
        return applyFailure(write, classifyWriteError(outcome.error, (offset) => lineColumn(editor, offset)));
      }
      return applyAnswer(write, outcome.answer);
    } catch (error) {
      // A bug in showing the outcome must not leave the editor read-only for good: the text is still in it.
      console.error("Could not show the outcome of a document write", error);
      loaded.saveEnded();
      showProblem({ kind: "error", message: `Could not show the outcome of the write (${errorMessage(error)}): Reload to see what is stored.` });
      return false;
    }
  };

  const insertFrom = (doc: Text) => attempt({ kind: "insert", text: doc.toString(), sent: doc, confirmed: false });

  /** The CAS these edits were made on is not known (a damaged workspace file): a save cannot say what it replaces. */
  const unknownVersion = () => {
    loaded.conflict();
    setConflict("changed");
  };

  const save = () => {
    const handle = mounted(editor);
    if (!handle || inFlight.current || loaded.saving) return;
    // A read-only connection, a binary document, a load that is not done, a missing document: nothing to write.
    if (!canEdit(loaded.load, readOnly)) return;
    const facts = factsOf(tab.id);
    if (!facts) return;
    const doc = handle.view.state.doc;
    if (facts.isNew) {
      void insertFrom(doc); // a new document is saved even while it is still {}
      return;
    }
    // Decided now, from the editor's document: the `dirty` flag settles only once typing stops (ruling C32).
    const baseline = loaded.baseline();
    if (baseline ? doc.eq(baseline) : !facts.dirty) {
      toast("info", "No changes to save");
      return;
    }
    if (facts.cas === null) return unknownVersion();
    void attempt({ kind: "save", cas: facts.cas, text: doc.toString(), sent: doc, confirmed: false });
  };

  const confirm = () => {
    const held = previewed.current;
    if (!held || inFlight.current) return;
    const { write, preview: shownPreview } = held;
    // The server's form replaces the text; laid out as the text was, when it had line breaks (ruling C13).
    const shown = write.text.includes("\n") ? prettyJson(shownPreview.server_text) : shownPreview.server_text;
    void attempt({ ...write, confirmed: true, shown });
  };

  const cancelPreview = () => {
    // The confirmed write is already on its way: the preview stays until its answer.
    if (inFlight.current || !previewed.current) return;
    previewed.current = null;
    setPreview(null);
    loaded.saveEnded();
  };

  const saveAsNew = () => {
    const handle = mounted(editor);
    if (!handle || inFlight.current) return;
    setConflict(null);
    loaded.saveAsNew(); // the tab is a new document now, holding the edits; the insert follows
    void insertFrom(handle.view.state.doc);
  };

  const remove = async (): Promise<boolean> => {
    const facts = factsOf(tab.id);
    if (!facts || facts.isNew || readOnly || inFlight.current || loaded.saving) return false;
    if (facts.cas === null) {
      unknownVersion();
      return false;
    }
    return attempt({ kind: "remove", cas: facts.cas });
  };

  return {
    busy,
    preview,
    conflict,
    problem,
    passwordNeeded,
    save,
    confirm,
    cancelPreview,
    dismissConflict: () => setConflict(null),
    saveAsNew,
    remove,
    edited: () => {
      const shown = problemShown.current;
      if (shown && TEXT_PROBLEMS.has(shown.kind)) showProblem(null);
    },
    clearProblem: () => {
      if (problemShown.current) showProblem(null);
    },
  };
}
