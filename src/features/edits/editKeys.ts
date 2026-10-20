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

/**
 * What a key asks of the inline editor. Return and ⌥⌘N act once, as in the grid (L7): the key-repeat events of the
 * Return that opened the editor land in it, and must not stage what was never typed or repeat a refusal's message.
 */
export function cellEditorKey(e: KeyLike): CellEditorKey | null {
  if (isImeKeyEvent(e)) return null;
  if (plain(e) && e.key === "Enter") return e.repeat ? null : "commit";
  if (!e.metaKey && !e.ctrlKey && !e.altKey && e.key === "Tab") return e.shiftKey ? "previous" : "next";
  if (plain(e) && e.key === "Escape") return "cancel";
  if (isSetNullKey(e)) return e.repeat ? null : "setNull";
  if (isCommandKey(e, "s")) return "save";
  return null;
}

/**
 * What a key asks of the drawer's value editor beyond what CodeMirror does with it. The editor's own keys come first
 * here, behind the input-method check, rather than in a CodeMirror keymap (which skips composition only once the first
 * DOM change of a composition has happened).
 */
export type ValueEditorKey =
  | "apply"
  | "save"
  | "cancel"
  | "setNull"
  // A plain-text value is typed as the keys say (a value's text is never changed by the editor):
  | "newline"
  | "tab"
  | "backspace"
  // The key does nothing, and nothing else sees it:
  | "swallow";

export interface ValueEditorKeyState {
  /** The value is plain text, not JSON: Return, Tab and Backspace insert and delete exactly what they say. */
  plain: boolean;
  /** Vim key bindings are on: Return, Tab, Backspace and Escape are vim's (K15). */
  vim: boolean;
  /** CodeMirror's search panel is open: Escape closes it. */
  searching: boolean;
  /** The editor has seen a key that did not repeat: before that, a repeating Return is the one that opened it. */
  typing: boolean;
}

/**
 * ⌘↵ and ⌘S stage (⌘S then asks for the preview), Escape drops the edit, ⌥⌘N sets NULL; each acts once, a held key
 * never repeats it (L7). Escape is not the edit's under vim (it leaves insert mode or ends a pending command: K15) nor
 * while the search panel is open. In plain text, Return inserts exactly "\n", Tab exactly "\t" and Backspace one
 * character (CodeMirror would copy the line's indentation, insert two spaces and drop a whole indent unit); ⇧Tab does
 * nothing (it would remove indentation). JSON keeps the language's own. A Return held down from the grid, which opened
 * this editor, is swallowed until a key that does not repeat has been pressed in it (L7).
 */
export function valueEditorKey(e: KeyLike, state: ValueEditorKeyState): ValueEditorKey | null {
  if (isImeKeyEvent(e)) return null;
  if (e.key === "Enter" && e.repeat && !state.typing) return "swallow";
  if (state.plain && !state.vim) {
    if (plain(e) && e.key === "Enter") return "newline"; // held down, it types line breaks
    if (plain(e) && e.key === "Tab") return "tab";
    if (e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey && e.key === "Tab") return "swallow";
    if (plain(e) && e.key === "Backspace") return "backspace";
  }
  if (e.repeat) return null;
  if (e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey && e.key === "Enter") return "apply";
  if (isCommandKey(e, "s")) return "save";
  if (plain(e) && e.key === "Escape" && !state.vim && !state.searching) return "cancel";
  if (isSetNullKey(e)) return "setNull";
  return null;
}
