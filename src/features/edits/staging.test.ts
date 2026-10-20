import { describe, expect, it, vi } from "vitest";
import {
  changeCount,
  emptyStaged,
  findChange,
  rowKeyFromKey,
  stage,
  toChanges,
  UNDO_CELLS,
  UNDO_LIMIT,
  type AppliedOutcome,
  type LoadedRowRef,
  type NewId,
  type StageAction,
  type StagedEdits,
} from "./staging";
import { NULL_INPUT } from "./cells";
import { NOT_SENT_OUTCOME } from "./commitFlow";

/*
 * The staging reducer (spec ⑤ §3.6.1): changes by key, never by position; one undo step per action; no no-op UPDATE;
 * NULL is not the empty string; the DELETE alone is sent for a key that also has an UPDATE (05a carry).
 */

/** Change ids in order: u:1, i:2, d:3, … */
function counter(): NewId {
  let n = 0;
  return (prefix) => `${prefix}:${++n}`;
}

const row = (id: number): LoadedRowRef => ({ kind: "row", rowKey: rowKeyFromKey({ id }, ["id"]), key: { id } });
const value = (text: string) => ({ kind: "value", text }) as const;

/** Runs the actions from an empty staging of the `id` key. */
function run(...actions: StageAction[]): StagedEdits {
  const newId = counter();
  return actions.reduce((state, action) => stage(state, action, newId), emptyStaged(["id"]));
}

describe("editing a loaded row", () => {
  it("stages the cell with the value it was loaded with, by the row's key", () => {
    const state = run({ type: "edit", cell: { row: row(1), column: "label", original: "a" }, input: value("b") });
    expect(state.updates[row(1).rowKey]).toEqual({
      id: "u:1",
      key: { id: 1 },
      set: { label: value("b") },
      original: { label: "a" },
      seq: 0,
    });
    expect(changeCount(state)).toBe(1);
  });

  it("keeps the first original when the cell is edited again, and one change id per row", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "label", original: "a" }, input: value("b") },
      { type: "edit", cell: { row: row(1), column: "label", original: "b?" }, input: value("c") },
      { type: "edit", cell: { row: row(1), column: "note", original: null }, input: value("n") },
    );
    expect(state.updates[row(1).rowKey]).toMatchObject({
      id: "u:1",
      set: { label: value("c"), note: value("n") },
      original: { label: "a", note: null },
    });
  });

  it("removes the cell when the value typed is the one it was loaded with: there is no no-op UPDATE", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "label", original: "a" }, input: value("b") },
      { type: "edit", cell: { row: row(1), column: "label", original: "b" }, input: value("a") },
    );
    expect(state.updates).toEqual({});
    expect(changeCount(state)).toBe(0);
    expect(run({ type: "edit", cell: { row: row(2), column: "n", original: 42 }, input: value("42") }).updates).toEqual(
      {},
    );
  });

  it("reads the typed value as the column's kind does: `yes` over true and hex in another case change nothing", () => {
    const flag = { row: row(1), column: "flag", original: true };
    expect(run({ type: "edit", cell: flag, input: value("yes"), kind: "bool" }).updates).toEqual({});
    expect(changeCount(run({ type: "edit", cell: flag, input: value("no"), kind: "bool" }))).toBe(1);
    const data = { row: row(1), column: "data", original: { $type: "bytes", size: 4, preview: "deadbeef" } };
    expect(run({ type: "edit", cell: data, input: value("0xDEADBEEF"), kind: "binary" }).updates).toEqual({});
    // back to the loaded value in another word removes the staged cell
    const back = run(
      { type: "edit", cell: flag, input: value("false"), kind: "bool" },
      { type: "edit", cell: flag, input: value("t"), kind: "bool" },
    );
    expect(changeCount(back)).toBe(0);
    // a number keeps its text: `1.0` over 1 is a change
    const amount = { row: row(1), column: "amount", original: 1 };
    expect(changeCount(run({ type: "edit", cell: amount, input: value("1.0"), kind: "number" }))).toBe(1);
  });

  it("tells NULL from the empty string", () => {
    const empty = run({ type: "edit", cell: { row: row(1), column: "note", original: null }, input: value("") });
    expect(empty.updates[row(1).rowKey].set).toEqual({ note: value("") });
    const nulled = run({ type: "setNull", cells: [{ row: row(1), column: "note", original: "" }] });
    expect(nulled.updates[row(1).rowKey].set).toEqual({ note: { kind: "null" } });
    expect(run({ type: "setNull", cells: [{ row: row(1), column: "note", original: null }] }).updates).toEqual({});
  });

  it("keeps a big integer's digits as typed text", () => {
    const state = run({
      type: "edit",
      cell: { row: row(1), column: "big", original: "1" },
      input: value("9007199254740993"),
    });
    expect(toChanges(state).updates?.[0].set).toEqual({ big: value("9007199254740993") });
  });

  it("never edits a cut value from its preview: staging over it always counts as a change", () => {
    const cut = { $type: "text", preview: "abc", size: 70_000, truncated: true };
    const state = run({ type: "edit", cell: { row: row(1), column: "note", original: cut }, input: value("abc") });
    expect(state.updates[row(1).rowKey].original).toEqual({ note: cut });
  });

  it("does not stage DEFAULT on a loaded row, nor edit a row marked for deletion", () => {
    expect(
      run({ type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: { kind: "default" } }).updates,
    ).toEqual({});
    const state = run(
      { type: "deleteRows", rows: [row(1)] },
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") },
    );
    expect(state.updates).toEqual({});
  });

  it("returns the same state for an action that changes nothing", () => {
    const state = run({ type: "edit", cell: { row: row(1), column: "label", original: "a" }, input: value("b") });
    expect(
      stage(state, { type: "edit", cell: { row: row(1), column: "label", original: "a" }, input: value("b") }),
    ).toBe(state);
    expect(stage(state, { type: "revertCells", cells: [{ row: row(2), column: "label" }] })).toBe(state);
  });

  it("stages a column named like an object's built-in property as any other column", () => {
    for (const column of ["constructor", "toString", "__proto__"]) {
      const state = run({ type: "edit", cell: { row: row(1), column, original: "a" }, input: value("b") });
      const update = state.updates[row(1).rowKey];
      expect(Object.keys(update.set)).toEqual([column]);
      expect(JSON.stringify(toChanges(state).updates)).toBe(
        JSON.stringify([{ id: "u:1", key: { id: 1 }, set: { [column]: value("b") }, original: { [column]: "a" } }]),
      );
      // A second column of the row: its loaded value is its original (never the prototype's entry).
      const two = run(
        { type: "edit", cell: { row: row(1), column: "label", original: "x" }, input: value("y") },
        { type: "edit", cell: { row: row(1), column, original: "a" }, input: value("b") },
      );
      expect(two.updates[row(1).rowKey].original).toEqual({ label: "x", [column]: "a" });
      // Typed back to the loaded value: nothing left.
      expect(
        stage(state, { type: "edit", cell: { row: row(1), column, original: "b" }, input: value("a") }).updates,
      ).toEqual({});
      // Re-based on the server's value now, as an own entry (never the object's prototype).
      const kept = stage(state, { type: "keepMine", row: row(1), current: { [column]: "z" } });
      const original = kept.updates[row(1).rowKey].original;
      expect(Object.getPrototypeOf(original)).toBe(Object.prototype);
      expect(Object.hasOwn(original, column) && original[column]).toBe("z");
      // A new row's cell: DEFAULT until a value is staged.
      const added = run({ type: "addRow", id: "i:a" });
      const at = { row: { kind: "insert", id: "i:a" } as const, column };
      expect(stage(added, { type: "useDefault", cells: [at] })).toBe(added);
      expect(Object.keys(stage(added, { type: "edit", cell: at, input: value("x") }).inserts[0].values)).toEqual([
        column,
      ]);
    }
  });
});

