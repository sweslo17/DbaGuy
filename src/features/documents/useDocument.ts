import type { Text } from "@codemirror/state";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import { ApiError } from "@/lib/api/client";
import { markEdited } from "@/stores/editorBuffers";
import { useWorkspace, type DocumentTabState } from "@/stores/workspace";
import type { CodeEditorHandle } from "../editor/CodeEditor";
import { documentRef, getDocument, isDocumentMissing } from "./documentApi";
import {
  DIRTY_CHECK_MS,
  initialModel,
  transition,
  type DocumentEffect,
  type DocumentEvent,
  type DocumentLoad,
  type DocumentMeta,
  type TabFacts,
} from "./documentState";

export { DIRTY_CHECK_MS };
export type { DocumentLoad, DocumentMeta };

export interface DocumentState {
  load: DocumentLoad;
  /** Reads the document again and puts its text in the editor (ask first when there are unsaved changes). */
  reload: () => void;
  /**
   * After a failed load (RecoverableError's Retry, the unlock dialog): the first load again, which keeps the editor's
   * unsaved text (ruling C29). Never discards anything.
   */
  retry: () => void;
  /** After a save or an insert: the new META, and `baseline` (the document that was sent) as the clean state. */
  saved: (meta: DocumentMeta, baseline: Text) => void;
  /**
   * After a save or an insert the user confirmed from a preview: the editor shows `text`, the server's form (outside the
   * undo history, which this empties like any put text), and that document is the clean state (ruling C13).
   */
  savedAsShown: (meta: DocumentMeta, text: string) => void;
  /**
   * The editor's document as last loaded or saved, null when there is none to compare with. Read when called, from the
   * model, never from what the last render saw: ⌘S asks it at the moment of the key (ruling C32).
   */
  baseline: () => Text | null;
  /** Restored unsaved text whose CAS is not the server's any more: a save will report a conflict. */
  changedOnServer: boolean;
  /** The editor's text changed (the tab calls it from CodeEditor's onChange). */
  edited: () => void;
  /** A save, insert or delete is in flight (`saveStarted` … `saveEnded`, `saved`, `conflict`, `deleted`). */
  saving: boolean;
  saveStarted: () => void;
  /** It ended without a result: refused, failed, or the preview was not confirmed. */
  saveEnded: () => void;
  /** The save found the document changed on the server: the notice says a save will report a conflict. */
  conflict: () => void;
  /** The save or delete found the document gone: the tab shows it missing (its unsaved text stays in view). */
  deleted: () => void;
  /** A missing document's tab becomes a new one ("Create it"): its unsaved text stays, a clean one gets `{}`. */
  created: () => void;
  /** A deleted document's edits are saved as a new document: the tab becomes a new one and keeps what the editor holds. */
  saveAsNew: () => void;
  /**
   * A new document's ID turned out to be taken ("Open the existing document", after the user agreed to give up the
   * draft): the tab becomes the existing document and reads it, replacing the draft.
   */
  openExisting: () => void;
}

/** The tab as the store holds it now (an event may follow an `updateTab` that has not rendered yet), or null. */
export function factsOf(tabId: string): TabFacts | null {
  const tab = useWorkspace.getState().tabs.find((t) => t.id === tabId);
  return tab?.kind === "document" ? tab : null;
}

/** The editor's document, or null when it is not mounted. */
function editorDoc(editor: RefObject<CodeEditorHandle | null>): Text | null {
  try {
    return editor.current?.view.state.doc ?? null;
  } catch {
    return null;
  }
}

const asError = (error: unknown) => (error instanceof Error ? error : new Error(String(error)));

/**
 * A document tab's state (spec §3.13). The transitions are `documentState.transition`'s; this feeds it events (the tab
 * shown, an answer, an edit, a timer) and carries out its effects: it reads the document, puts text in the editor and
 * updates the tab. It reads the document the first time the tab is `shown` (never before: a restored tab nobody has
 * looked at asks nothing), unless the tab is new, and drops a read that is no longer wanted when the tab goes.
 */
