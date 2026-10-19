import type { StagedEditsFile } from "@/lib/api/types";
import { characterCount } from "@/lib/unicodeText";
import type { CellInput } from "./cells";
import {
  bySeq,
  emptyStaged,
  MAX_CHANGES,
  rowKeyFromKey,
  type StagedDelete,
  type StagedEdits,
  type StagedInsert,
  type StagedUpdate,
} from "./staging";

/*
 * A table tab's staged edits in `workspace.json` (spec ⑤ §3.7, R-05-11). `PUT /workspace` validates the whole state, so
 * one staged set the backend refuses would fail every save and a crash would lose every tab's unsaved work (Q2, B40):
 * `savedEdits` writes only what the backend's `StagedEditsFile` takes, and a set beyond its limits, or one it would
 * refuse, not at all (the edit bar then says it will not survive a restart, and why). The staging itself stays in
 * memory as it is and can still be committed: only keeping it across a restart is skipped. The undo history and the
 * last preview's problems are never kept.
 *
 * Everything is judged as the backend judges it (`domain/edits.py`): a set the frontend writes must be one the backend
 * keeps, or the repair drops it unseen (pre-flight M3, ruling K5).
 * - Size (`_text_chars`, `_json_chars`): keys and originals count their column names, and a text counts characters
 *   (Python's `len`), not UTF-16 units.
 * - What the model refuses (`StagedEditsFile` and the models it holds): a text that is not valid Unicode (a lone
 *   surrogate; pydantic takes one inside a key or an original too, but the file cannot be written with it, so the
 *   backend refuses it there), a column name of 0 or over 1,024 characters, an id of 0 or over 100 characters or one
 *   used twice, an UPDATE that sets nothing or has no original for a set column, no key columns, and a cell with
 *   lists and objects nested more than 64 deep (`MAX_CELL_LEVELS`; our own cap, far below the depth pydantic reads and
 *   writes: a file with 254 levels was taken, written, and then could not be read back).
 * `fixtures/staged_edits_cases.json` in the backend's tests holds the same files for both sides: `savedEdits.test.ts`
 * and `test_local_stores.py` each show that this keeps exactly what the model takes.
 */

/** D-05-11, as `MAX_VALUE_CHARS` in `domain/edits.py`: one staged value. */
export const MAX_SAVED_VALUE_CHARS = 1_048_576;
/** D-05-11, as `MAX_STAGED_CHARS`: all of one tab's staged text (values, keys and originals). */
export const MAX_SAVED_CHARS = 8 * 1_048_576;
/** `ColumnName` in `domain/edits.py`: a column name has 1 to this many characters. */
const MAX_NAME_CHARS = 1_024;
/** `ChangeId`: an id has 1 to this many characters. */
const MAX_ID_CHARS = 100;
/** `MAX_CELL_LEVELS` in `domain/edits.py`: how many lists and objects may hold one another in a key or an original. */
const MAX_CELL_LEVELS = 64;

/**
 * Why staged edits cannot be kept across a restart though they are within the limits: "value" a value, key or original
 * holds a character that cannot be saved, "name" a column name cannot be, "other" anything else the backend's model
 * refuses (an id, a cell nested too deep, …).
 */
export type Unsaveable = "value" | "name" | "other";
/** Why nothing is kept for a tab: `savedEdits` gives it as `tooLarge` or `unsaveable`. */
type NotKept = "too_large" | Unsaveable;

/** `tooLarge`: beyond a limit. `unsaveable`: the backend would refuse them (set only when `tooLarge` is false). */
export type SavedEdits =
  | { file: StagedEditsFile; tooLarge: false; unsaveable: null }
  | { file: null; tooLarge: boolean; unsaveable: Unsaveable | null };

/** What the edit bar says when `unsaveable` is set (for `tooLarge` it says "Too large to keep across a restart"). */
export const UNSAVEABLE_HINT: Readonly<Record<Unsaveable, string>> = {
  value: "Can't keep across a restart: a value holds a character that cannot be saved",
  name: "Can't keep across a restart: a column name that cannot be saved",
  other: "Can't keep across a restart: these changes cannot be saved",
};

