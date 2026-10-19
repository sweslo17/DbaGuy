import type { EditChanges, EditKind } from "@/lib/api/types";
import { inputMatches, NULL_INPUT, sameCell, sameInput, type CellInput } from "./cells";

/*
 * The staged changes of one table-data tab (spec ⑤ §3.6.1, R-05-3) as one pure reducer: `stage(state, action)` returns
 * the next state and never mutates. A loaded row is known by its key as it was loaded (`RowKey`, CR-4), never by its
 * position, so sorting, filtering, Load more, Refresh and a failed commit's re-read never lose or misplace a change; a
 * key the result no longer shows keeps its change. New rows are a list of their own (they have no key yet).
 *
 * Every user action is one undo step (D-05-14): `past` keeps the stagings before it (at most UNDO_LIMIT), sharing every
 * change it did not touch, so an untouched change keeps its object (the overlay's cache, and so Q1, rely on it).
 *
 * Column names are looked up as own properties only (`own`): a table may have a column named `constructor` or
 * `__proto__`, which a plain lookup would find on the object's prototype.
 */

/** D-05-11: changes (rows) in one commit, and so staged at once. */
export const MAX_CHANGES = 10_000;
/** D-05-14: undo steps kept (memory only). */
export const UNDO_LIMIT = 100;

/** A loaded row's key cells, as loaded, as JSON in key order. */
export type RowKey = string;
type Cells = Readonly<Record<string, unknown>>;

/** An UPDATE: the key and each set column's cell as loaded (`original`), and the values to set. `set` is never empty. */
export interface StagedUpdate {
  readonly id: string;
  readonly key: Cells;
  readonly set: Readonly<Record<string, CellInput>>;
  readonly original: Cells;
  readonly seq: number;
}
/** An INSERT: a column it does not name takes its DEFAULT. */
export interface StagedInsert {
  readonly id: string;
  readonly values: Readonly<Record<string, CellInput>>;
  readonly seq: number;
}
export interface StagedDelete {
  readonly id: string;
  readonly key: Cells;
  readonly seq: number;
}
/** Why the last preview or commit refused a change; `column` null: the change as a whole. */
export interface StagedProblem {
  readonly column: string | null;
  readonly message: string;
}

/** What an undo step restores. */
export interface Staging {
  readonly updates: Readonly<Record<RowKey, StagedUpdate>>;
  /** In the order they were added (the grid shows the newest first). */
  readonly inserts: readonly StagedInsert[];
  readonly deletes: Readonly<Record<RowKey, StagedDelete>>;
}

/**
 * Whether the last apply's outcome is not known (R-05-6, ruling H6). A commit clears it, an unknown outcome makes it
 * "pending", and a rollback or a partial outcome leaves it as it was before that apply (ruling K10):
 * - "applying": an apply is being sent and its outcome has not arrived (pre-flight H1). `workspace.json` keeps it as an
 *   unknown outcome, so a reload, crash or quit before the outcome restores the staging as "pending", never as unsent
 *   (a Commit would send it again, and new rows would be inserted twice);
 * - "pending": the outcome never arrived; Commit is off until fresh rows have been read;
 * - "refreshed": fresh rows were read; the next preview warns that new rows would be inserted again.
 */
export type UnknownOutcome = "applying" | "pending" | "refreshed" | null;

export interface StagedEdits extends Staging {
  /** The key the changes were staged on (the edit target's when the first one was). */
  readonly keyColumns: readonly string[];
  /** By change id. */
  readonly problems: Readonly<Record<string, readonly StagedProblem[]>>;
  readonly unknownOutcome: UnknownOutcome;
  /**
   * The `unknownOutcome` the apply in flight replaced (ruling K10), which a rollback or a partial outcome puts back: a
   * "refreshed" warning stays while the rows it is about are still staged. Null unless "applying". Memory only, never
   * saved: the file keeps "applying" as an unknown outcome, which comes back "pending".
   */
  readonly beforeApplying: Exclude<UnknownOutcome, "applying">;
  /** The next change's `seq`: within each kind, changes are sent in the order they were staged (D-05-3). */
  readonly nextSeq: number;
  readonly past: readonly Staging[];
  readonly future: readonly Staging[];
}