describe("new rows", () => {
  it("starts every cell at DEFAULT and stages values, NULL and DEFAULT again", () => {
    const id = "i:new";
    const at = (column: string) => ({ row: { kind: "insert", id } as const, column });
    const state = run(
      { type: "addRow", id },
      { type: "edit", cell: at("label"), input: value("x") },
      { type: "setNull", cells: [at("note")] },
      { type: "edit", cell: at("code"), input: value("c") },
      { type: "useDefault", cells: [at("code")] },
    );
    expect(state.inserts).toEqual([{ id, values: { label: value("x"), note: { kind: "null" } }, seq: 0 }]);
    expect(toChanges(state).inserts).toEqual([{ id, values: { label: value("x"), note: { kind: "null" } } }]);
  });

  it("goes at once when deleted or reverted", () => {
    const fresh = { kind: "insert", id: "i:a" } as const;
    expect(run({ type: "addRow", id: "i:a" }, { type: "deleteRows", rows: [fresh] }).inserts).toEqual([]);
    expect(run({ type: "addRow", id: "i:a" }, { type: "revertRows", rows: [fresh] }).inserts).toEqual([]);
  });

  it("is added once per id: a change id is sent once", () => {
    const state = run({ type: "addRow", id: "i:a" });
    expect(stage(state, { type: "addRow", id: "i:a" })).toBe(state);
  });

  it("reverts a cell to DEFAULT", () => {
    const at = { row: { kind: "insert", id: "i:a" } as const, column: "label" };
    const state = run({ type: "addRow", id: "i:a" }, { type: "edit", cell: at, input: value("x") });
    expect(stage(state, { type: "revertCells", cells: [at] }).inserts).toEqual([{ id: "i:a", values: {}, seq: 0 }]);
  });
});

