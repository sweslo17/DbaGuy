import { forEachDiagnostic, setDiagnostics, setDiagnosticsEffect, type Diagnostic } from "@codemirror/lint";
import { EditorState, StateEffect, StateField, type ChangeDesc, type Extension } from "@codemirror/state";
import { EditorView } from "@codemirror/view";

/*
 * Error marks from statement errors (`@codemirror/lint` diagnostics set by hand: there is no linter). A mark goes away
 * when a new run starts or when an edit touches the marked text. The ranges a run executed are tracked through edits,
 * so marks, flashes and jumps made while (or after) the run go where the text moved.
 */

export interface Span {
  from: number;
  to: number;
}

const setTracked = StateEffect.define<readonly Span[]>();

/** Where a span is after `changes`: text inserted at either edge stays outside it. */
function mapSpan<T extends Span>(span: T, changes: ChangeDesc): T {
  const from = changes.mapPos(span.from, 1);
  return { ...span, from, to: Math.max(from, changes.mapPos(span.to, -1)) };
}

const trackedField = StateField.define<readonly Span[]>({
  create: () => [],
  update(spans, tr) {
    let next = spans;
    if (tr.docChanged && spans.length) next = spans.map((span) => mapSpan(span, tr.changes));
    for (const effect of tr.effects) if (effect.is(setTracked)) next = effect.value;
    return next;
  },
});

/** Every diagnostic, at its current position. */
function currentMarks(state: EditorState): Diagnostic[] {
  const marks: Diagnostic[] = [];
  forEachDiagnostic(state, (diagnostic, from, to) => marks.push({ ...diagnostic, from, to }));
  return marks;
}

/** An edit inside (or at the edge of) a mark removes that mark; the others move with the text. */
const dropTouchedMarks = EditorState.transactionExtender.of((tr) => {
  if (!tr.docChanged) return null;
  const marks = currentMarks(tr.startState);
  const kept = marks.filter((mark) => !tr.changes.touchesRange(mark.from, mark.to));
  if (kept.length === marks.length) return null;
  return { effects: setDiagnosticsEffect.of(kept.map((mark) => mapSpan(mark, tr.changes))) };
});

/** Lint's own colours are fixed; these follow the theme tokens. */
const markTheme = EditorView.theme({
  ".cm-lintRange-error": {
    backgroundImage: "none",
    textDecoration: "underline wavy var(--danger)",
    textDecorationSkipInk: "none",
    textUnderlineOffset: "3px",
  },
  ".cm-lintPoint-error:after": { borderBottomColor: "var(--danger)" },
  ".cm-diagnostic": { padding: "4px 8px", fontFamily: "var(--font-sans)", fontSize: "12px", whiteSpace: "pre-wrap" },
  ".cm-diagnostic-error": { borderLeftColor: "var(--danger)" },
});

/** Error marks and run-range tracking (lint itself is enabled by the first mark). */
export function errorMarksExtension(): Extension {
  return [trackedField, dropTouchedMarks, markTheme];
}

/** A new run: its statement ranges are tracked from now on, and the previous run's marks go. */
export function startTracking(view: EditorView, spans: readonly Span[]): void {
  const effects = [setTracked.of(spans)];
  view.dispatch(currentMarks(view.state).length ? { effects: [...effects, setDiagnosticsEffect.of([])] } : { effects });
}

/** Where the `index`-th tracked range is now; null if there is none. */
export function trackedSpan(state: EditorState, index: number): Span | null {
  return state.field(trackedField, false)?.[index] ?? null;
}

/** Marks [from, to) as an error with `message` (shown on hover), next to the marks already there. */
export function addErrorMark(view: EditorView, from: number, to: number, message: string): void {
  const length = view.state.doc.length;
  const start = Math.min(from, length);
  const mark: Diagnostic = { from: start, to: Math.min(Math.max(to, start), length), severity: "error", message };
  view.dispatch(setDiagnostics(view.state, [...currentMarks(view.state), mark]));
}

const WORD = /[\p{L}\p{N}_$]/u;
const QUOTES = new Set(['"', "'", "`"]);

/**
 * The end of the token at `pos`, at most `limit`: a word (letters, digits, `_`, `$`), a quoted name or string, else one
 * character. `pos` itself when there is no text there (an error at the end of the statement).
 */
export function tokenEnd(state: EditorState, pos: number, limit: number): number {
  const text = state.sliceDoc(pos, Math.max(pos, limit));
  if (!text) return pos;
  if (QUOTES.has(text[0])) {
    const close = text.indexOf(text[0], 1);
    return pos + (close > 0 ? close + 1 : text.length);
  }
  let length = 0;
  for (const char of text) {
    if (!WORD.test(char)) break;
    length += char.length; // UTF-16 units: an astral letter is two
  }
  return pos + (length || String.fromCodePoint(text.codePointAt(0)!).length);
}