export type LoadedRowRef = { readonly kind: "row"; readonly rowKey: RowKey; readonly key: Cells };
export type NewRowRef = { readonly kind: "insert"; readonly id: string };
/** A row of the grid as staging knows it. */
export type RowRef = LoadedRowRef | NewRowRef;

/** One cell; `original` is a loaded row's cell as loaded (what an UPDATE checks), unused for a new row. */
export interface CellTarget {
  readonly row: RowRef;
  readonly column: string;
  readonly original?: unknown;
}

/** An apply's outcome as staging takes it (from `edit_outcome` and the request's error). */
export interface AppliedOutcome {
  readonly outcome: "committed" | "rolled_back" | "partial" | "unknown";
  /** The change ids that took effect. */
  readonly applied: readonly string[];
  readonly failedChange: string | null;
  /** The failed change's message (the server's text), shown on its row. */
  readonly message: string | null;
}

export type StageAction =
  /** `kind`: the column's (`inputMatches`): typed `yes` over a loaded true, or hex in another case, changes nothing. */
  | { type: "edit"; cell: CellTarget; input: CellInput; kind?: EditKind }
  | { type: "setNull"; cells: readonly CellTarget[] }
  | { type: "useDefault"; cells: readonly CellTarget[] }
  | { type: "revertCells"; cells: readonly CellTarget[] }
  | { type: "revertRows"; rows: readonly RowRef[] }
  /** Marks the rows for deletion (a new row goes at once); rows that are all marked already are restored. */
  | { type: "deleteRows"; rows: readonly RowRef[] }
  | { type: "restoreRows"; rows: readonly RowRef[] }
  | { type: "addRow"; id: string }
  | { type: "discardAll" }
  /**
   * The row's set columns are checked against `current` (the server's cells now) instead of what was loaded; `kinds`
   * (the columns', by name) as for `edit`.
   */
  | { type: "keepMine"; row: LoadedRowRef; current: Cells; kinds?: ReadonlyMap<string, EditKind> }
  | { type: "setProblems"; problems: Readonly<Record<string, readonly StagedProblem[]>> }
  /**
   * The apply is about to be sent (pre-flight H1): its outcome is unknown until `applyOutcome`. Not an undo step. A
   * second one while applying changes nothing (the first remembers what it replaced).
   */
  | { type: "applying" }
  /** Every settled apply ends with this one action, a command that never left included (`NOT_SENT_OUTCOME`). */
  | { type: "applyOutcome"; outcome: AppliedOutcome }
  /** Fresh rows were read: an unknown outcome is "refreshed" now. */
  | { type: "refreshed" }
  | { type: "undo" }
  | { type: "redo" };

/** Makes a change id: "u:", "i:" or "d:" and a random part (the backend echoes it, never reads it). */
export type NewId = (prefix: "u" | "i" | "d") => string;
export const newChangeId: NewId = (prefix) => `${prefix}:${crypto.randomUUID()}`;

const DEFAULT_INPUT: CellInput = { kind: "default" };

/** A record's own entry for `key`, never one its prototype has (a column may be named `constructor` or `__proto__`). */
export function own<T>(record: Readonly<Record<string, T>>, key: string): T | undefined {
  return Object.hasOwn(record, key) ? record[key] : undefined;
}

/** The RowKey of a key given by column (a staged change's, or one read back from `workspace.json`). */
export function rowKeyFromKey(key: Cells, keyColumns: readonly string[]): RowKey {
  return JSON.stringify(keyColumns.map((column) => own(key, column) ?? null));
}

export function emptyStaged(keyColumns: readonly string[]): StagedEdits {
  return {
    keyColumns,
    updates: {},
    inserts: [],
    deletes: {},
    problems: {},
    unknownOutcome: null,
    beforeApplying: null,
    nextSeq: 0,
    past: [],
    future: [],
  };
}