const NOTHING: SavedEdits = { file: null, tooLarge: false, unsaveable: null };
const NOT_KEPT: Readonly<Record<NotKept, SavedEdits>> = {
  too_large: { file: null, tooLarge: true, unsaveable: null },
  value: { file: null, tooLarge: false, unsaveable: "value" },
  name: { file: null, tooLarge: false, unsaveable: "name" },
  other: { file: null, tooLarge: false, unsaveable: "other" },
};

/** Whether `name` is one the backend takes for a column (`ColumnName`). */
function isColumnName(name: string): boolean {
  const length = characterCount(name);
  return length !== null && length >= 1 && length <= MAX_NAME_CHARS;
}

const hasColumnNames = (cells: Readonly<Record<string, unknown>>) => Object.keys(cells).every(isColumnName);

/**
 * The text a key or original holds as the backend counts it (`_json_chars`): each string's length, an object's names
 * too, 1 for any other scalar; or why it cannot be kept. Iterative, like the backend's, so a deep cell is measured,
 * never a stack overflow.
 */
function jsonChars(cell: unknown): number | NotKept {
  let total = 0;
  // `enclosing`: how many lists and objects hold the item (the key or original itself is no level: -1)
  const pending: [unknown, number][] = [[cell, -1]];
  for (let next = pending.pop(); next; next = pending.pop()) {
    const [item, enclosing] = next;
    if (typeof item === "object" && item !== null && enclosing >= MAX_CELL_LEVELS) return "other";
    if (typeof item === "string") {
      const length = characterCount(item);
      if (length === null) return "value";
      total += length;
    } else if (Array.isArray(item)) {
      for (const element of item) pending.push([element, enclosing + 1]); // not a spread: a long array has no argument limit
    } else if (item !== null && typeof item === "object") {
      for (const [name, value] of Object.entries(item)) {
        const length = characterCount(name);
        if (length === null) return "value";
        total += length;
        pending.push([value, enclosing + 1]);
      }
    } else total += 1;
  }
  return total;
}

/** The text of a change's staged values, or why they cannot be kept (a value is over its limit, or not valid text). */
function valuesChars(values: Readonly<Record<string, CellInput>>): number | NotKept {
  let total = 0;
  for (const input of Object.values(values)) {
    if (input.kind !== "value") continue;
    const length = characterCount(input.text);
    if (length === null) return "value";
    if (length > MAX_SAVED_VALUE_CHARS) return "too_large";
    total += length;
  }
  return total;
}

const cache = new WeakMap<StagedEdits, SavedEdits>();

/** What `workspace.json` keeps of a tab's staged edits; memoized per state (the writer asks on every save). */
export function savedEdits(staged: StagedEdits | null): SavedEdits {
  if (!staged) return NOTHING;
  const known = cache.get(staged);
  if (known) return known;
  const result = build(staged);
  cache.set(staged, result);
  return result;
}

type Changes = { updates: StagedUpdate[]; inserts: StagedInsert[]; deletes: StagedDelete[] };

/** The text all the changes hold, or why they cannot be kept: the first thing the backend's model would refuse. */
function measure(keyColumns: readonly string[], { updates, inserts, deletes }: Changes): number | NotKept {
  if (keyColumns.length === 0) return "other";
  if (!keyColumns.every(isColumnName)) return "name";
  const ids = new Set<string>();
  const hasId = (id: string) => {
    const length = characterCount(id);
    if (length === null || length < 1 || length > MAX_ID_CHARS || ids.has(id)) return false;
    ids.add(id);
    return true;
  };
  let total = 0;
  const count = (measured: number | NotKept): NotKept | null => {
    if (typeof measured === "string") return measured;
    total += measured;
    return null;
  };
  for (const update of updates) {
    const columns = Object.keys(update.set);
    if (!hasId(update.id) || columns.length === 0) return "other";
    if (!hasColumnNames(update.set) || !hasColumnNames(update.key) || !hasColumnNames(update.original)) return "name";
    // `undefined` is not written (JSON leaves it out), so it is no original either
    if (columns.some((column) => !Object.hasOwn(update.original, column) || update.original[column] === undefined))
      return "other";
    const refused = count(valuesChars(update.set)) ?? count(jsonChars(update.key)) ?? count(jsonChars(update.original));
    if (refused) return refused;
  }
  for (const insert of inserts) {
    if (!hasId(insert.id)) return "other";
    if (!hasColumnNames(insert.values)) return "name";
    const refused = count(valuesChars(insert.values));
    if (refused) return refused;
  }
  for (const remove of deletes) {
    if (!hasId(remove.id)) return "other";
    if (!hasColumnNames(remove.key)) return "name";
    const refused = count(jsonChars(remove.key));
    if (refused) return refused;
  }
  return total;
}

