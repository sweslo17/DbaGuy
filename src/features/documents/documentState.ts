import type { Text } from "@codemirror/state";
import type { DocumentView } from "@/lib/api/types";
import type { DocumentTabState } from "@/stores/workspace";

/*
 * Every state change of a Couchbase document tab (spec §3.13), as one pure function: `transition` takes the model, an
 * event and what the tab holds, and gives the next model with the effects to run. It decides what happens to the
 * user's unsaved edits (Q2): whether the server's text replaces the editor's, what counts as unsaved, and which
 * panel hides the editor. `useDocument` only feeds it events and carries out the effects, and holds no state of its own.
 *
 * What it knows about unsaved edits (rulings C15, C28-C30, C33):
 * - the editor's text lives in CodeMirror, never here; the model keeps only `baseline`, the editor's document as it
 *   was last loaded or saved, to tell whether the text differs (`Text.eq` skips shared structure: cheap even for
 *   20 MiB);
 * - a first read (the tab shown, or Retry and the unlock dialog after a failure) never replaces unsaved text: a tab is
 *   clean only when it is neither `dirty` nor `isNew` (not "its `initialText` is null": a tab edited and mounted
 *   again by its error boundary has none and unsaved text);
 * - only a reload discards edits, and only the user's Reload (or Task 7's "Open the existing document") sends one;
 * - a missing document keeps the editor when it holds unsaved text.
 */

/** A new document starts as this text. */
export const NEW_DOCUMENT_TEXT = "{}";

/** The dirty check runs this long after typing stops (a keystroke itself only marks the tab dirty). */
export const DIRTY_CHECK_MS = 200;

/** What a document's tab shows of it besides its text: the server's own fields, so it follows the API's type. */
export type DocumentMeta = Pick<DocumentView, "cas" | "expiration" | "type" | "size">;

export type DocumentLoad =
  | { status: "new" } // not in the database yet: nothing to load
  | { status: "loading" }
  | { status: "ready"; meta: DocumentMeta }
  | { status: "missing" }
  | { status: "error"; error: Error };

/** The fields of the tab the store owns and a transition reads (it is given them fresh, never from a render). */
export type TabFacts = Pick<DocumentTabState, "isNew" | "cas" | "dirty" | "initialText">;
/** What a transition asks the store to change on the tab. */
export type TabPatch = Partial<TabFacts>;

/** A read of the document: the first one, or one the user asked for that replaces the editor's text. */
export type ReadMode = "first" | "reload";

export interface DocumentModel {
  load: DocumentLoad;
  /**
   * The read that should be in flight, or null. A new object for each read: the hook runs it for as long as it stays
   * the same one (and drops it once it is not), and an answer for another `generation` is ignored.
   */
  request: { generation: number; mode: ReadMode } | null;
  /** The last read's number. */
  generation: number;
  /** The tab has been shown, so its first read has been decided: a tab that is never shown reads nothing. */
  started: boolean;
  /** The editor's document as last loaded or saved: null when there is none to compare with (see the header). */
  baseline: Text | null;
  /** The text was edited from a CAS that is not the server's any more: a save will report a conflict. */
  changedOnServer: boolean;
  /** A save, insert or delete is in flight: no read starts meanwhile. */
  saving: boolean;
}

export type DocumentEvent =
  /** The tab is shown (the hook sends it once the tab has been shown, and again harmlessly). */
  | { type: "shown" }
  /** A read answered. */
  | { type: "loaded"; generation: number; view: DocumentView }
  | { type: "load-missing"; generation: number }
  | { type: "load-failed"; generation: number; error: Error }
  /** After `put-text` with `baseline: true`: the editor's document, now holding the text just put in. */
  | { type: "text-put"; doc: Text }
  /** `put-text` could not be done (the editor is not mounted). */
  | { type: "put-failed"; error: Error }
  /** Retry after a failed read, or the unlock dialog has the password: the first read again, which keeps edits. */
  | { type: "retry" }
  /** Reload: read the document and replace the editor's text, edits included (the user has agreed). */
  | { type: "reload" }
  /** The user edited the text (never a text the app put in). */
  | { type: "edited" }
  /** `DIRTY_CHECK_MS` after the last edit: the editor's document. */
  | { type: "settled"; doc: Text }
  | { type: "save-started" }
  /** A save, insert or delete ended without a result for the model: refused, failed or not confirmed. */
  | { type: "save-ended" }
  /** A save found the document changed on the server (`document_conflict`). */
  | { type: "conflict" }
  /** A save or delete found the document gone (`document_missing`). */
  | { type: "deleted" }
  /** A missing document's tab becomes a new one ("Create it"): its unsaved text stays. */
  | { type: "created" }
  /** A save or insert went through: `baseline` is the document that was sent, `doc` the editor's now. */
  | { type: "saved"; meta: DocumentMeta; baseline: Text; doc: Text };