/** The changes a commit sends: an UPDATE hidden under its row's DELETE is not one (05a carry: only the DELETE is sent). */
export function changeCount(staged: Staging): number {
  let updates = 0;
  for (const rowKey in staged.updates) if (!(rowKey in staged.deletes)) updates += 1;
  return updates + staged.inserts.length + Object.keys(staged.deletes).length;
}

export function hasChanges(staged: Staging): boolean {
  return staged.inserts.length > 0 || Object.keys(staged.updates).length > 0 || Object.keys(staged.deletes).length > 0;
}

/** The changes in the order they were staged (`seq`), as a new array. */
export function bySeq<T extends { readonly seq: number }>(items: Iterable<T>): T[] {
  return [...items].sort((a, b) => a.seq - b.seq);
}

/** The staging part of a state: what an undo step keeps, and what a preview was made from. */
export function stagingOf(state: Staging): Staging {
  return { updates: state.updates, inserts: state.inserts, deletes: state.deletes };
}

/** Whether Delete over `rows` restores them: every one is a loaded row marked for deletion already (spec §3.6.4). */
export function restoresRows(staged: Staging, rows: readonly RowRef[]): boolean {
  return rows.length > 0 && rows.every((row) => row.kind === "row" && row.rowKey in staged.deletes);
}

function pick(cells: Cells, columns: readonly string[]): Record<string, unknown> {
  return Object.fromEntries(columns.map((column) => [column, own(cells, column) ?? null]));
}

/**
 * What `POST /edits/preview` takes (spec §3.3.1), each kind in staging order. An UPDATE hidden under its row's DELETE
 * is left out: the DELETE alone is sent (05a carry). Every UPDATE carries an original per set column and never a
 * DEFAULT (staging never sets one on a loaded row).
 */
export function toChanges(staged: Staging): EditChanges {
  const updates = bySeq(
    Object.entries(staged.updates)
      .filter(([rowKey]) => !(rowKey in staged.deletes))
      .map(([, u]) => u),
  );
  return {
    updates: updates.map((u) => ({
      id: u.id,
      key: { ...u.key },
      set: { ...u.set },
      original: pick(u.original, Object.keys(u.set)),
    })),
    inserts: bySeq(staged.inserts).map((i) => ({ id: i.id, values: { ...i.values } })),
    deletes: bySeq(Object.values(staged.deletes)).map((d) => ({ id: d.id, key: { ...d.key } })),
  };
}

/** The staged change with this id and the row it is on (null for a new row), or null. */
export function findChange(
  staged: Staging,
  id: string,
):
  | { kind: "update"; rowKey: RowKey; change: StagedUpdate }
  | { kind: "delete"; rowKey: RowKey; change: StagedDelete }
  | { kind: "insert"; rowKey: null; change: StagedInsert }
  | null {
  for (const [rowKey, change] of Object.entries(staged.updates))
    if (change.id === id) return { kind: "update", rowKey, change };
  for (const [rowKey, change] of Object.entries(staged.deletes))
    if (change.id === id) return { kind: "delete", rowKey, change };
  const insert = staged.inserts.find((change) => change.id === id);
  return insert ? { kind: "insert", rowKey: null, change: insert } : null;
}

const isEmpty = (record: object): boolean => Object.keys(record).length === 0;

/**
 * `record` without the entries named in `keys`, copied once however many go (M1: dropping the problems of 10,000
 * changes stays linear), or `record` itself when none of them is in it. This is the one place a record is copied to
 * take entries out; the copy holds own entries whatever their names (`fromEntries`, never an assignment to `__proto__`).
 */
function withoutAll<T>(record: Readonly<Record<string, T>>, keys: Iterable<string>): Readonly<Record<string, T>> {
  let copy: Record<string, T> | null = null;
  for (const key of keys) {
    if (!Object.hasOwn(record, key)) continue;
    copy ??= Object.fromEntries(Object.entries(record));
    delete copy[key];
  }
  return copy ?? record;
}

const without = <T>(record: Readonly<Record<string, T>>, key: string) => withoutAll(record, [key]);

