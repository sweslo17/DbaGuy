import { describe, expect, it } from "vitest";
import {
  changeCount,
  emptyStaged,
  findChange,
  rowKeyFromKey,
  stage,
  toChanges,
  UNDO_LIMIT,
  type LoadedRowRef,
  type NewId,
  type StageAction,
  type StagedEdits,
} from "./staging";

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

  it("an unknown outcome is pending until fresh rows are read, then refreshed until the next apply", () => {
    const unknown = stage(staged(), {
      type: "applyOutcome",
      outcome: { outcome: "unknown", applied: [], failedChange: null, message: null },
    });
    expect(unknown.unknownOutcome).toBe("pending");
    expect(changeCount(unknown)).toBe(3);
    const refreshed = stage(unknown, { type: "refreshed" });
    expect(refreshed.unknownOutcome).toBe("refreshed");
    expect(stage(refreshed, { type: "refreshed" })).toBe(refreshed);
    const next = stage(refreshed, {
      type: "applyOutcome",
      outcome: { outcome: "rolled_back", applied: [], failedChange: null, message: null },
    });
    expect(next.unknownOutcome).toBeNull();
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
});

describe("an apply in flight (pre-flight H1)", () => {
  const staged = () =>
    run(
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") },
      { type: "addRow", id: "i:x" },
    );
  const outcome = (kind: "rolled_back" | "unknown") =>
    ({ type: "applyOutcome", outcome: { outcome: kind, applied: [], failedChange: null, message: null } }) as const;

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
