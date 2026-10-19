import { history } from "@codemirror/commands";
import {
  Compartment,
  EditorSelection,
  type EditorState,
  type Extension,
  type TransactionSpec,
} from "@codemirror/state";
import { clamp } from "@/lib/math";

/*
 * An editor's text is sometimes set from outside as a new starting point (a script read from disk, a Couchbase
 * document loaded or reloaded). CodeMirror's history cannot simply skip such a replacement: it keeps its old events and
 * maps them through it, so Undo would put pieces of the old text into the new one (a deleted piece coming back inside
 * the server's text, and the tab turning dirty). The only way to forget history is to take its state field out of the
 * configuration and put it back, which is why it sits in a compartment.
 */

const HISTORY = history();
const historySlot = new Compartment();

/** The undo history (in the compartment `startingPoint` empties). */
export const undoHistory: Extension = historySlot.of(HISTORY);

/**
 * The transactions that make `text` the editor's starting point, to dispatch in order, one at a time: the whole text is
 * replaced with the selection as given (clamped to the new text, else at the start; the view scrolls to it), and the
 * undo and redo history is empty afterwards, so Undo and Redo do nothing until the user types again. Both steps are
 * needed: the first replaces the text while the history is out of the configuration (so it never sees the
 * replacement), the second puts a new, empty history back. The state's other settings are untouched.
 */
export function startingPoint(
  state: EditorState,
  text: string,
  selection?: { anchor: number; head: number },
): TransactionSpec[] {
  const insert = state.toText(text); // its length as the editor counts it (line breaks normalised)
  return [
    {
      changes: { from: 0, to: state.doc.length, insert },
      selection: EditorSelection.single(
        clamp(selection?.anchor ?? 0, 0, insert.length),
        clamp(selection?.head ?? 0, 0, insert.length),
      ),
      scrollIntoView: true,
      effects: historySlot.reconfigure([]),
    },
    { effects: historySlot.reconfigure(HISTORY) },
  ];
}