describe("deleting rows", () => {
  it("marks loaded rows, and restores them when every selected row is marked already", () => {
    const marked = run({ type: "deleteRows", rows: [row(1), row(2)] });
    expect(Object.values(marked.deletes).map((d) => d.key)).toEqual([{ id: 1 }, { id: 2 }]);
    expect(stage(marked, { type: "deleteRows", rows: [row(1), row(2)] }).deletes).toEqual({});
    // Not all marked: the unmarked one is marked too, nothing is restored.
    expect(Object.keys(stage(marked, { type: "deleteRows", rows: [row(2), row(3)] }).deletes)).toHaveLength(3);
  });

  it("keeps a row marked already as it was when more rows are marked", () => {
    const marked = run({ type: "deleteRows", rows: [row(1)] });
    const more = stage(marked, { type: "deleteRows", rows: [row(1), row(2)] });
    expect(more.deletes[row(1).rowKey]).toBe(marked.deletes[row(1).rowKey]);
  });

  it("takes a new row away and keeps the marked rows marked when both are selected", () => {
    const fresh = { kind: "insert", id: "i:a" } as const;
    const state = stage(run({ type: "addRow", id: "i:a" }, { type: "deleteRows", rows: [row(1)] }), {
      type: "deleteRows",
      rows: [fresh, row(1)],
    });
    expect(state.inserts).toEqual([]);
    expect(Object.keys(state.deletes)).toEqual([row(1).rowKey]);
  });

  it("sends only the DELETE for a key that also has an UPDATE, and restoring brings the UPDATE back", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "label", original: "a" }, input: value("b") },
      { type: "deleteRows", rows: [row(1)] },
    );
    expect(changeCount(state)).toBe(1);
    expect(toChanges(state)).toEqual({ updates: [], inserts: [], deletes: [{ id: "d:2", key: { id: 1 } }] });
    const restored = stage(state, { type: "restoreRows", rows: [row(1)] });
    expect(toChanges(restored).updates).toHaveLength(1);
  });

  it("keeps the hidden UPDATE as it is while the row is marked: its cells are neither edited nor reverted", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "label", original: "a" }, input: value("b") },
      { type: "deleteRows", rows: [row(1)] },
    );
    const at = { row: row(1), column: "label", original: "a" };
    expect(stage(state, { type: "revertCells", cells: [at] })).toBe(state);
    expect(stage(state, { type: "setNull", cells: [at] })).toBe(state);
    const restored = stage(state, { type: "restoreRows", rows: [row(1)] });
    expect(restored.updates[row(1).rowKey]).toBe(state.updates[row(1).rowKey]);
  });

  it("removes the hidden UPDATE with its DELETE once the DELETE is committed", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "label", original: "a" }, input: value("b") },
      { type: "deleteRows", rows: [row(1)] },
    );
    const done = stage(state, {
      type: "applyOutcome",
      outcome: { outcome: "committed", applied: ["d:2"], failedChange: null, message: null },
    });
    expect(changeCount(done)).toBe(0);
    expect(done.updates).toEqual({});
  });
});

describe("reverting", () => {
  it("reverts one cell, a row, or everything", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") },
      { type: "edit", cell: { row: row(1), column: "b", original: 1 }, input: value("2") },
      { type: "deleteRows", rows: [row(2)] },
    );
    expect(
      stage(state, { type: "revertCells", cells: [{ row: row(1), column: "a" }] }).updates[row(1).rowKey].set,
    ).toEqual({
      b: value("2"),
    });
    expect(changeCount(stage(state, { type: "revertRows", rows: [row(1), row(2)] }))).toBe(0);
    const discarded = stage(state, { type: "discardAll" });
    expect(changeCount(discarded)).toBe(0);
    expect(changeCount(stage(discarded, { type: "undo" }))).toBe(2);
  });

  it("reverts a marked row to the server's row: its hidden UPDATE goes too", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") },
      { type: "deleteRows", rows: [row(1)] },
    );
    const reverted = stage(state, { type: "revertRows", rows: [row(1)] });
    expect(reverted).toMatchObject({ updates: {}, deletes: {} });
  });

  it("discards nothing when nothing is staged, and new rows with the rest", () => {
    const state = emptyStaged(["id"]);
    expect(stage(state, { type: "discardAll" })).toBe(state);
    const added = run({ type: "addRow", id: "i:a" });
    expect(stage(added, { type: "discardAll" }).inserts).toEqual([]);
  });
});

