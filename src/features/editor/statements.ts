import { StateEffect, StateField, type ChangeDesc, type EditorState, type Extension } from "@codemirror/state";
import { Decoration, EditorView, ViewPlugin, type DecorationSet, type ViewUpdate } from "@codemirror/view";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import type { Dialect } from "@/lib/api/types";

/*
 * The statements of the document, as the backend splitter sees them (`POST /sql/split`: `$$` bodies, BEGIN ATOMIC and
 * trigger bodies are handled there). The split is cached in a state field and mapped through every edit, so the
 * statement under the cursor is known at once; a fresh split replaces it once typing stops (150 ms, longer for a
 * script that takes long to split). Offsets are UTF-16 code units on both sides, which is what JS strings and
 * CodeMirror positions use.
 */

/** UTF-16 offsets into the document: [from, to) is the statement, `terminatorEnd` is after its `;` (or `to`). */
export interface StatementRange {
  from: number;
  to: number;
  terminatorEnd: number;
}

export interface StatementsState {
  /** Bumped by every document change (and by a dialect change): the input of the split. */
  version: number;
  /** The last split, mapped through the changes since; null before the first one. */
  ranges: StatementRange[] | null;
  /** `ranges` is the split of exactly this version. */
  fresh: boolean;
}

const SPLIT_DELAY_MS = 150;
const FLASH_MS = 300;

const setStatements = StateEffect.define<{ version: number; ranges: StatementRange[] }>();
const refresh = StateEffect.define<null>();

function mapRanges(ranges: StatementRange[], changes: ChangeDesc): StatementRange[] {
  const mapped: StatementRange[] = [];
  for (const range of ranges) {
    // Text typed at either edge belongs to the statement until the next split says otherwise.
    const from = changes.mapPos(range.from, -1);
    const to = changes.mapPos(range.to, 1);
    if (to > from) mapped.push({ from, to, terminatorEnd: Math.max(to, changes.mapPos(range.terminatorEnd, -1)) });
  }
  return mapped;
}

export const statementsField = StateField.define<StatementsState>({
  create: () => ({ version: 0, ranges: null, fresh: false }),
  update(value, tr) {
    let next = value;
    if (tr.docChanged) {
      next = { version: value.version + 1, ranges: value.ranges && mapRanges(value.ranges, tr.changes), fresh: false };
    }
    for (const effect of tr.effects) {
      if (effect.is(refresh)) next = { ...next, version: next.version + 1, fresh: false };
      // A split of an older version is dropped: the mapped ranges are closer to the truth than a stale answer.
      else if (effect.is(setStatements) && effect.value.version === next.version) {
        next = { version: next.version, ranges: effect.value.ranges, fresh: true };
      }
    }
    return next;
  },
});

/** Splits `text` with the backend splitter; offsets are relative to `text`. */
export async function splitText(text: string, dialect: Dialect): Promise<StatementRange[]> {
  const client = await api();
  const { statements } = unwrap(await client.POST("/sql/split", { body: { text, dialect } }));
  return statements.map((s) => ({ from: s.start, to: s.end, terminatorEnd: s.terminator_end }));
}

function applySplit(view: EditorView, version: number, ranges: StatementRange[]): void {
  if (view.state.field(statementsField, false)?.version === version) {
    view.dispatch({ effects: setStatements.of({ version, ranges }) });
  }
}

/** Splits again soon, e.g. because the dialect changed (the split depends on it). */
export function refreshStatements(view: EditorView): void {
  view.dispatch({ effects: refresh.of(null) });
}

/**
 * Re-splits the document once typing pauses; answers for an older version are dropped. One split at a time: the
 * whole document goes to the backend (about 0.5 s per MB), so a large script must not pile splits up behind each
 * other, which would also hold up completion. The pause grows with the time the last split took.
 */