export type DocumentEffect =
  /** `updateTab(id, "document", patch)`. */
  | { kind: "patch-tab"; patch: TabPatch }
  /**
   * Put `text` in the editor outside the undo history (the hook does not count it as an edit). With `baseline`, the
   * hook then sends `text-put` with the editor's document.
   */
  | { kind: "put-text"; text: string; baseline: boolean }
  /** (Re)start the `DIRTY_CHECK_MS` timer: when it runs out, `settled`. */
  | { kind: "check-dirty" };

export interface Transition {
  model: DocumentModel;
  effects: readonly DocumentEffect[];
}

export function initialModel(tab: Pick<TabFacts, "isNew">): DocumentModel {
  return {
    load: tab.isNew ? { status: "new" } : { status: "loading" },
    request: null,
    generation: 0,
    started: false,
    baseline: null,
    changedOnServer: false,
    saving: false,
  };
}

const NO_EFFECTS: readonly DocumentEffect[] = [];

const stay = (model: DocumentModel): Transition => ({ model, effects: NO_EFFECTS });

/** The fields of `patch` that are not what the tab holds already; no effect when it holds them all. */
function patchTab(tab: TabFacts, patch: TabPatch): readonly DocumentEffect[] {
  const changed = Object.fromEntries(
    Object.entries(patch).filter(([field, value]) => tab[field as keyof TabFacts] !== value),
  ) as TabPatch;
  return Object.keys(changed).length > 0 ? [{ kind: "patch-tab", patch: changed }] : NO_EFFECTS;
}

/** Starts a read: the model asks for it and shows it loading. */
function begin(model: DocumentModel, mode: ReadMode): DocumentModel {
  const generation = model.generation + 1;
  return { ...model, started: true, generation, request: { generation, mode }, load: { status: "loading" } };
}

/** The server holds no such document (a read or a write found it gone). */
const gone = (model: DocumentModel): DocumentModel => ({
  ...model,
  request: null,
  load: { status: "missing" },
  baseline: null, // nothing on the server to be clean against
  changedOnServer: false,
  saving: false,
});

const metaOf = ({ cas, expiration, type, size }: DocumentView): DocumentMeta => ({ cas, expiration, type, size });

const isCurrent = (model: DocumentModel, generation: number) => model.request?.generation === generation;

function loaded(model: DocumentModel, view: DocumentView, tab: TabFacts): Transition {
  const mode = model.request?.mode ?? "first";
  // A new document has nothing on the server to show: a first read that "New" overtook changes nothing.
  if (mode === "first" && tab.isNew) return stay({ ...model, request: null });
  const ready: DocumentLoad = { status: "ready", meta: metaOf(view) };
  if (mode === "first" && tab.dirty) {
    // Unsaved text stays, whatever the server holds: only a save or the user's Reload decides about it.
    return stay({ ...model, request: null, load: ready, changedOnServer: tab.cas !== view.cas });
  }
  // A clean tab, or a reload: the server's text goes in, and it is the baseline once it is in the editor.
  return {
    model: { ...model, request: null, load: ready, changedOnServer: false, baseline: null },
    effects: [
      { kind: "put-text", text: view.text ?? "", baseline: true },
      ...patchTab(tab, { cas: view.cas, dirty: false, isNew: false }),
    ],
  };
}

