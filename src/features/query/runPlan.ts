import type { EditorView } from "@codemirror/view";
import type { Dialect, StatementParams } from "@/lib/api/types";
import { currentStatements, rangeAt, splitText, type StatementRange } from "../editor/statements";
import { EXPLAIN_PREFIX, isPlanRequest } from "../explain/explainText";
import { paramsFor, type ParamProblem } from "./params";

/** ⌘↵ runs by the `run.default` setting (or the selection); ⌘⇧↵ runs every statement. */
export type RunMode = "default" | "all";

/**
 * One statement to send: its text and where it is in the editor (UTF-16 offsets, without the `;`). `repeat` is how
 * many times it is sent (T-SQL `GO n`; 1 otherwise, 0 for `GO 0`). `position` is its place in the run plan (0-based):
 * what the user counts, kept when `GO 0` batches are dropped and `GO n` copies are made. `params` are the parameters
 * it uses (SQL++, see `StatementRange`); `values` is what it is sent with, set by the tab once every one has a value
 * (`bindParams`): undefined or null sends none. `prefix` is text sent before the statement that is not in the editor
 * (Explain's `EXPLAIN `): `sentText` is what the database receives, and positions it reports count the prefix.
 */
export interface PlannedStatement {
  sql: string;
  from: number;
  to: number;
  repeat: number;
  position: number;
  params: string[];
  values?: StatementParams | null;
  prefix?: string;
}

/** The text a statement is sent as: its prefix, then the editor's text. */
export function sentText(statement: PlannedStatement): string {
  return (statement.prefix ?? "") + statement.sql;
}

/**
 * The statement as Explain sends it: after `EXPLAIN ` unless it starts with EXPLAIN or ADVISE (after whitespace), and
 * with the values the drafts give for the parameters it uses. A plan does not need the values, so a parameter without a
 * usable one is simply left out (`paramsFor` lenient) and Explain never refuses for one.
 */
export function explainStatement(
  statement: PlannedStatement,
  drafts: Readonly<Record<string, string>>,
): PlannedStatement {
  const result = paramsFor(statement.params, drafts, true);
  const values = "problem" in result ? null : result.params;
  return { ...statement, values, prefix: isPlanRequest(statement.sql) ? undefined : EXPLAIN_PREFIX };
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
    return ranges.map((r, position) => ({
      sql: text.slice(r.from, r.to),
      from: selection.from + r.from,
      to: selection.from + r.to,
      repeat: r.repeat,
      position,
      params: r.params,
    }));
  }
  const { state, ranges } = await currentStatements(view, dialect);
  const all = mode === "all" || runDefault === "all";
  const chosen: (StatementRange | null)[] = all ? ranges : [rangeAt(ranges, state.selection.main.head)];
  return chosen
    .filter((r) => r !== null)
    .map((r, position) => ({
      sql: state.sliceDoc(r.from, r.to),
      from: r.from,
      to: r.to,
      repeat: r.repeat,
      position,
      params: r.params,
    }));
}

/**
 * The statements with the values they are sent with, from the tab's drafts; or the first parameter that has none (or
 * text that is not JSON), when nothing may be sent or analysed.
 */
export function bindParams(
  planned: readonly PlannedStatement[],
  drafts: Readonly<Record<string, string>>,
): { statements: PlannedStatement[] } | ParamProblem {
  const statements: PlannedStatement[] = [];
  for (const statement of planned) {
    const result = paramsFor(statement.params, drafts);
    if ("problem" in result) return result;
    statements.push({ ...statement, values: result.params });
  }
  return { statements };
}

/**
 * The statements a run really sends: each one `repeat` times, in order (`GO 0` sends none). A confirmation given to a
 * statement holds for each of its copies, so `GO 100` asks once; the copies share the statement's editor offsets.
 */
export function expandRepeats(
  planned: readonly PlannedStatement[],
  confirmed: ReadonlySet<number>,
): { statements: PlannedStatement[]; confirmed: Set<number> } {
  const statements: PlannedStatement[] = [];
  const agreed = new Set<number>();
  planned.forEach((statement, index) => {
    for (let copy = 0; copy < statement.repeat; copy += 1) {
      if (confirmed.has(index)) agreed.add(statements.length);
      statements.push(statement);
    }
  });
  return { statements, confirmed: agreed };
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