function splitter(dialect: () => Dialect | null) {
  return ViewPlugin.fromClass(
    class {
      private timer: number | null = null;
      private destroyed = false;
      /** A split is waiting for the backend. */
      private inFlight = false;
      private lastDurationMs = 0;

      constructor(private readonly view: EditorView) {
        this.schedule(0);
      }

      update(update: ViewUpdate): void {
        if (update.state.field(statementsField).version !== update.startState.field(statementsField).version) {
          this.schedule(Math.max(SPLIT_DELAY_MS, 2 * this.lastDurationMs));
        }
      }

      destroy(): void {
        this.destroyed = true;
        if (this.timer !== null) window.clearTimeout(this.timer);
      }

      private schedule(delay: number): void {
        if (this.timer !== null) window.clearTimeout(this.timer);
        this.timer = window.setTimeout(() => void this.split(), delay);
      }

      private async split(): Promise<void> {
        this.timer = null;
        if (this.inFlight) return; // the split in flight splits again when it returns, if the text moved on
        const current = dialect();
        if (!current) return; // no connection yet: nothing to split for; a dialect change refreshes
        const { state } = this.view;
        const { version } = state.field(statementsField);
        this.inFlight = true;
        const started = performance.now();
        try {
          const ranges = await splitText(state.doc.toString(), current);
          if (!this.destroyed) applySplit(this.view, version, ranges);
        } catch (error) {
          console.warn(`Could not split the statements: ${errorMessage(error)}`);
        } finally {
          this.inFlight = false;
          this.lastDurationMs = performance.now() - started;
        }
        // Changed while it ran, and the split scheduled for that already came and went: once more, now.
        if (!this.destroyed && this.timer === null && this.view.state.field(statementsField).version !== version) {
          void this.split();
        }
      }
    },
  );
}

/** The split cache and its background refresh. `dialect` is read at each split (null: no split). */
export function statementsExtension(opts: { dialect: () => Dialect | null }): Extension {
  return [statementsField, splitter(opts.dialect)];
}

/**
 * The split of the current document, fetched now when the cache is not fresh (typing within the last 150 ms). The
 * ranges are relative to the returned `state`, the document as it was when this was called.
 */
export async function currentStatements(
  view: EditorView,
  dialect: Dialect,
): Promise<{ state: EditorState; ranges: StatementRange[] }> {
  const { state } = view;
  const cached = state.field(statementsField);
  if (cached.fresh && cached.ranges) return { state, ranges: cached.ranges };
  const ranges = await splitText(state.doc.toString(), dialect);
  applySplit(view, cached.version, ranges);
  return { state, ranges };
}

/**
 * The statement containing `pos`, else the one before it (the cursor sits after its `;`), else the first one (the
 * cursor is above every statement). Ranges are sorted and do not overlap.
 */
export function rangeAt(ranges: readonly StatementRange[], pos: number): StatementRange | null {
  return ranges[Math.max(lastStartingAt(ranges, pos), 0)] ?? null;
}

/** Index of the last range that starts at or before `pos`; -1 when every range starts after it. */
function lastStartingAt(ranges: readonly StatementRange[], pos: number): number {
  let low = 0;
  let high = ranges.length - 1;
  let found = -1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (ranges[mid].from <= pos) {
      found = mid;
      low = mid + 1;
    } else high = mid - 1;
  }
  return found;
}

/** The statement under `pos` from the cached split (mapped through edits since); null before the first split. */
export function statementAt(state: EditorState, pos: number): StatementRange | null {
  const ranges = state.field(statementsField, false)?.ranges;
  return ranges ? rangeAt(ranges, pos) : null;
}

/** The most text around the cursor that completion and describe send (②a contract: a window, never the script). */
const WINDOW_LIMIT = 20_000;

const isHighSurrogate = (code: number) => code >= 0xd800 && code <= 0xdbff;
const isLowSurrogate = (code: number) => code >= 0xdc00 && code <= 0xdfff;

/**
 * The text that `/sql/complete` and `/sql/describe` get for `pos`: the statement under it (up to its `;`), else the gap
 * between the statements around it (text typed since the last split), else (no split yet) the whole document; cut to
 * at most 20,000 code units around `pos`. The backend finds the statement under the cursor inside it. As in the
 * backend (`_statement_bounds`), an unterminated last statement owns everything after it: the split ends it before
 * trailing whitespace, but `SELECT * FROM |` is still that statement.
 */
