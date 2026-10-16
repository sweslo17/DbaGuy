import { EditorSelection } from "@codemirror/state";
import type { EditorView } from "@codemirror/view";
import { addErrorMark, startTracking, tokenEnd, trackedSpan, type Span } from "../editor/diagnostics";
import { flashRange } from "../editor/statements";
import type { ExecutionState } from "../session/execution";
import { positionToOffset, type PlannedStatement } from "./runPlan";

/*
 * What a run shows in the editor: the statement that starts flashes, a failed one gets an error mark, and an error in
 * Messages jumps to its place. Positions come from the run's tracked ranges, so edits made since still land right.
 */

/** A run starts: track its statements and clear the previous run's marks. */
export function beginRunMarks(view: EditorView, planned: readonly PlannedStatement[]): void {
  startTracking(
    view,
    planned.map(({ from, to }) => ({ from, to })),
  );
}

/** Statement `index` starts running. */
export function flashStatement(view: EditorView, index: number): void {
  const span = trackedSpan(view.state, index);
  if (span) flashRange(view, span.from, span.to);
}

/** Where the error of statement `index` is: its token when the database gave a position, else the whole statement. */
function errorSpan(view: EditorView, index: number, execution: ExecutionState): Span | null {
  const span = trackedSpan(view.state, index);
  const position = execution.error?.position;
  if (!span || !position) return span;
  const at = Math.min(positionToOffset(execution.sql, span.from, position.line, position.column), span.to);
  return { from: at, to: tokenEnd(view.state, at, span.to) };
}

/** Statement `index` settled: mark its error, if it has one. */
export function markStatementError(view: EditorView, index: number, execution: ExecutionState): void {
  if (!execution.error) return;
  const span = errorSpan(view, index, execution);
  if (span) addErrorMark(view, span.from, span.to, execution.error.message);
}

/** The error's place as the editor shows it: 1-based line and column (UTF-16 units, like the editor). */
export function errorLocation(
  view: EditorView,
  index: number,
  execution: ExecutionState,
): { line: number; column: number } | null {
  const span = errorSpan(view, index, execution);
  if (!span) return null;
  const line = view.state.doc.lineAt(Math.min(span.from, view.state.doc.length));
  return { line: line.number, column: span.from - line.from + 1 };
}

/** Puts the cursor on the error of statement `index` (or its start) and focuses the editor. */
export function jumpToError(view: EditorView, index: number, execution: ExecutionState): void {
  const span = errorSpan(view, index, execution);
  if (!span) return;
  const at = Math.min(span.from, view.state.doc.length);
  view.dispatch({ selection: EditorSelection.cursor(at), scrollIntoView: true });
  view.focus();
}
