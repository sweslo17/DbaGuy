import type { EditorView } from "@codemirror/view";
import type { Dialect } from "@/lib/api/types";
import { currentStatements, rangeAt, splitText, type StatementRange } from "../editor/statements";

/** ⌘↵ runs by the `run.default` setting (or the selection); ⌘⇧↵ runs every statement. */
export type RunMode = "default" | "all";

/** One statement to send: its text and where it is in the editor (UTF-16 offsets, without the `;`). */
export interface PlannedStatement {
  sql: string;
  from: number;
  to: number;
}

/**
 * Which statements a run sends, in order: a non-empty selection → the statements in it; else every statement (⌘⇧↵,
 * or `run.default` "all"); else the statement under the cursor. Empty or comment-only text gives none.
 */
export async function planRun(
  view: EditorView,
  mode: RunMode,
  runDefault: "statement" | "all",
  dialect: Dialect,
): Promise<PlannedStatement[]> {
  const selection = view.state.selection.main;
  if (!selection.empty) {
    const text = view.state.sliceDoc(selection.from, selection.to);
    const ranges = await splitText(text, dialect);
    return ranges.map((r) => ({
      sql: text.slice(r.from, r.to),
      from: selection.from + r.from,
      to: selection.from + r.to,
    }));
  }
  const { state, ranges } = await currentStatements(view, dialect);
  const all = mode === "all" || runDefault === "all";
  const chosen: (StatementRange | null)[] = all ? ranges : [rangeAt(ranges, state.selection.main.head)];
  return chosen.filter((r) => r !== null).map((r) => ({ sql: state.sliceDoc(r.from, r.to), from: r.from, to: r.to }));
}

/**
 * The document offset of an error position inside a statement: `line` is 1-based, `column` 1-based in UTF-16 units
 * (the backend's `ErrorPosition`), so the column is a plain JS string offset within its line.
 */
export function positionToOffset(statementText: string, statementFrom: number, line: number, column: number): number {
  let offset = 0;
  for (let l = 1; l < line; l += 1) {
    const next = statementText.indexOf("\n", offset);
    if (next < 0) return statementFrom + statementText.length;
    offset = next + 1;
  }
  return statementFrom + Math.min(offset + Math.max(0, column - 1), statementText.length);
}