export function useDocument(
  tab: DocumentTabState,
  shown: boolean,
  editor: RefObject<CodeEditorHandle | null>,
): DocumentState {
  const tabId = tab.id;
  const [model, setModel] = useState(() => initialModel(tab));
  // Events arrive from timers and answers: the model they act on is the latest one, not the one a render closed over.
  const current = useRef(model);
  const latestTab = useRef(tab);
  /** The editor's text is being put in by the app: the change it makes is not an edit. */
  const putting = useRef(false);
  const dirtyTimer = useRef<number | null>(null);
  /** Handles an event with what the last render saw; `send` (stable) calls it. */
  const handle = useRef<(event: DocumentEvent) => void>(() => undefined);
  const send = useCallback((event: DocumentEvent) => handle.current(event), []);

  useLayoutEffect(() => {
    latestTab.current = tab;
    /** Carries out one effect; false when it failed (the effects after it are not run). */
    const perform = (effect: DocumentEffect): boolean => {
      switch (effect.kind) {
        case "patch-tab":
          useWorkspace.getState().updateTab(tabId, "document", effect.patch);
          return true;
        case "put-text": {
          putting.current = true;
          try {
            if (!editor.current) throw new Error("The editor is not mounted");
            editor.current.setText(effect.text);
          } catch (error) {
            console.error("Could not put a document's text in the editor", error);
            send({ type: "put-failed", error: asError(error) });
            return false;
          } finally {
            putting.current = false;
          }
          const doc = effect.baseline ? editorDoc(editor) : null;
          if (doc) send({ type: "text-put", doc });
          return true;
        }
        case "check-dirty":
          if (dirtyTimer.current !== null) window.clearTimeout(dirtyTimer.current);
          dirtyTimer.current = window.setTimeout(() => {
            dirtyTimer.current = null;
            const doc = editorDoc(editor);
            if (doc) send({ type: "settled", doc });
          }, DIRTY_CHECK_MS);
          return true;
      }
    };
    handle.current = (event) => {
      const facts = factsOf(tabId);
      if (!facts) return; // the tab is closed
      const next = transition(current.current, event, facts);
      if (next.model !== current.current) {
        current.current = next.model;
        setModel(next.model);
      }
      for (const effect of next.effects) if (!perform(effect)) break;
    };
  });

  useEffect(
    () => () => {
      if (dirtyTimer.current !== null) window.clearTimeout(dirtyTimer.current);
    },
    [],
  );

  // The first read waits for the first time the tab is shown; sent again (React's mount check), it changes nothing.
  useEffect(() => {
    if (shown) send({ type: "shown" });
  }, [shown, send]);

  // The read the model asks for runs for as long as it is the one asked for; a newer one (or the tab going) drops it.
  const { request } = model;
  useEffect(() => {
    if (!request) return;
    const { generation } = request;
    const controller = new AbortController();
    void (async () => {
      let answer: DocumentEvent;
      try {
        const view = await getDocument(documentRef(latestTab.current), controller.signal);
        console.info(`Read a document: ${view.type}, ${view.size} bytes`);
        answer = { type: "loaded", generation, view };
      } catch (error) {
        if (controller.signal.aborted) return;
        if (isDocumentMissing(error)) {
          answer = { type: "load-missing", generation };
        } else {
          console.warn("Reading a document failed", error instanceof ApiError ? error.code : error);
          answer = { type: "load-failed", generation, error: asError(error) };
        }
      }
      if (!controller.signal.aborted) send(answer);
    })();
    return () => controller.abort();
  }, [request, send]);

  const commands = useMemo(
    () => ({
      reload: () => send({ type: "reload" }),
      retry: () => send({ type: "retry" }),
      saveStarted: () => send({ type: "save-started" }),
      saveEnded: () => send({ type: "save-ended" }),
      conflict: () => send({ type: "conflict" }),
      deleted: () => send({ type: "deleted" }),
      created: () => send({ type: "created" }),
      saveAsNew: () => send({ type: "save-as-new" }),
      openExisting: () => send({ type: "open-existing" }),
      saved: (meta: DocumentMeta, baseline: Text) =>
        send({ type: "saved", meta, baseline, doc: editorDoc(editor) ?? baseline }),
      savedAsShown: (meta: DocumentMeta, text: string) => send({ type: "saved-as-shown", meta, text }),
      baseline: () => current.current.baseline,
      edited: () => {
        if (putting.current) return;
        markEdited();
        send({ type: "edited" });
      },
    }),
    [send, editor],
  );

  return {
    ...commands,
    load: tab.isNew ? { status: "new" } : model.load,
    changedOnServer: model.changedOnServer,
    saving: model.saving,
  };
}