describe("keep mine", () => {
  const conflicted = () =>
    run(
      { type: "edit", cell: { row: row(1), column: "a", original: "x" }, input: value("mine") },
      { type: "edit", cell: { row: row(1), column: "b", original: "y" }, input: value("theirs") },
    );

  it("checks against the server's values now", () => {
    const state = stage(conflicted(), { type: "keepMine", row: row(1), current: { a: "changed", b: "y" } });
    expect(state.updates[row(1).rowKey].original).toEqual({ a: "changed", b: "y" });
  });

  it("drops a cell the server already holds the staged value of", () => {
    const state = stage(conflicted(), { type: "keepMine", row: row(1), current: { a: "mine", b: "y" } });
    expect(state.updates[row(1).rowKey].set).toEqual({ b: value("theirs") });
  });

  it("changes nothing when nothing changed on the server", () => {
    const state = conflicted();
    expect(stage(state, { type: "keepMine", row: row(1), current: { a: "x", b: "y" } })).toBe(state);
  });

  it("drops a cell the server holds in another word of the column's kind", () => {
    const state = run({ type: "edit", cell: { row: row(1), column: "flag", original: false }, input: value("yes") });
    const kinds = new Map([["flag", "bool" as const]]);
    const kept = stage(state, { type: "keepMine", row: row(1), current: { flag: true }, kinds });
    expect(changeCount(kept)).toBe(0);
    expect(changeCount(stage(state, { type: "keepMine", row: row(1), current: { flag: true } }))).toBe(1);
  });
});

describe("undo and redo", () => {
  it("steps back and forward one action at a time, a multi-cell action being one step", () => {
    const one = run(
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") },
      {
        type: "setNull",
        cells: [
          { row: row(2), column: "a", original: 1 },
          { row: row(3), column: "a", original: 1 },
        ],
      },
    );
    const back = stage(one, { type: "undo" });
    expect(changeCount(back)).toBe(1);
    expect(changeCount(stage(back, { type: "redo" }))).toBe(3);
    expect(stage(stage(back, { type: "undo" }), { type: "undo" })).toMatchObject({ updates: {}, past: [] });
  });

  it("a new action after an undo clears the redo", () => {
    const state = stage(run({ type: "deleteRows", rows: [row(1)] }), { type: "undo" });
    const next = stage(state, { type: "deleteRows", rows: [row(2)] });
    expect(next.future).toEqual([]);
  });

  it(`keeps at most ${UNDO_LIMIT} steps`, () => {
    const actions: StageAction[] = Array.from({ length: UNDO_LIMIT + 20 }, (_, i) => ({
      type: "deleteRows",
      rows: [row(i)],
    }));
    expect(run(...actions).past).toHaveLength(UNDO_LIMIT);
  });

  it("keeps untouched changes as the same objects", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") },
      { type: "edit", cell: { row: row(2), column: "a", original: 1 }, input: value("2") },
    );
    const next = stage(state, { type: "edit", cell: { row: row(2), column: "a", original: 1 }, input: value("3") });
    expect(next.updates[row(1).rowKey]).toBe(state.updates[row(1).rowKey]);
  });

  it("gives back the very changes it stepped over, and does nothing at either end", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") },
      { type: "deleteRows", rows: [row(2)] },
    );
    const back = stage(state, { type: "undo" });
    expect(back.updates).toBe(state.past[1].updates);
    expect(back.deletes).toEqual({});
    const again = stage(back, { type: "redo" });
    expect(again.deletes).toBe(state.deletes);
    expect(again.past).toEqual(state.past);
    expect(stage(again, { type: "redo" })).toBe(again);
    const empty = emptyStaged(["id"]);
    expect(stage(empty, { type: "undo" })).toBe(empty);
    // Two steps back, one forward: the step undone last comes back first.
    const middle = stage(stage(back, { type: "undo" }), { type: "redo" });
    expect(middle.updates).toBe(back.updates);
    expect(middle.deletes).toEqual({});
  });
});