export function statementWindow(state: EditorState, pos: number): { from: number; to: number } {
  const ranges = state.field(statementsField, false)?.ranges;
  let from = 0;
  let to = state.doc.length;
  if (ranges?.length) {
    const index = lastStartingAt(ranges, pos);
    const current = index >= 0 ? ranges[index] : null;
    const unterminatedLast = index === ranges.length - 1 && current?.terminatorEnd === current?.to;
    if (current && (pos <= current.terminatorEnd || unterminatedLast)) {
      from = current.from;
      to = unterminatedLast ? state.doc.length : current.terminatorEnd;
    } else {
      from = current?.terminatorEnd ?? 0;
      to = ranges[index + 1]?.from ?? state.doc.length;
    }
  }
  return clampWindow(state, Math.min(from, pos), Math.max(Math.min(to, state.doc.length), pos), pos);
}

/**
 * [from, to) cut to WINDOW_LIMIT around `pos`: half on each side, a short side's rest to the other. A cut never splits a
 * surrogate pair (a lone surrogate cannot be sent as UTF-8).
 */
function clampWindow(state: EditorState, from: number, to: number, pos: number): { from: number; to: number } {
  if (to - from <= WINDOW_LIMIT) return { from, to };
  const half = WINDOW_LIMIT / 2;
  let start = Math.max(from, Math.min(pos - half, to - WINDOW_LIMIT));
  let end = Math.min(to, start + WINDOW_LIMIT);
  if (start > from && isLowSurrogate(state.doc.sliceString(start, start + 1).charCodeAt(0))) start += 1;
  if (end < to && isHighSurrogate(state.doc.sliceString(end - 1, end).charCodeAt(0))) end -= 1;
  return { from: start, to: end };
}

const currentMark = Decoration.mark({ class: "cm-currentStatement" });

/** A faint background on the statement under the main cursor. */
export function currentStatementHighlight(): Extension {
  return [
    EditorView.decorations.compute([statementsField, "selection"], (state): DecorationSet => {
      const range = statementAt(state, state.selection.main.head);
      if (!range) return Decoration.none;
      const to = Math.min(range.terminatorEnd, state.doc.length);
      return to > range.from ? Decoration.set([currentMark.range(range.from, to)]) : Decoration.none;
    }),
    EditorView.theme({
      ".cm-currentStatement": { backgroundColor: "color-mix(in srgb, var(--selected) 70%, transparent)" },
    }),
  ];
}

interface Flash {
  id: number;
  decorations: DecorationSet;
}

const flashEffect = StateEffect.define<{ id: number; from: number; to: number } | { id: number; clear: true }>();
const flashMark = Decoration.mark({ class: "cm-flashStatement" });
let flashCount = 0;

const flashField = StateField.define<Flash>({
  create: () => ({ id: 0, decorations: Decoration.none }),
  update(value, tr) {
    let next = tr.docChanged ? { ...value, decorations: value.decorations.map(tr.changes) } : value;
    for (const effect of tr.effects) {
      if (!effect.is(flashEffect)) continue;
      const flash = effect.value;
      if ("clear" in flash) {
        if (flash.id === next.id) next = { id: next.id, decorations: Decoration.none };
      } else next = { id: flash.id, decorations: Decoration.set([flashMark.range(flash.from, flash.to)]) };
    }
    return next;
  },
  provide: (field) => EditorView.decorations.from(field, (value) => value.decorations),
});

const flashExtension: Extension = [
  flashField,
  EditorView.theme({
    ".cm-flashStatement": { animation: `cm-flash-statement ${FLASH_MS}ms ease-out` },
    "@keyframes cm-flash-statement": {
      from: { backgroundColor: "color-mix(in srgb, var(--accent) 35%, transparent)" },
      to: { backgroundColor: "transparent" },
    },
  }),
];

/** Flashes [from, to) for 300 ms (the statement that starts running). */
export function flashRange(view: EditorView, from: number, to: number): void {
  const end = Math.min(to, view.state.doc.length);
  if (end <= from) return;
  const id = ++flashCount;
  const effects: StateEffect<unknown>[] = [flashEffect.of({ id, from, to: end })];
  if (!view.state.field(flashField, false)) effects.unshift(StateEffect.appendConfig.of(flashExtension));
  view.dispatch({ effects });
  window.setTimeout(() => view.dispatch({ effects: flashEffect.of({ id, clear: true }) }), FLASH_MS);
}
