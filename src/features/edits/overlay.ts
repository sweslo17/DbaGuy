import type { ColumnCategory, ColumnMeta, EditKind } from "@/lib/api/types";
import { describeCell } from "@/lib/cells";
import type { CellMark, RowMark } from "../grid/marks";
import { sameElements } from "../grid/useHeldResult";
import { displayCell, sameCell } from "./cells";
import {
  hasChanges,
  own,
  rowKeyFromKey,
  type RowKey,
  type RowRef,
  type StagedDelete,
  type StagedEdits,
  type StagedInsert,
  type StagedProblem,
  type StagedUpdate,
} from "./staging";

/*
 * The staged changes laid over the loaded rows for the grid (spec ⑤ §3.6.2, R-05-3), as pure functions. The result is
 * never copied: the display rows are a new array of row references in which only a changed row is a new array (its
 * cells otherwise shared), every other row is the loaded row object itself, so the grid's memoized rows skip it (Q1).
 * An overlay also reuses, from the last one (`OverlayCache`), each changed row whose source row, change and problems
 * are the same objects: staging one cell re-renders that row alone.
 *
 * Binding (`bindRows`) maps each loaded row's key to its index, only while something is staged on loaded rows; Load
 * more appends a page, and the binding then keys only the new page. Rows that share a key cannot be told apart: no
 * staged change is drawn on them and they are marked not editable (`REPEATED_KEY_MARK`).
 *
 * Column names are looked up as own properties only (`own`): a table may have a column named `constructor` or
 * `__proto__`, which a plain lookup would find on the object's prototype.
 */

/*
 * A loaded row's RowKey is `rowKeyFromKey` over the row itself, so it is the very text of its staged change's: the row
 * is read as a key by column whose column names are its key cells' indexes (an array's own entries are "0", "1", …).
 */
const cellNames = (keyIndexes: readonly number[]) => keyIndexes.map(String);
const asCells = (row: readonly unknown[]) => row as unknown as Readonly<Record<string, unknown>>;

/** A loaded row's key cells as JSON in key order: the same text as `rowKeyFromKey` gives for its staged change. */
export function rowKeyOf(row: readonly unknown[], keyIndexes: readonly number[]): RowKey {
  return rowKeyFromKey(asCells(row), cellNames(keyIndexes));
}

/** Where the result's columns are, by name, and its key columns' indexes (null when one is not in the result). */
export interface ResultColumns {
  readonly byName: ReadonlyMap<string, number>;
  readonly keyColumns: readonly string[];
  readonly keyIndexes: readonly number[] | null;
}

export function resultColumns(columns: readonly ColumnMeta[], keyColumns: readonly string[]): ResultColumns {
  const byName = new Map(columns.map((column, index) => [column.name, index]));
  const indexes = keyColumns.map((column) => byName.get(column));
  const keyIndexes =
    keyColumns.length > 0 && indexes.every((index) => index !== undefined) ? (indexes as number[]) : null;
  return { byName, keyColumns, keyIndexes };
}

/** The keys of one result's rows. Its maps are extended in place when the same result grows (see `bindRows`). */
export interface Binding {
  /** The rows bound: the very array `bindRows` was given (`overlay()` takes no binding of other rows). */
  readonly rows: readonly (readonly unknown[])[];
  readonly keyIndexes: readonly number[];
  /** Each key's row index (the first, if a key repeats). */
  readonly byKey: ReadonlyMap<RowKey, number>;
  /** The keys that more than one row has: no staged change binds to them. */
  readonly repeated: ReadonlySet<RowKey>;
  /** The index of every row whose key repeats, in the order found. */
  readonly repeatedRows: readonly number[];
  /** How many rows this binding keyed: all of a new result, only the new page after Load more. */
  readonly keyed: number;
}

/**
 * The binding of `rows` (the same binding again for the same array). When they are `previous`'s rows with a page
 * appended (the same row objects first: pages are only appended), only the new rows are keyed and `previous`'s maps are
 * extended in place (`previous` must not be used again; `overlay()` guards against an index past its rows).
 */