const withoutColumn = (update: StagedUpdate, column: string): StagedUpdate => ({
  ...update,
  set: without(update.set, column),
  original: without(update.original, column),
});

/** One action's work on copies of the maps (entries are shared until replaced); `edited`: changes whose problems go. */
interface Work {
  updates: Record<RowKey, StagedUpdate>;
  inserts: StagedInsert[];
  deletes: Record<RowKey, StagedDelete>;
  seq: number;
  changed: boolean;
  edited: Set<string>;
  newId: NewId;
}

function begin(state: StagedEdits, newId: NewId): Work {
  return {
    updates: { ...state.updates },
    inserts: [...state.inserts],
    deletes: { ...state.deletes },
    seq: state.nextSeq,
    changed: false,
    edited: new Set(),
    newId,
  };
}

/** The state after `work` as one undo step; the state itself when nothing changed. */
function finish(state: StagedEdits, work: Work): StagedEdits {
  if (!work.changed) return state;
  return {
    ...state,
    updates: work.updates,
    inserts: work.inserts,
    deletes: work.deletes,
    nextSeq: work.seq,
    problems: withoutAll(state.problems, work.edited),
    past: [...state.past, stagingOf(state)].slice(-UNDO_LIMIT),
    future: [],
  };
}

function setUpdate(work: Work, rowKey: RowKey, update: StagedUpdate | null, id: string): void {
  if (update && Object.keys(update.set).length > 0) work.updates[rowKey] = update;
  else delete work.updates[rowKey];
  work.changed = true;
  work.edited.add(id);
}

function editLoaded(
  work: Work,
  row: LoadedRowRef,
  column: string,
  input: CellInput,
  loaded: unknown,
  kind: EditKind | undefined,
): void {
  if (row.rowKey in work.deletes) return; // a row marked for deletion is not edited (restore it first)
  const existing = work.updates[row.rowKey];
  const staged = existing !== undefined && Object.hasOwn(existing.set, column);
  const original = staged ? own(existing.original, column) : loaded;
  if (inputMatches(input, original, kind)) {
    // Back to the value it was loaded with: no change to the cell (there is no no-op UPDATE).
    if (staged) setUpdate(work, row.rowKey, withoutColumn(existing, column), existing.id);
    return;
  }
  if (staged && sameInput(existing.set[column], input)) return;
  const id = existing?.id ?? work.newId("u");
  const update: StagedUpdate = {
    id,
    key: existing?.key ?? row.key,
    set: { ...existing?.set, [column]: input },
    original: { ...existing?.original, [column]: original === undefined ? null : original },
    seq: existing?.seq ?? work.seq++,
  };
  setUpdate(work, row.rowKey, update, id);
}

function editInsert(work: Work, id: string, column: string, input: CellInput): void {
  const index = work.inserts.findIndex((insert) => insert.id === id);
  if (index < 0) return;
  const insert = work.inserts[index];
  const current = own(insert.values, column);
  if (input.kind === "default" ? current === undefined : current !== undefined && sameInput(current, input)) return;
  const values = input.kind === "default" ? without(insert.values, column) : { ...insert.values, [column]: input };
  work.inserts[index] = { ...insert, values };
  work.changed = true;
  work.edited.add(id);
}

function editCell(work: Work, cell: CellTarget, input: CellInput, kind?: EditKind): void {
  if (cell.row.kind === "insert") editInsert(work, cell.row.id, cell.column, input);
  else if (input.kind !== "default") editLoaded(work, cell.row, cell.column, input, cell.original, kind);
}

function revertCell(work: Work, cell: CellTarget): void {
  if (cell.row.kind === "insert") {
    editInsert(work, cell.row.id, cell.column, DEFAULT_INPUT);
    return;
  }
  if (cell.row.rowKey in work.deletes) return; // a marked row's UPDATE is hidden, kept as it is until Restore
  const existing = work.updates[cell.row.rowKey];
  if (!existing || !Object.hasOwn(existing.set, cell.column)) return;
  setUpdate(work, cell.row.rowKey, withoutColumn(existing, cell.column), existing.id);
}