/** Where the model goes from `event`, and what the hook then does. Pure: it reads and writes nothing else. */
export function transition(model: DocumentModel, event: DocumentEvent, tab: TabFacts): Transition {
  switch (event.type) {
    case "shown":
      if (model.started) return stay(model);
      if (tab.isNew) return stay({ ...model, started: true });
      return stay(begin(model, "first"));
    case "loaded":
      return isCurrent(model, event.generation) ? loaded(model, event.view, tab) : stay(model);
    case "load-missing":
      return isCurrent(model, event.generation) ? stay(gone(model)) : stay(model);
    case "load-failed":
      if (!isCurrent(model, event.generation)) return stay(model);
      return stay({ ...model, request: null, load: { status: "error", error: event.error } });
    case "text-put":
      return stay({ ...model, baseline: event.doc });
    case "put-failed":
      return stay({ ...model, request: null, baseline: null, load: { status: "error", error: event.error } });
    case "retry":
      // Only after a failure, and never while a write is in flight; a second click finds the read already running.
      return model.load.status === "error" && !model.saving ? stay(begin(model, "first")) : stay(model);
    case "reload":
      return model.saving ? stay(model) : stay(begin(model, "reload"));
    case "edited": {
      // Dirty at once; whether the text is back to the baseline is found out once typing stops.
      const check: readonly DocumentEffect[] = model.baseline ? [{ kind: "check-dirty" }] : NO_EFFECTS;
      return { model, effects: [...patchTab(tab, { dirty: true }), ...check] };
    }
    case "settled":
      if (tab.dirty && model.baseline && event.doc.eq(model.baseline)) {
        return { model, effects: patchTab(tab, { dirty: false }) };
      }
      return stay(model);
    case "save-started":
      return model.saving ? stay(model) : stay({ ...model, saving: true });
    case "save-ended":
      return model.saving ? stay({ ...model, saving: false }) : stay(model);
    case "conflict":
      return stay({ ...model, saving: false, changedOnServer: true });
    case "deleted":
      return stay(gone(model));
    case "created": {
      // The edits stay; a tab without any starts as `{}` (a clean missing tab shows no editor, so nothing is lost).
      const clean = !tab.dirty;
      const start: readonly DocumentEffect[] = clean
        ? [{ kind: "put-text", text: NEW_DOCUMENT_TEXT, baseline: false }]
        : NO_EFFECTS;
      return {
        model: { ...model, request: null, load: { status: "new" }, baseline: null, changedOnServer: false, saving: false },
        effects: [
          ...start,
          ...patchTab(tab, { isNew: true, cas: null, ...(clean ? { initialText: NEW_DOCUMENT_TEXT } : {}) }),
        ],
      };
    }
    case "saved": {
      const { meta, baseline, doc } = event;
      // What was sent is the clean state: text changed while the save ran stays told apart from it.
      const next: DocumentModel = {
        ...model,
        request: null,
        load: { status: "ready", meta },
        baseline,
        changedOnServer: false,
        saving: false,
      };
      return { model: next, effects: patchTab(tab, { cas: meta.cas, isNew: false, dirty: !doc.eq(baseline) }) };
    }
  }
}

/** What shows in place of the editor; null: the editor itself. */
export type Panel = "loading" | "error" | "missing" | "binary";

/**
 * The panel that hides the editor, hidden and never unmounted (ruling C28). A missing document whose tab holds unsaved
 * text keeps its editor (ruling C30: the edits stay in view, and can be saved as a new document).
 */
export function panelOf(load: DocumentLoad, dirty: boolean): Panel | null {
  switch (load.status) {
    case "loading":
      return "loading";
    case "error":
      return "error";
    case "missing":
      return dirty ? null : "missing";
    case "ready":
      return load.meta.type === "base64" ? "binary" : null;
    case "new":
      return null;
  }
}

/** Whether the text can be typed in: a document that is read (JSON) or new, on a connection that is not read-only. */
export function canEdit(load: DocumentLoad, readOnlyProfile: boolean): boolean {
  if (readOnlyProfile) return false;
  return load.status === "new" || (load.status === "ready" && load.meta.type === "json");
}