export function bindRows(
  rows: readonly (readonly unknown[])[],
  keyIndexes: readonly number[],
  previous: Binding | null,
): Binding {
  const sameKey = !!previous && sameElements(previous.keyIndexes, keyIndexes);
  if (sameKey && previous.rows === rows) return previous;
  const old = previous?.rows;
  const grows =
    sameKey &&
    !!old &&
    old.length > 0 &&
    old.length <= rows.length &&
    rows[0] === old[0] &&
    rows[old.length - 1] === old[old.length - 1];
  // The maps are this module's own (made below, typed read-only for everyone else).
  const byKey = grows ? (previous.byKey as Map<RowKey, number>) : new Map<RowKey, number>();
  const repeated = grows ? (previous.repeated as Set<RowKey>) : new Set<RowKey>();
  const repeatedRows = grows ? (previous.repeatedRows as number[]) : [];
  const start = grows ? old.length : 0;
  const names = cellNames(keyIndexes);
  for (let index = start; index < rows.length; index += 1) {
    const key = rowKeyFromKey(asCells(rows[index]), names);
    const first = byKey.get(key);
    if (first === undefined) {
      byKey.set(key, index);
      continue;
    }
    if (!repeated.has(key)) {
      repeated.add(key);
      repeatedRows.push(first);
    }
    repeatedRows.push(index);
  }
  return { rows, keyIndexes, byKey, repeated, repeatedRows, keyed: rows.length - start };
}

/** What the grid draws: new rows first (the newest at the top), then the loaded rows with their changes. */
export interface Overlay {
  readonly rows: unknown[][];
  /** By display index; sparse (undefined: an untouched row). */
  readonly marks: readonly (RowMark | undefined)[];
  /** New rows at the top: the loaded rows' numbers count from after them. */
  readonly leading: number;
  /**
   * Staged changes of loaded rows the rows shown do not have (filtered out, not loaded yet, or deleted meanwhile), or
   * whose key more than one row shown has.
   */
  readonly notShown: number;
}

const NO_MARKS: readonly (RowMark | undefined)[] = [];

/** Every row whose key another loaded row has too: one object, so the rows it is passed to stay memoized (Q1). */
export const REPEATED_KEY_MARK: RowMark = { kind: "repeated_key" };
/** Why such a row cannot be edited (the editor's refusal, a tooltip). */
export const REPEATED_KEY = "Another row in the result has the same key: this row cannot be edited here";

export interface OverlayInput {
  readonly rows: unknown[][];
  readonly columns: readonly ColumnMeta[];
  /** The edit target's kinds by column name (how a staged text is drawn: bool, hex); empty until it is known. */
  readonly kinds: ReadonlyMap<string, EditKind>;
  readonly staged: StagedEdits | null;
  /**
   * The binding of `rows` (`bindRows(rows, …)`), or null (nothing staged on loaded rows, or the key is not in the
   * result). A binding of another array, or of other key columns, is taken as none.
   */
  readonly binding: Binding | null;
  readonly result: ResultColumns;
}

interface LoadedEntry {
  readonly source: readonly unknown[];
  readonly update: StagedUpdate | undefined;
  readonly remove: StagedDelete | undefined;
  readonly problems: readonly StagedProblem[] | undefined;
  readonly row: unknown[];
  readonly mark: RowMark;
}

interface InsertEntry {
  readonly insert: StagedInsert;
  readonly problems: readonly StagedProblem[] | undefined;
  readonly row: unknown[];
  readonly mark: RowMark;
}

/**
 * What the last overlay drew, by change. An entry is drawn from the columns, the kinds and the result it was drawn
 * with, so the whole cache holds only while those are the same objects (the kinds arrive with the edit target, after a
 * restored staging may have been drawn without them).
 */
export interface OverlayCache {
  readonly columns: readonly ColumnMeta[];
  readonly kinds: ReadonlyMap<string, EditKind>;
  readonly result: ResultColumns;
  readonly loaded: ReadonlyMap<RowKey, LoadedEntry>;
  readonly inserts: ReadonlyMap<string, InsertEntry>;
}

const NO_LOADED: ReadonlyMap<RowKey, LoadedEntry> = new Map();
const NO_INSERTS: ReadonlyMap<string, InsertEntry> = new Map();
/** A tooltip's value is cut here. */
const SHOWN_CHARS = 200;

/** A cell's value as a tooltip names it: NULL, "(empty string)", or its text. */
function tooltipValue(cell: unknown, category: ColumnCategory): string {
  const display = describeCell(cell, category, SHOWN_CHARS);
  if (display.kind === "empty") return "(empty string)";
  return `${display.text}${display.clipped ? "…" : ""}${display.detail ?? ""}`;
}

/** "Was: …", a changed cell's tooltip and the value editor's line. */
export const wasText = (cell: unknown, category: ColumnCategory) => `Was: ${tooltipValue(cell, category)}`;