describe("the undo history's weight (ruling K22)", () => {
  const sum = (cells: readonly number[]) => cells.reduce((total, weight) => total + weight, 0);
  const ROWS = Array.from({ length: 10_000 }, (_, i) => row(i + 1));
  const COLUMNS = Array.from({ length: 29 }, (_, i) => `c${i + 1}`);
  const columnCells = (column: string) => ROWS.map((r) => ({ row: r, column, original: "v" }));
  /** ⌥⌘N over 10,000 rows, column by column (c1…c29): each step makes 10,000 UPDATEs one column wider. */
  const mass = () => {
    const newId = counter();
    return COLUMNS.reduce(
      (state, column) => stage(state, { type: "setNull", cells: columnCells(column) }, newId),
      emptyStaged(["id"]),
    );
  };

  it("weighs each step: the map entries the action copied, and each change it made with its entries", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") }, // 0 + (1 + 1 + 1)
      { type: "edit", cell: { row: row(1), column: "b", original: 1 }, input: value("2") }, // 1 + (1 + 2 + 2)
      { type: "deleteRows", rows: [row(2)] }, // 1 + 1
      { type: "addRow", id: "i:x" }, // 2 + (1 + 0)
      { type: "revertCells", cells: [{ row: row(1), column: "a" }, { row: row(1), column: "b" }] }, // 3 + 0
      { type: "discardAll" }, // makes nothing
    );
    expect(state.pastCells).toEqual([3, 6, 2, 3, 3, 0]);
    expect(state.past).toHaveLength(6);
    // The weight goes with its step: to the redo list and back.
    const back = stage(stage(state, { type: "undo" }), { type: "undo" });
    expect(back).toMatchObject({ pastCells: [3, 6, 2, 3], futureCells: [3, 0] });
    const again = stage(back, { type: "redo" });
    expect(again).toMatchObject({ pastCells: [3, 6, 2, 3, 3], futureCells: [0] });
    const edited = stage(again, { type: "edit", cell: { row: row(9), column: "a", original: 1 }, input: value("2") });
    expect(edited).toMatchObject({ pastCells: [3, 6, 2, 3, 3, 5], futureCells: [], future: [] }); // 2 + (1 + 1 + 1)
  });

  it(`keeps all ${UNDO_LIMIT} steps of editing cells one by one at 10,000 staged changes of 29 columns`, () => {
    const wide = mass();
    expect(changeCount(wide)).toBe(10_000);
    const newId = counter();
    let state = wide;
    for (let i = 0; i < UNDO_LIMIT; i += 1) {
      const cell = { row: ROWS[i * 37], column: "c1", original: null };
      state = stage(state, { type: "edit", cell, input: value(`x${i}`) }, newId);
    }
    expect(state.past).toHaveLength(UNDO_LIMIT);
    expect(sum(state.pastCells)).toBeLessThanOrEqual(UNDO_CELLS);
    for (let i = 0; i < UNDO_LIMIT; i += 1) state = stage(state, { type: "undo" });
    expect(state.updates).toBe(wide.updates); // every one of them undone, back to the 29 columns
    expect(stage(state, { type: "undo" })).toBe(state); // the mass steps went to make room
  });

  it("after actions over 10,000 rows keeps only the last few, and undoes and redoes those", () => {
    const state = mass();
    // ⌥⌘N on the k-th column: 10,000 entries copied (none for the first) + 10,000 × (1 + k + k)
    expect(state.pastCells).toEqual([560_000, 580_000, 600_000]);
    let back = state;
    for (let i = 0; i < 3; i += 1) back = stage(back, { type: "undo" });
    expect(stage(back, { type: "undo" })).toBe(back);
    expect(Object.keys(Object.values(back.updates)[0].set)).toHaveLength(26);
    let forward = back;
    for (let i = 0; i < 3; i += 1) forward = stage(forward, { type: "redo" });
    expect(forward.updates).toBe(state.updates);
    expect(forward.pastCells).toEqual(state.pastCells);
  });

  it("keeps steps that weigh exactly as much as the history may: ⌥⌘N and Revert on one column, 50 times each", () => {
    // ⌥⌘N: no entry copied (Revert left none) + 10,000 × (1 + 1 + 1); Revert: 10,000 copied, nothing made
    const newId = counter();
    let state = emptyStaged(["id"]);
    for (let i = 0; i < UNDO_LIMIT; i += 1) {
      const cells = columnCells("c1");
      state = stage(state, i % 2 === 0 ? { type: "setNull", cells } : { type: "revertCells", cells }, newId);
    }
    expect(sum(state.pastCells)).toBe(UNDO_CELLS);
    expect(state.past).toHaveLength(UNDO_LIMIT);
  });

  it("keeps the last step even when it alone weighs more than the whole history may", () => {
    // 10,000 UPDATEs of 100 columns each, then ⌥⌘N on a 101st: 10,000 + 10,000 × (1 + 101 + 101) cells
    const columns = Array.from({ length: 100 }, (_, i) => `w${i}`);
    const updates = Object.fromEntries(
      ROWS.map((r, i) => [
        r.rowKey,
        {
          id: `u:w${i}`,
          key: r.key,
          set: Object.fromEntries(columns.map((column) => [column, NULL_INPUT])),
          original: Object.fromEntries(columns.map((column) => [column, "v"])),
          seq: i,
        },
      ]),
    );
    const wide: StagedEdits = { ...emptyStaged(["id"]), updates, nextSeq: ROWS.length };
    const edited = stage(wide, { type: "addRow", id: "i:x" });
    const state = stage(edited, { type: "setNull", cells: columnCells("w100") });
    expect(state.pastCells).toEqual([2_040_001]);
    expect(state.past).toHaveLength(1); // the step before it went
    expect(stage(state, { type: "undo" }).inserts).toBe(edited.inserts);
  });

  it("goes with the steps when an outcome clears the history", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") },
      { type: "deleteRows", rows: [row(2)] },
      { type: "undo" },
    );
    expect(state).toMatchObject({ pastCells: [3], futureCells: [2] });
    const outcome: AppliedOutcome = { outcome: "committed", applied: [], failedChange: null, message: null };
    expect(stage(state, { type: "applyOutcome", outcome })).toMatchObject({ pastCells: [], futureCells: [] });
  });

  it("reverts a row's cells in one copy of its set and original, however many columns go (a select-all Revert)", () => {
    const rows = ROWS.slice(0, 1_000);
    const cells = ["a", "b", "c"].flatMap((column) => rows.map((r) => ({ row: r, column, original: 1 })));
    const state = stage(emptyStaged(["id"]), { type: "setNull", cells }, counter());
    const spy = vi.spyOn(Object, "fromEntries");
    let all: StagedEdits;
    let copies: number;
    try {
      all = stage(state, { type: "revertCells", cells });
    } finally {
      copies = spy.mock.calls.length;
      spy.mockRestore();
    }
    expect(all.updates).toEqual({});
    expect(copies).toBe(2 * rows.length);
    const two = stage(state, { type: "revertCells", cells: cells.filter((cell) => cell.column !== "c") });
    expect(two.updates[rows[0].rowKey]).toMatchObject({ set: { c: NULL_INPUT }, original: { c: 1 } });
    expect(Object.keys(two.updates[rows[0].rowKey].set)).toEqual(["c"]);
  });
});