function removeInsert(work: Work, id: string): void {
  const index = work.inserts.findIndex((insert) => insert.id === id);
  if (index < 0) return;
  work.inserts.splice(index, 1);
  work.changed = true;
  work.edited.add(id); // its problems go with it (an undo brings the row back without them)
}

function revertRow(work: Work, row: RowRef): void {
  if (row.kind === "insert") {
    removeInsert(work, row.id); // a new row reverts to not existing (ruling H20)
    return;
  }
  const update = work.updates[row.rowKey];
  const remove = work.deletes[row.rowKey];
  if (update) setUpdate(work, row.rowKey, null, update.id);
  if (remove) {
    delete work.deletes[row.rowKey];
    work.changed = true;
    work.edited.add(remove.id);
  }
}

function deleteRows(work: Work, rows: readonly RowRef[]): void {
  if (restoresRows(work, rows)) {
    restoreRows(work, rows);
    return;
  }
  for (const row of rows) {
    if (row.kind === "insert") removeInsert(work, row.id);
    else if (!(row.rowKey in work.deletes)) {
      const hidden = work.updates[row.rowKey];
      if (hidden) work.edited.add(hidden.id); // not sent while the row is marked: the last preview's problems go
      work.deletes[row.rowKey] = { id: work.newId("d"), key: row.key, seq: work.seq++ };
      work.changed = true;
    }
  }
}

function restoreRows(work: Work, rows: readonly RowRef[]): void {
  for (const row of rows) {
    if (row.kind !== "row" || !(row.rowKey in work.deletes)) continue;
    work.edited.add(work.deletes[row.rowKey].id);
    delete work.deletes[row.rowKey]; // its hidden UPDATE, if any, shows again
    work.changed = true;
  }
}

function keepMine(
  work: Work,
  row: LoadedRowRef,
  current: Cells,
  kinds: ReadonlyMap<string, EditKind> | undefined,
): void {
  const existing = work.updates[row.rowKey];
  if (!existing) return;
  const set: [string, CellInput][] = [];
  const original: [string, unknown][] = [];
  let moved = false;
  for (const [column, input] of Object.entries(existing.set)) {
    const was = own(existing.original, column) ?? null;
    if (!Object.hasOwn(current, column) || sameCell(current[column], was)) {
      set.push([column, input]);
      original.push([column, was]);
      continue;
    }
    moved = true;
    // the server holds the staged value now: nothing to change
    if (inputMatches(input, current[column], kinds?.get(column))) continue;
    set.push([column, input]);
    original.push([column, current[column] ?? null]);
  }
  if (!moved) return;
  // `fromEntries` makes own entries whatever the column's name (an assignment to `__proto__` would not).
  const rebased = { ...existing, set: Object.fromEntries(set), original: Object.fromEntries(original) };
  setUpdate(work, row.rowKey, rebased, existing.id);
}

/**
 * The unknown outcome after an apply's (ruling K10): an unknown one waits for fresh rows ("pending"); a commit leaves
 * nothing to warn about; a rollback or a partial outcome (a command that never left included) leaves the rest staged as
 * it was, and so its warning: the one the apply in flight replaced, or the current one when none was in flight.
 */
function outcomeAfter(state: StagedEdits, outcome: AppliedOutcome["outcome"]): UnknownOutcome {
  if (outcome === "unknown") return "pending";
  if (outcome === "committed") return null;
  return state.unknownOutcome === "applying" ? state.beforeApplying : state.unknownOutcome;
}

