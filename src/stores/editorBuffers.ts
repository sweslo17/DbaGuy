import type { TextSelection } from "@/lib/api/types";

/** A query tab's editor content, as `workspace.json` keeps it. */
export interface EditorBuffer {
  text: string;
  selection: TextSelection;
}

/** Reads a mounted editor; null when it cannot (the editor is already gone). */
export type EditorBufferReader = () => EditorBuffer | null;

/*
 * The query tabs' editor content lives in CodeMirror, never in React state (ruling R-02c-4): a keystroke only calls
 * `markEdited`, and the workspace writer reads the text when it saves.
 */
const readers = new Map<string, EditorBufferReader>();
/** What an editor held when it unmounted while its tab stayed open, by tab id. */
const lastKnown = new Map<string, EditorBuffer>();
const editListeners = new Set<() => void>();

/** Registers a mounted editor's reader; the returned function unregisters it, keeping what it held last. */
export function registerEditorBuffer(tabId: string, read: EditorBufferReader): () => void {
  readers.set(tabId, read);
  return () => {
    const final = safeRead(read);
    if (final) lastKnown.set(tabId, final);
    if (readers.get(tabId) === read) readers.delete(tabId);
  };
}

function safeRead(read: EditorBufferReader): EditorBuffer | null {
  try {
    return read();
  } catch (error) {
    console.warn("Could not read an editor's content", error);
    return null;
  }
}

/** The tab's editor content now: its mounted editor's, else what it held last; null if it never mounted. */
export function readEditorBuffer(tabId: string): EditorBuffer | null {
  const read = readers.get(tabId);
  return (read && safeRead(read)) ?? lastKnown.get(tabId) ?? null;
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