describe("apply outcomes", () => {
  const staged = () =>
    run(
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") },
      { type: "addRow", id: "i:x" },
      { type: "deleteRows", rows: [row(2)] },
    );

  it("a commit empties the staging and its history", () => {
    const done = stage(staged(), {
      type: "applyOutcome",
      outcome: { outcome: "committed", applied: ["u:1", "i:x", "d:2"], failedChange: null, message: null },
    });
    expect(done).toMatchObject({ updates: {}, inserts: [], deletes: {}, past: [], future: [], unknownOutcome: null });
  });

  it("a rollback keeps everything and marks the failed change", () => {
    const state = staged();
    const failed = stage(state, {
      type: "applyOutcome",
      outcome: { outcome: "rolled_back", applied: [], failedChange: "u:1", message: "Row id = 1 was changed" },
    });
    expect(failed.updates).toBe(state.updates);
    expect(failed.past).toBe(state.past);
    expect(failed.problems).toEqual({ "u:1": [{ column: null, message: "Row id = 1 was changed" }] });
  });

  it("a partial outcome removes what took effect and clears the history, so undo cannot bring it back", () => {
    const part = stage(staged(), {
      type: "applyOutcome",
      outcome: { outcome: "partial", applied: ["d:2"], failedChange: "u:1", message: "conflict" },
    });
    expect(changeCount(part)).toBe(2);
    expect(part.past).toEqual([]);
  });

  it("an unknown outcome is pending until fresh rows are read, then refreshed until a commit (ruling K10)", () => {
    const unknown = stage(staged(), {
      type: "applyOutcome",
      outcome: { outcome: "unknown", applied: [], failedChange: null, message: null },
    });
    expect(unknown.unknownOutcome).toBe("pending");
    expect(changeCount(unknown)).toBe(3);
    const refreshed = stage(unknown, { type: "refreshed" });
    expect(refreshed.unknownOutcome).toBe("refreshed");
    expect(stage(refreshed, { type: "refreshed" })).toBe(refreshed);
    const rolledBack = stage(refreshed, {
      type: "applyOutcome",
      outcome: { outcome: "rolled_back", applied: [], failedChange: null, message: null },
    });
    expect(rolledBack).toBe(refreshed); // the rows its warning is about are all still staged
    const committed = stage(refreshed, {
      type: "applyOutcome",
      outcome: { outcome: "committed", applied: [], failedChange: null, message: null },
    });
    expect(committed.unknownOutcome).toBeNull();
  });

  it("an unknown outcome that names what took effect removes it and clears the history", () => {
    const unknown = stage(staged(), {
      type: "applyOutcome",
      outcome: { outcome: "unknown", applied: ["u:1"], failedChange: null, message: null },
    });
    expect(unknown).toMatchObject({ updates: {}, past: [], future: [], unknownOutcome: "pending" });
    expect(changeCount(unknown)).toBe(2);
  });

  it("a commit empties the history even when it names no staged change", () => {
    const done = stage(staged(), {
      type: "applyOutcome",
      outcome: { outcome: "committed", applied: [], failedChange: null, message: null },
    });
    expect(done).toMatchObject({ past: [], future: [] });
  });

  it("an outcome that changes nothing gives the same state", () => {
    const state = staged();
    expect(
      stage(state, {
        type: "applyOutcome",
        outcome: { outcome: "rolled_back", applied: [], failedChange: null, message: null },
      }),
    ).toBe(state);
  });

  it("an outcome without a failed change clears the last preview's problems, and keeps the changes", () => {
    const problems = { "u:1": [{ column: "a", message: "Not a number" }] };
    const state = stage(staged(), { type: "setProblems", problems });
    const next = stage(state, {
      type: "applyOutcome",
      outcome: { outcome: "rolled_back", applied: [], failedChange: null, message: null },
    });
    expect(next.problems).toEqual({});
    expect(next).toMatchObject({ updates: state.updates, inserts: state.inserts, deletes: state.deletes });
  });
});

