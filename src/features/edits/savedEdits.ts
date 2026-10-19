import type { StagedEditsFile } from "@/lib/api/types";
import type { CellInput } from "./cells";
import {
  bySeq,
  emptyStaged,
  MAX_CHANGES,
  rowKeyFromKey,
  type StagedDelete,
  type StagedEdits,
  type StagedUpdate,
} from "./staging";

/*
 * A table tab's staged edits in `workspace.json` (spec ⑤ §3.7, R-05-11). `PUT /workspace` validates the whole state, so
 * one staged set the backend refuses would fail every save and a crash would lose every tab's unsaved work (Q2, B40):
 * `savedEdits` writes only what the backend's `StagedEditsFile` takes, and a set beyond its limits not at all (the edit
 * bar then says it will not survive a restart). The undo history and the last preview's problems are never kept.
 *
 * The size is counted exactly as the backend counts it (`_text_chars`, `_json_chars` in `domain/edits.py`): a set the
 * frontend measures under the limit must be one the backend keeps, or the repair drops it unseen (pre-flight M3). So:
 * keys and originals count their column names, and a text counts characters (Python's `len`), not UTF-16 units.
 */

/** D-05-11, as `MAX_VALUE_CHARS` in `domain/edits.py`: one staged value. */
export const MAX_SAVED_VALUE_CHARS = 1_048_576;
/** D-05-11, as `MAX_STAGED_CHARS`: all of one tab's staged text (values, keys and originals). */
export const MAX_SAVED_CHARS = 8 * 1_048_576;

export type SavedEdits = { file: StagedEditsFile; tooLarge: false } | { file: null; tooLarge: boolean };

const NOTHING: SavedEdits = { file: null, tooLarge: false };
const TOO_LARGE: SavedEdits = { file: null, tooLarge: true };

/** A string's length in characters, as Python's `len` (the backend's) gives it: a surrogate pair is one, not two. */
function textLength(text: string): number {
  if (!/[\ud800-\udbff]/.test(text)) return text.length; // the usual case, scanned natively
  let length = text.length;
  for (let index = 0; index < text.length - 1; index += 1) {
    const unit = text.charCodeAt(index);
    const next = text.charCodeAt(index + 1);
    if (unit >= 0xd800 && unit <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) {
      length -= 1;
      index += 1;
    }
  }
  return length;
}

/**
 * The text a key or original holds as the backend counts it (`_json_chars`): each string's length, an object's names
 * too, 1 for any other scalar. Iterative, like the backend's, so a deep cell is measured, never a stack overflow.
 */
function jsonChars(cell: unknown): number {
  let total = 0;
  const pending = [cell];
  while (pending.length) {
    const item = pending.pop();
    if (typeof item === "string") total += textLength(item);
    else if (Array.isArray(item)) {
      for (const element of item) pending.push(element); // not a spread: a long array has no argument limit
    } else if (item !== null && typeof item === "object") {
      for (const [name, value] of Object.entries(item)) {
        total += textLength(name);
        pending.push(value);
      }
    } else total += 1;
  }
  return total;
}

/** The text of a change's staged values, or null when one of them is over a value's limit. */
function valuesChars(values: Readonly<Record<string, CellInput>>): number | null {
  let total = 0;
  for (const input of Object.values(values)) {
    if (input.kind !== "value") continue;
    const length = textLength(input.text);
    if (length > MAX_SAVED_VALUE_CHARS) return null;
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

function build(staged: StagedEdits): SavedEdits {
  const updates = bySeq(Object.values(staged.updates));
  const inserts = bySeq(staged.inserts);
  const deletes = bySeq(Object.values(staged.deletes));
  // Every entry counts, an UPDATE under its row's DELETE included (ruling K3: it is kept so Restore brings it back).
  const entries = updates.length + inserts.length + deletes.length;
  if (entries === 0 && staged.unknownOutcome === null) return NOTHING;
  if (entries > MAX_CHANGES || staged.keyColumns.length === 0) return TOO_LARGE;
  let total = 0;
  for (const change of [...updates, ...inserts]) {
    const values = valuesChars("set" in change ? change.set : change.values);
    if (values === null) return TOO_LARGE;
    total += values;
  }
  for (const update of updates) total += jsonChars(update.key) + jsonChars(update.original);
  for (const entry of deletes) total += jsonChars(entry.key);
  if (total > MAX_SAVED_CHARS) return TOO_LARGE;
  return {
    tooLarge: false,
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
