import { isolateHistory } from "@codemirror/commands";
import { EditorSelection, type EditorState } from "@codemirror/state";
import type { EditorView } from "@codemirror/view";
import type { SqlLanguage } from "sql-formatter";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import type { Dialect, EditorSettings } from "@/lib/api/types";
import { currentStatements, splitText, type StatementRange } from "./statements";

/*
 * ⌘⇧F (spec §4.11): formats the selection, else the statement under the cursor, with `sql-formatter`, locally. The
 * formatter is loaded on first use, so it stays out of the startup bundle.
 */

/** The formatter's language per dialect; a dialect not listed (or no connection) formats as generic SQL. */
const LANGUAGES: Partial<Record<Dialect, SqlLanguage>> = { postgres: "postgresql", sqlite: "sqlite", mysql: "mysql" };

export function formatLanguage(dialect: Dialect | null): SqlLanguage {
  return (dialect && LANGUAGES[dialect]) || "sql";
}

export interface FormatSettings {
  /** The tab's dialect; null (no connection) formats as generic SQL. */
  dialect: Dialect | null;
  editor: Pick<EditorSettings, "keyword_case" | "tab_size">;
}

/** A formatter run per editor at a time: a second ⌘⇧F while the first loads is ignored. */
const formatting = new WeakSet<EditorView>();

/** [from, to) of the selection without its surrounding whitespace, which stays as it is (it separates the lines). */
function trimmed(state: EditorState, from: number, to: number): { from: number; to: number } {
  const text = state.sliceDoc(from, to);
  const start = from + (text.length - text.trimStart().length);
  const end = to - (text.length - text.trimEnd().length);
  return { from: start, to: Math.max(start, end) };
}

/**
 * The statement the cursor is in (from its first character to after its terminator: `;`, or the custom delimiter a
 * MySQL `DELIMITER` line set), or null when the cursor is in the whitespace or comments between statements: then
 * nothing is formatted.
 */
function statementUnder(ranges: readonly StatementRange[], pos: number): StatementRange | null {
  return ranges.find((r) => r.from <= pos && pos <= r.terminatorEnd) ?? null;
}

/**
 * Where `offset` of `before` is in `after`, for texts that differ only in whitespace and letter case (what the
 * formatter changes): after the same number of non-space characters. Keeps the cursor on its token.
 */
function mapOffset(before: string, after: string, offset: number): number {
  let left = 0;
  for (let i = 0; i < offset; i += 1) if (!/\s/.test(before[i])) left += 1;
  let at = 0;
  while (at < after.length && left > 0) {
    if (!/\s/.test(after[at])) left -= 1;
    at += 1;
  }
  return at;
}

/**
 * What to format: the trimmed selection, or the statement under the cursor (without its `;`); null when that is
 * nothing (a blank selection, or the cursor between statements).
 */
async function target(
  view: EditorView,
  dialect: Dialect | null,
): Promise<{ state: EditorState; from: number; to: number; selection: boolean } | null> {
  const { main } = view.state.selection;
  if (!main.empty) {
    const range = trimmed(view.state, main.from, main.to);
    return range.to > range.from ? { state: view.state, ...range, selection: true } : null;
  }
  let state: EditorState;
  let ranges: StatementRange[];
  if (dialect) ({ state, ranges } = await currentStatements(view, dialect));
  else {
    // No connection: the editor keeps no split; the PostgreSQL rules (as completion's fallback) find the statement.
    state = view.state;
    ranges = await splitText(state.doc.toString(), "postgres");
  }
  const range = statementUnder(ranges, state.selection.main.head);
  return range ? { state, from: range.from, to: range.to, selection: false } : null;
}

/**
 * Formats the selection, else the statement under the cursor, as one undo step of its own. A cursor between statements
 * formats nothing (never a neighbouring statement) and says so; a text the formatter cannot parse is left unchanged
 * and the reason shown in a toast.
 */
export async function formatSql(view: EditorView, settings: FormatSettings): Promise<void> {
  if (formatting.has(view)) return;
  formatting.add(view);
  try {
    let found: Awaited<ReturnType<typeof target>>;
    try {
      found = await target(view, settings.dialect);
    } catch (error) {
      toast("error", `Could not find the statement to format: ${errorMessage(error)}`);
      return;
    }
    if (!found) {
      toast("info", "Put the cursor in a statement (or select text) to format it");
      return;
    }
    const { state, from, to, selection } = found;
    const original = state.sliceDoc(from, to);
    let formatted: string;
    try {
      const { format } = await import("sql-formatter");
      formatted = format(original, {
        language: formatLanguage(settings.dialect),
        keywordCase: settings.editor.keyword_case === "upper" ? "upper" : "preserve",
        tabWidth: settings.editor.tab_size,
      });
    } catch (error) {
      // Its parse errors go on for pages (the grammar's expectations): the first line says where.
      const reason = errorMessage(error).split("\n", 1)[0];
      console.warn("Formatting failed", error);
      toast("error", `Could not format the ${selection ? "selection" : "statement"}: ${reason}`);
      return;
    }
    if (view.state.doc !== state.doc) {
      toast("warning", "The text changed — format again");
      return;
    }
    if (formatted === original) return;
    // The formatter joins lines with "\n", as the editor does. A cursor after the statement (by its `;`) moves with it.
    const head = state.selection.main.head;
    const cursor = head >= to ? head + formatted.length - original.length : from + mapOffset(original, formatted, head - from);
    view.dispatch({
      changes: { from, to, insert: formatted },
      selection: selection ? EditorSelection.single(from, from + formatted.length) : EditorSelection.cursor(cursor),
      scrollIntoView: true,
      userEvent: "format",
      annotations: isolateHistory.of("full"), // its own undo step, never merged with typing around it
    });
    console.info(`Formatted ${original.length} characters as ${formatLanguage(settings.dialect)}`);
  } finally {
    formatting.delete(view);
  }
}