function applyOutcome(state: StagedEdits, outcome: AppliedOutcome): StagedEdits {
  const applied = new Set(outcome.applied);
  const updates = { ...state.updates };
  const deletes = { ...state.deletes };
  let removed = false;
  for (const [rowKey, update] of Object.entries(state.updates)) {
    if (applied.has(update.id)) {
      delete updates[rowKey];
      removed = true;
    }
  }
  for (const [rowKey, remove] of Object.entries(state.deletes)) {
    if (applied.has(remove.id)) {
      delete deletes[rowKey];
      delete updates[rowKey]; // the row is gone: the UPDATE hidden under its DELETE (never sent) goes with it
      removed = true;
    }
  }
  const inserts = state.inserts.filter((insert) => !applied.has(insert.id));
  removed ||= inserts.length !== state.inserts.length;
  const { failedChange, message } = outcome;
  const failed = failedChange && message ? { [failedChange]: [{ column: null, message }] } : null;
  const unknownOutcome = outcomeAfter(state, outcome.outcome);
  // Removed changes must never come back through undo (they took effect), and a commit empties the history (D-05-14).
  const hasHistory = state.past.length > 0 || state.future.length > 0;
  const clearHistory = (removed || outcome.outcome === "committed") && hasHistory;
  const sameProblems = !failed && isEmpty(state.problems);
  const same = unknownOutcome === state.unknownOutcome && state.beforeApplying === null;
  if (!removed && !clearHistory && sameProblems && same) return state;
  return {
    ...state,
    ...(removed ? { updates, inserts, deletes } : {}),
    problems: sameProblems ? state.problems : (failed ?? {}),
    unknownOutcome,
    beforeApplying: null,
    past: clearHistory ? [] : state.past,
    future: clearHistory ? [] : state.future,
  };
}

/** The next state; `state` itself when the action changes nothing (a dispatcher then stores nothing). */
export function stage(state: StagedEdits, action: StageAction, newId: NewId = newChangeId): StagedEdits {
  switch (action.type) {
    case "undo": {
      const previous = state.past.at(-1);
      if (!previous) return state;
      return { ...state, ...previous, past: state.past.slice(0, -1), future: [stagingOf(state), ...state.future] };
    }
    case "redo": {
      const next = state.future[0];
      if (!next) return state;
      return {
        ...state,
        ...next,
        past: [...state.past, stagingOf(state)].slice(-UNDO_LIMIT),
        future: state.future.slice(1),
      };
    }
    case "setProblems": {
      // A clean preview sends `{}` every time: no new state, so no render and no save, when nothing was there either.
      const same = action.problems === state.problems || (isEmpty(action.problems) && isEmpty(state.problems));
      return same ? state : { ...state, problems: action.problems };
    }
    case "applying":
      if (state.unknownOutcome === "applying") return state; // the first one remembers what it replaced
      return { ...state, unknownOutcome: "applying", beforeApplying: state.unknownOutcome };
    case "applyOutcome":
      return applyOutcome(state, action.outcome);
    case "refreshed":
      return state.unknownOutcome === "pending" ? { ...state, unknownOutcome: "refreshed" } : state;
    case "discardAll": {
      if (!hasChanges(state)) return state;
      return {
        ...state,
        updates: {},
        inserts: [],
        deletes: {},
        problems: {},
        past: [...state.past, stagingOf(state)].slice(-UNDO_LIMIT),
        future: [],
      };
    }
    default:
      break;
  }
  const work = begin(state, newId);
  switch (action.type) {
    case "edit":
      editCell(work, action.cell, action.input, action.kind);
      break;
    case "setNull":
      for (const cell of action.cells) editCell(work, cell, NULL_INPUT);
      break;
    case "useDefault":
      for (const cell of action.cells)
        if (cell.row.kind === "insert") editInsert(work, cell.row.id, cell.column, DEFAULT_INPUT);
      break;
    case "revertCells":
      for (const cell of action.cells) revertCell(work, cell);
      break;
    case "revertRows":
      for (const row of action.rows) revertRow(work, row);
      break;
    case "deleteRows":
      deleteRows(work, action.rows);
      break;
    case "restoreRows":
      restoreRows(work, action.rows);
      break;
    case "addRow":
      if (work.inserts.some((insert) => insert.id === action.id)) break; // a change id is sent once
      work.inserts.push({ id: action.id, values: {}, seq: work.seq++ });
      work.changed = true;
      break;
    case "keepMine":
      keepMine(work, action.row, action.current, action.kinds);
      break;
  }
  return finish(state, work);
}
