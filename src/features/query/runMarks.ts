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

/**
 * The length of each statement's `prefix` in the latest run of each editor. The database counts positions in the text it
 * was sent, which starts with the prefix (Explain's `EXPLAIN `); the editor's text does not have it.
 */
const prefixLengths = new WeakMap<EditorView, readonly number[]>();

/** A run starts: track its statements (and the prefixes they are sent with) and clear the previous run's marks. */
export function beginRunMarks(view: EditorView, planned: readonly PlannedStatement[]): void {
  prefixLengths.set(view, planned.map(({ prefix }) => prefix?.length ?? 0));
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

/**
 * Where the error of statement `index` is: its token when the database gave a position, else the whole statement. The
 * position is read in the text that was sent (`execution.sql`) and moved back by the statement's prefix, never to before
 * the statement's start: an error in the prefix itself is at the start (M10).
 */
function errorSpan(view: EditorView, index: number, execution: ExecutionState): Span | null {
  const span = trackedSpan(view.state, index);
  const position = execution.error?.position;
  if (!span || !position) return span;
  const prefix = prefixLengths.get(view)?.[index] ?? 0;
  const sent = positionToOffset(execution.sql, span.from, position.line, position.column);
  const at = Math.min(Math.max(sent - prefix, span.from), span.to);
  return { from: at, to: tokenEnd(view.state, at, span.to) };
}

/** Statement `index` settled: mark its error, if it has one. */
export function markStatementError(view: EditorView, index: number, execution: ExecutionState): void {
  if (!execution.error) return;
  const span = errorSpan(view, index, execution);
  if (span) addErrorMark(view, span.from, span.to, execution.error.message);
}

/**
 * The error's place as the editor shows it: 1-based line and column (UTF-16 units, like the editor); the column is null
 * when the database gave no position (the line is then where the statement starts).
 */
export function errorLocation(
  view: EditorView,
  index: number,
  execution: ExecutionState,
): { line: number; column: number | null } | null {
  const span = errorSpan(view, index, execution);
  if (!span) return null;
  const line = view.state.doc.lineAt(Math.min(span.from, view.state.doc.length));
  return { line: line.number, column: execution.error?.position ? span.from - line.from + 1 : null };
}

/** Puts the cursor on the error of statement `index` (or its start) and focuses the editor. */
export function jumpToError(view: EditorView, index: number, execution: ExecutionState): void {
  const span = errorSpan(view, index, execution);
  if (!span) return;
  const at = Math.min(span.from, view.state.doc.length);
  view.dispatch({ selection: EditorSelection.cursor(at), scrollIntoView: true });
  view.focus();
}