describe("an apply in flight (pre-flight H1)", () => {
  const staged = () =>
    run(
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") },
      { type: "addRow", id: "i:x" },
    );
  const outcome = (kind: AppliedOutcome["outcome"], applied: string[] = []) =>
    ({ type: "applyOutcome", outcome: { outcome: kind, applied, failedChange: null, message: null } }) as const;

  it("is an outcome not known yet (saved as unknown) until its own outcome arrives, and never an undo step", () => {
    const state = staged();
    const applying = stage(state, { type: "applying" });
    expect(applying.unknownOutcome).toBe("applying");
    expect(applying).toMatchObject({ updates: state.updates, inserts: state.inserts, past: state.past });
    expect(stage(applying, { type: "applying" })).toBe(applying);
    // Rows read meanwhile do not make it known; neither does an undo.
    expect(stage(applying, { type: "refreshed" })).toBe(applying);
    expect(stage(applying, { type: "undo" }).unknownOutcome).toBe("applying");
    // Its outcome replaces it: nothing sent → nothing pending; lost → pending.
    expect(stage(applying, outcome("rolled_back")).unknownOutcome).toBeNull();
    expect(stage(applying, outcome("unknown")).unknownOutcome).toBe("pending");
  });

  describe("after an unknown outcome and fresh rows (ruling K10)", () => {
    const refreshed = () =>
      stage(stage(stage(staged(), { type: "applying" }), outcome("unknown")), { type: "refreshed" });
    const applying = () => stage(refreshed(), { type: "applying" });

    it("keeps the warning through an apply that took nothing: not sent, rolled back, or a partial one", () => {
      expect(refreshed().unknownOutcome).toBe("refreshed");
      expect(applying().unknownOutcome).toBe("applying");
      expect(stage(applying(), { type: "applyOutcome", outcome: NOT_SENT_OUTCOME }).unknownOutcome).toBe("refreshed");
      expect(stage(applying(), outcome("rolled_back")).unknownOutcome).toBe("refreshed"); // plan_expired, a conflict…
      const part = stage(applying(), outcome("partial", ["u:1"]));
      expect(part).toMatchObject({ unknownOutcome: "refreshed", beforeApplying: null });
      expect(part.inserts).toHaveLength(1); // the new row the warning is about is still staged
    });

    it("drops it after a commit, and waits for fresh rows again after another unknown outcome", () => {
      expect(stage(applying(), outcome("committed", ["u:1", "i:x"])).unknownOutcome).toBeNull();
      expect(stage(applying(), outcome("unknown"))).toMatchObject({ unknownOutcome: "pending", beforeApplying: null });
    });

    it("remembers only what the first applying replaced", () => {
      const first = applying();
      const twice = stage(first, { type: "applying" });
      expect(twice).toBe(first); // no new state, so "applying" is never what it remembers
      expect(twice).toMatchObject({ unknownOutcome: "applying", beforeApplying: "refreshed" });
      expect(stage(twice, outcome("rolled_back")).unknownOutcome).toBe("refreshed");
    });

    it("gives nothing to remember to an apply that started with no warning", () => {
      const fresh = stage(staged(), { type: "applying" });
      expect(fresh.beforeApplying).toBeNull();
      expect(stage(fresh, outcome("rolled_back"))).toMatchObject({ unknownOutcome: null, beforeApplying: null });
    });
  });
});