const messages = (problems: readonly StagedProblem[]) => problems.map((problem) => problem.message).join("\n");

function problemFor(problems: readonly StagedProblem[] | undefined, column: string): string | undefined {
  const ofColumn = problems?.filter((problem) => problem.column === column) ?? [];
  return ofColumn.length ? messages(ofColumn) : undefined;
}

/** The problems that are not on a cell the grid shows: the change's own, and those of columns not in the result. */
function rowProblem(problems: readonly StagedProblem[] | undefined, result: ResultColumns): string | undefined {
  const rest = problems?.filter((problem) => problem.column === null || !result.byName.has(problem.column)) ?? [];
  return rest.length ? messages(rest) : undefined;
}

/**
 * Marks the cells of a loaded row's problems that are not on a cell marked already (e.g. `key_incomplete` on a key
 * column the UPDATE does not set, or on a DELETE's key), so every problem of a column in the result is on its cell.
 */
function markProblemCells(
  cells: Record<number, CellMark>,
  problems: readonly StagedProblem[] | undefined,
  result: ResultColumns,
): void {
  for (const problem of problems ?? []) {
    const index = problem.column === null ? undefined : result.byName.get(problem.column);
    if (index === undefined || index in cells) continue;
    cells[index] = { changed: false, problem: problemFor(problems, problem.column!) };
  }
}

function insertEntry(
  insert: StagedInsert,
  problems: readonly StagedProblem[] | undefined,
  input: OverlayInput,
): InsertEntry {
  const cells: Record<number, CellMark> = {};
  const row = input.columns.map((column, index) => {
    const value = own(insert.values, column.name);
    const problem = problemFor(problems, column.name);
    if (!value) cells[index] = { changed: false, isDefault: true, problem };
    else if (problem) cells[index] = { changed: false, problem };
    return value ? displayCell(value, input.kinds.get(column.name)) : null;
  });
  const mark: RowMark = { kind: "new", problem: rowProblem(problems, input.result), cells };
  return { insert, problems, row, mark };
}

function loadedEntry(
  source: readonly unknown[],
  update: StagedUpdate | undefined,
  remove: StagedDelete | undefined,
  problems: readonly StagedProblem[] | undefined,
  input: OverlayInput,
): LoadedEntry {
  const base = { source, update, remove, problems };
  const cells: Record<number, CellMark> = {};
  if (remove || !update) {
    // Marked for deletion: the loaded values, struck through (an UPDATE under it stays hidden until it is restored).
    markProblemCells(cells, problems, input.result);
    const problem = rowProblem(problems, input.result);
    const mark: RowMark =
      Object.keys(cells).length > 0 ? { kind: "deleted", problem, cells } : { kind: "deleted", problem };
    return { ...base, row: source as unknown[], mark };
  }
  const row = source.slice();
  for (const [column, value] of Object.entries(update.set)) {
    const index = input.result.byName.get(column);
    if (index === undefined) continue; // not in the result any more: the next preview says `unknown_column`
    const category = input.columns[index].category;
    const original = own(update.original, column);
    row[index] = displayCell(value, input.kinds.get(column));
    cells[index] = {
      changed: true,
      was: wasText(original, category),
      problem: problemFor(problems, column),
      conflict: sameCell(source[index], original)
        ? undefined
        : `Changed on the server: now ${tooltipValue(source[index], category)}`,
    };
  }
  markProblemCells(cells, problems, input.result);
  return { ...base, row, mark: { kind: "changed", problem: rowProblem(problems, input.result), cells } };
}

/** `input.binding` if it is the binding of `input.rows` on the result's key, else null. */
function bindingOf(input: OverlayInput): Binding | null {
  const { binding, rows, result } = input;
  if (!binding || !result.keyIndexes || binding.rows !== rows) return null;
  return sameElements(binding.keyIndexes, result.keyIndexes) ? binding : null;
}

