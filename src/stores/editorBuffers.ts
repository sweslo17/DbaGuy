import type { CodeEditorHandle } from "@/features/editor/CodeEditor";
import type { TextSelection } from "@/lib/api/types";

/** A query tab's editor content, as `workspace.json` keeps it. */
export interface EditorBuffer {
  text: string;
  selection: TextSelection;
}

/** A query tab's editor while it is mounted; null once it is gone. */
export type MountedEditor = () => CodeEditorHandle | null;

/*
 * The query tabs' mounted editors, by tab id. Their content lives in CodeMirror, never in React state (ruling
 * R-02c-4): a keystroke only calls `markEdited`, the workspace writer reads the text when it saves, and the History
 * panel inserts into the active tab's editor.
 */
const editors = new Map<string, MountedEditor>();
/** What each tab's editor held when it was last destroyed, by tab id (dropped once the tab is closed). */
const lastKnown = new Map<string, EditorBuffer>();
const editListeners = new Set<() => void>();

/** Registers a tab's mounted editor; the returned function unregisters it. */
export function registerEditor(tabId: string, editor: MountedEditor): () => void {
  editors.set(tabId, editor);
  return () => {
    if (editors.get(tabId) === editor) editors.delete(tabId);
  };
}

/**
 * A tab's editor is being destroyed (ruling R-02c-14): what it held, read by the editor itself just before, so it does
 * not depend on the order React detaches refs and runs cleanups (a crash unmounts everything at once). A mounted
 * editor's live content still wins, so the write from StrictMode's mount check (the first editor it destroys) is
 * harmless; the real unmount writes again.
 */
export function rememberEditorBuffer(tabId: string, buffer: EditorBuffer): void {
  lastKnown.set(tabId, buffer);
}

function readMounted(editor: MountedEditor): EditorBuffer | null {
  try {
    const state = editor()?.view.state;
    if (!state) return null;
    const { anchor, head } = state.selection.main;
    return { text: state.doc.toString(), selection: { anchor, head } };
  } catch (error) {
    console.warn("Could not read an editor's content", error);
    return null;
  }
}

/**
 * The tab's editor content now: its mounted editor's, else what it held when it was destroyed (a tab whose content
 * crashed, or one being mounted again); null if it never mounted.
 */
export function readEditorBuffer(tabId: string): EditorBuffer | null {
  const editor = editors.get(tabId);
  return (editor && readMounted(editor)) ?? lastKnown.get(tabId) ?? null;
}

/** The tab's editor, if it is mounted. */
export function mountedEditor(tabId: string): CodeEditorHandle | null {
  return editors.get(tabId)?.() ?? null;
}

/** Drops what is kept for tabs that are no longer open. */
export function forgetEditorBuffersExcept(openTabIds: ReadonlySet<string>): void {
  for (const tabId of lastKnown.keys()) if (!openTabIds.has(tabId)) lastKnown.delete(tabId);
}

/** An editor's text or selection changed. Called on every keystroke, so it only notifies (it reads nothing). */
export function markEdited(): void {
  for (const listener of editListeners) listener();
}

export function onEdited(listener: () => void): () => void {
  editListeners.add(listener);
  return () => editListeners.delete(listener);
}

// Dev only: the mounted editors live here for the app's lifetime. A hot update would hand the writer an empty copy (it
// would save stale text), so the page reloads instead; the writer's flush on unload keeps the latest text.
if (import.meta.hot) {
  import.meta.hot.accept();
  import.meta.hot.dispose(() => window.location.reload());
}
