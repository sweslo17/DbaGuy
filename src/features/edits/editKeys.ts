import { isImeKeyEvent } from "@/lib/ime";
import { isCommandKey } from "@/lib/shortcuts";

/*
 * The editing keys (spec ⑤ §3.6.4, R-05-9), as pure maps from a key event. An input method's key — composing, or the
 * Enter that confirms it, which WebKit may send late with keyCode 229 — is never one (R-02c-9): a Zhuyin user's Enter
 * that picks a character never stages a value or opens the preview.
 */

type KeyLike = Pick<
  KeyboardEvent,
  "key" | "code" | "metaKey" | "ctrlKey" | "altKey" | "shiftKey" | "repeat" | "isComposing" | "keyCode"
>;

/** What a key asks of the focused, editable grid (not of an editor). */
export type GridEditKey = "edit" | "setNull" | "deleteRows" | "undo" | "redo";

const plain = (e: KeyLike) => !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey;
/** ⌥⌘N (DataGrip's key): ⌥N types a dead key on a Mac layout, so the physical key decides. */
export const isSetNullKey = (e: KeyLike) =>
  e.metaKey && e.altKey && !e.ctrlKey && !e.shiftKey && e.code === "KeyN";

/**
 * What a key asks of the grid. A key held down asks for an edit, NULL or a deletion once (L7): a held Return would
 * open and close the editor, a held ⌘⌫ would flip the rows between deleted and restored. Undo and redo are steps back
 * and forward, so holding them walks the history.
 */
export function gridEditKey(e: KeyLike): GridEditKey | null {
  if (isImeKeyEvent(e)) return null;
  if (isCommandKey(e, "z")) return "undo";
  if (isCommandKey(e, "z", { shift: true })) return "redo";
  if (e.repeat) return null;
  if (plain(e) && (e.key === "Enter" || e.key === "F2")) return "edit";
  if (isSetNullKey(e)) return "setNull";
  if (e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey && e.key === "Backspace") return "deleteRows";
  return null;
}

/** What a key asks of the inline cell editor. */
export type CellEditorKey = "commit" | "next" | "previous" | "cancel" | "setNull" | "save";

export function cellEditorKey(e: KeyLike): CellEditorKey | null {
  if (isImeKeyEvent(e)) return null;
  if (plain(e) && e.key === "Enter") return "commit";
  if (!e.metaKey && !e.ctrlKey && !e.altKey && e.key === "Tab") return e.shiftKey ? "previous" : "next";
  if (plain(e) && e.key === "Escape") return "cancel";
  if (isSetNullKey(e)) return "setNull";
  if (isCommandKey(e, "s")) return "save";
  return null;
}