describe("problems", () => {
  it("are replaced by the next preview's, and a change edited again loses its own", () => {
    const state = stage(run({ type: "edit", cell: { row: row(1), column: "n", original: 1 }, input: value("x") }), {
      type: "setProblems",
      problems: { "u:1": [{ column: "n", message: "Not a number" }] },
    });
    expect(state.problems["u:1"]).toHaveLength(1);
    const edited = stage(state, { type: "edit", cell: { row: row(1), column: "n", original: 1 }, input: value("5") });
    expect(edited.problems).toEqual({});
  });

  it("go with a change the next preview will not send: an UPDATE hidden by its DELETE, a new row taken away", () => {
    const state = stage(
      run(
        { type: "edit", cell: { row: row(1), column: "n", original: 1 }, input: value("x") },
        { type: "addRow", id: "i:a" },
      ),
      {
        type: "setProblems",
        problems: { "u:1": [{ column: "n", message: "Not a number" }], "i:a": [{ column: null, message: "No" }] },
      },
    );
    expect(stage(state, { type: "deleteRows", rows: [row(1)] }).problems).toEqual({
      "i:a": [{ column: null, message: "No" }],
    });
    expect(stage(state, { type: "deleteRows", rows: [{ kind: "insert", id: "i:a" }] }).problems).toEqual({
      "u:1": [{ column: "n", message: "Not a number" }],
    });
  });

  it("stay the same map when an action drops none of them", () => {
    const state = stage(run({ type: "edit", cell: { row: row(1), column: "n", original: 1 }, input: value("x") }), {
      type: "setProblems",
      problems: { "u:1": [{ column: "n", message: "Not a number" }] },
    });
    const other = stage(state, { type: "edit", cell: { row: row(2), column: "n", original: 1 }, input: value("y") });
    expect(other.problems).toBe(state.problems);
  });

  it("leave the state as it is when a preview finds none and none were there, or brings the same ones", () => {
    const state = run({ type: "edit", cell: { row: row(1), column: "n", original: 1 }, input: value("x") });
    expect(stage(state, { type: "setProblems", problems: {} })).toBe(state);
    expect(stage(state, { type: "setProblems", problems: state.problems })).toBe(state);
    const some = { "u:1": [{ column: "n", message: "Not a number" }] };
    const marked = stage(state, { type: "setProblems", problems: some });
    expect(marked.problems).toBe(some);
    expect(stage(marked, { type: "setProblems", problems: some })).toBe(marked);
    expect(stage(marked, { type: "setProblems", problems: {} }).problems).toEqual({});
  });

  it("of every change one action touches go in one copy of the map (M1: linear at D-05-11's limit)", () => {
    // The pre-flight's case, smaller: N staged UPDATEs, a preview with N problems, then Revert rows or ⌘⌫ over all.
    const rows = Array.from({ length: 1_000 }, (_, i) => row(i));
    const cells = rows.map((r) => ({ row: r, column: "n", original: 1 }));
    const staged = stage(emptyStaged(["id"]), { type: "setNull", cells }, counter());
    const problems = Object.fromEntries(
      Object.values(staged.updates).map((u) => [u.id, [{ column: "n", message: "Not a number" }]]),
    );
    const state = stage(staged, { type: "setProblems", problems });
    for (const action of [
      { type: "revertRows", rows },
      { type: "deleteRows", rows },
    ] satisfies StageAction[]) {
      // Every copy that takes entries out of a record is made by `Object.fromEntries` (`withoutAll`): count them.
      const spy = vi.spyOn(Object, "fromEntries");
      let next: StagedEdits;
      let copies: number;
      try {
        next = stage(state, action);
      } finally {
        copies = spy.mock.calls.length;
        spy.mockRestore();
      }
      expect(next.problems).toEqual({});
      expect(copies).toBe(1);
    }
  });
});

describe("the changes a preview gets", () => {
  it("lists each kind in the order it was staged, with an original per set column", () => {
    const state = run(
      { type: "edit", cell: { row: row(2), column: "a", original: "x" }, input: value("1") },
      { type: "edit", cell: { row: row(1), column: "a", original: "y" }, input: value("2") },
      { type: "edit", cell: { row: row(2), column: "b", original: "z" }, input: { kind: "null" } },
      { type: "addRow", id: "i:b" },
      { type: "addRow", id: "i:a" },
    );
    const changes = toChanges(state);
    expect(changes.updates?.map((u) => u.key)).toEqual([{ id: 2 }, { id: 1 }]);
    expect(changes.updates?.[0]).toEqual({
      id: "u:1",
      key: { id: 2 },
      set: { a: value("1"), b: { kind: "null" } },
      original: { a: "x", b: "z" },
    });
    expect(changes.inserts?.map((i) => i.id)).toEqual(["i:b", "i:a"]);
  });

  it("gives every change its own place in that order, and keeps it whatever order the maps hold them in", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "a", original: "x" }, input: value("1") },
      { type: "addRow", id: "i:a" },
      { type: "addRow", id: "i:b" },
      { type: "deleteRows", rows: [row(2)] },
    );
    expect([state.inserts.map((i) => i.seq), state.nextSeq]).toEqual([[1, 2], 4]);
    // A staging read back from a file holds its maps in the file's order.
    const restored: StagedEdits = {
      ...emptyStaged(["id"]),
      deletes: {
        [row(2).rowKey]: { id: "d:b", key: { id: 2 }, seq: 1 },
        [row(1).rowKey]: { id: "d:a", key: { id: 1 }, seq: 0 },
      },
    };
    expect(toChanges(restored).deletes?.map((d) => d.id)).toEqual(["d:a", "d:b"]);
  });

  it("keys a row by its key cells in key order", () => {
    expect(rowKeyFromKey({ b: 2, a: "x" }, ["b", "a"])).toBe('[2,"x"]');
    expect(rowKeyFromKey({ a: "x" }, ["a", "b"])).toBe('["x",null]');
    expect(rowKeyFromKey({}, ["constructor"])).toBe("[null]");
  });

  it("finds a change by its id", () => {
    const state = run({ type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") });
    expect(findChange(state, "u:1")).toMatchObject({ kind: "update", rowKey: row(1).rowKey });
    expect(findChange(state, "u:9")).toBeNull();
  });
});