function build(staged: StagedEdits): SavedEdits {
  const updates = bySeq(Object.values(staged.updates));
  const inserts = bySeq(staged.inserts);
  const deletes = bySeq(Object.values(staged.deletes));
  // Every entry counts, an UPDATE under its row's DELETE included (ruling K3: it is kept so Restore brings it back).
  const entries = updates.length + inserts.length + deletes.length;
  if (entries === 0 && staged.unknownOutcome === null) return NOTHING;
  if (entries > MAX_CHANGES) return NOT_KEPT.too_large;
  const total = measure(staged.keyColumns, { updates, inserts, deletes });
  if (typeof total === "string") return NOT_KEPT[total];
  if (total > MAX_SAVED_CHARS) return NOT_KEPT.too_large;
  return {
    tooLarge: false,
    unsaveable: null,
    file: {
      version: 1,
      key_columns: [...staged.keyColumns],
      updates: updates.map((u) => ({ id: u.id, key: { ...u.key }, set: { ...u.set }, original: { ...u.original } })),
      inserts: inserts.map((i) => ({ id: i.id, values: { ...i.values } })),
      deletes: deletes.map((d) => ({ id: d.id, key: { ...d.key } })),
      outcome_unknown: staged.unknownOutcome !== null,
    },
  };
}

/** The backend's cell as a staged value. */
function inputOf(cell: { kind: "value" | "null" | "default"; text?: string | null }): CellInput {
  return cell.kind === "value" ? { kind: "value", text: cell.text ?? "" } : { kind: cell.kind };
}

const inputs = (cells: Readonly<Record<string, { kind: "value" | "null" | "default"; text?: string | null }>> = {}) =>
  Object.fromEntries(Object.entries(cells).map(([column, cell]) => [column, inputOf(cell)]));

/**
 * Staged edits read back from `workspace.json`: bound again to their rows by key once the Data view has rows. An
 * unknown outcome (an apply that was in flight when the app went away included) comes back "pending": the first rows
 * read count as the refresh it waits for.
 */
export function restoredEdits(file: StagedEditsFile): StagedEdits {
  const keyColumns = file.key_columns;
  let seq = 0;
  const updates: Record<string, StagedUpdate> = {};
  for (const u of file.updates ?? []) {
    const rowKey = rowKeyFromKey(u.key, keyColumns);
    if (!(rowKey in updates))
      updates[rowKey] = { id: u.id, key: u.key, set: inputs(u.set), original: u.original, seq: seq++ };
  }
  const inserts = (file.inserts ?? []).map((i) => ({ id: i.id, values: inputs(i.values), seq: seq++ }));
  const deletes: Record<string, StagedDelete> = {};
  for (const d of file.deletes ?? []) {
    const rowKey = rowKeyFromKey(d.key, keyColumns);
    if (!(rowKey in deletes)) deletes[rowKey] = { id: d.id, key: d.key, seq: seq++ };
  }
  return {
    ...emptyStaged(keyColumns),
    updates,
    inserts,
    deletes,
    unknownOutcome: file.outcome_unknown ? "pending" : null,
    nextSeq: seq,
  };
}

/**
 * `restoredEdits` of a set that is read from a local copy, or null when it does not read as staged edits (a field
 * missing, the wrong type). `workspace.json` is validated by the backend, but the local copy is only checked for its
 * tabs, and one malformed set must cost that tab its staged edits, never the restore of every tab (Q2, B40). It is also
 * measured as the writer will measure it (`savedEdits`, which then answers from its cache), so a set that would throw
 * on every save is caught here. The warning names the kind of error and nothing of the set: never a value.
 */
export function readableEdits(file: StagedEditsFile): StagedEdits | null {
  try {
    const edits = restoredEdits(file);
    savedEdits(edits);
    return edits;
  } catch (error) {
    console.warn(
      `Dropped a tab's staged edits: they do not read as staged edits (${error instanceof Error ? error.name : "unknown error"})`,
    );
    return null;
  }
}