/** The display rows and marks; `cache` is the previous call's (null the first time). */
export function overlay(input: OverlayInput, cache: OverlayCache | null): { overlay: Overlay; cache: OverlayCache } {
  const { staged, rows, columns, kinds, result } = input;
  const binding = bindingOf(input);
  const repeatedRows = binding?.repeatedRows ?? [];
  if ((!staged || !hasChanges(staged)) && repeatedRows.length === 0) {
    return {
      overlay: { rows, marks: NO_MARKS, leading: 0, notShown: 0 },
      cache: { columns, kinds, result, loaded: NO_LOADED, inserts: NO_INSERTS },
    };
  }
  const reuse = cache && cache.columns === columns && cache.kinds === kinds && cache.result === result ? cache : null;
  const loaded = new Map<RowKey, LoadedEntry>();
  const inserts = new Map<string, InsertEntry>();

  const top: unknown[][] = [];
  const topMarks: RowMark[] = [];
  const staging = staged?.inserts ?? [];
  for (let i = staging.length - 1; i >= 0; i -= 1) {
    const insert = staging[i];
    const problems = own(staged!.problems, insert.id);
    const old = reuse?.inserts.get(insert.id);
    const entry =
      old && old.insert === insert && old.problems === problems ? old : insertEntry(insert, problems, input);
    inserts.set(insert.id, entry);
    top.push(entry.row);
    topMarks.push(entry.mark);
  }

  let body = rows;
  let bodyMarks: (RowMark | undefined)[] | null = null;
  for (const index of repeatedRows) {
    if (index >= rows.length) continue; // from a binding grown in place since (by Load more): not one of these rows
    bodyMarks ??= new Array<RowMark | undefined>(rows.length);
    bodyMarks[index] = REPEATED_KEY_MARK;
  }

  let notShown = 0;
  const touched = new Set([...Object.keys(staged?.updates ?? {}), ...Object.keys(staged?.deletes ?? {})]);
  for (const rowKey of touched) {
    const index = binding && !binding.repeated.has(rowKey) ? binding.byKey.get(rowKey) : undefined;
    if (index === undefined || index >= rows.length) {
      notShown += 1;
      continue;
    }
    const source = rows[index];
    const update = staged!.updates[rowKey];
    const remove = staged!.deletes[rowKey];
    const problems = own(staged!.problems, (remove ?? update)!.id);
    const old = reuse?.loaded.get(rowKey);
    const entry =
      old && old.source === source && old.update === update && old.remove === remove && old.problems === problems
        ? old
        : loadedEntry(source, update, remove, problems, input);
    loaded.set(rowKey, entry);
    if (body === rows) body = rows.slice();
    body[index] = entry.row;
    bodyMarks ??= new Array<RowMark | undefined>(rows.length);
    bodyMarks[index] = entry.mark;
  }

  const leading = top.length;
  const display = leading ? [...top, ...body] : body;
  const marks: readonly (RowMark | undefined)[] = leading
    ? [...topMarks, ...(bodyMarks ?? new Array<RowMark | undefined>(rows.length))]
    : (bodyMarks ?? NO_MARKS);
  return {
    overlay: { rows: display, marks, leading, notShown },
    cache: { columns, kinds, result, loaded, inserts },
  };
}

/** The row at a display index as staging knows it; null past the end or when the key is not in the result. */
export function rowRefAt(
  display: Overlay,
  staged: StagedEdits | null,
  rows: readonly (readonly unknown[])[],
  index: number,
  result: ResultColumns,
): RowRef | null {
  if (index < display.leading) {
    const inserts = staged?.inserts ?? [];
    const insert = inserts[inserts.length - 1 - index];
    return insert ? { kind: "insert", id: insert.id } : null;
  }
  const row = rows[index - display.leading];
  const { keyIndexes, keyColumns } = result;
  if (!row || !keyIndexes) return null;
  // `fromEntries` makes own entries whatever the column's name (an assignment to `__proto__` would not).
  const key = Object.fromEntries(keyColumns.map((column, i) => [column, row[keyIndexes[i]] ?? null]));
  return { kind: "row", rowKey: rowKeyOf(row, keyIndexes), key };
}

/**
 * Where a row is shown now (its display index), or -1. A binding grown in place since `display` was drawn may know rows
 * that `display` does not have yet: those are -1 too, and so is a key that more than one row has.
 */
export function displayIndexOf(
  display: Overlay,
  staged: StagedEdits | null,
  binding: Binding | null,
  ref: RowRef,
): number {
  if (ref.kind === "insert") {
    const inserts = staged?.inserts ?? [];
    const index = inserts.findIndex((insert) => insert.id === ref.id);
    return index < 0 ? -1 : inserts.length - 1 - index;
  }
  const index = binding && !binding.repeated.has(ref.rowKey) ? binding.byKey.get(ref.rowKey) : undefined;
  const shownAt = index === undefined ? -1 : display.leading + index;
  return shownAt < display.rows.length ? shownAt : -1;
}
