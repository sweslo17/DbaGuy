import { describe, expect, it, vi, type Mock } from "vitest";
import { tableEditActions, type TableEditActionId, type TableEditFacts } from "./editActions";

/* The palette's table-edit actions (spec ⑤ §3.6.4): enabled as the tab's own controls are, run by their handlers. */

const IDS: TableEditActionId[] = ["addRow", "deleteRows", "setNull", "commit", "discardAll", "undo", "redo"];
/** A mock handler per action. */
const handlers = () => {
  const run = {} as Record<TableEditActionId, Mock<() => void>>;
  for (const id of IDS) run[id] = vi.fn();
  return run;
};
const facts = (overrides: Partial<TableEditFacts> = {}): TableEditFacts => ({
  editable: true,
  shown: true,
  changes: 2,
  commitBlocked: null,
  applying: false,
  undoable: true,
  redoable: true,
  ...overrides,
});
const enabled = (input: TableEditFacts) =>
  Object.fromEntries(Object.entries(tableEditActions(input, handlers())).map(([id, action]) => [id, action.enabled]));

describe("the palette's table-edit actions", () => {
  it("run the tab's own handlers", () => {
    const run = handlers();
    const actions = tableEditActions(facts(), run);
    for (const id of IDS) {
      actions[id].run();
      expect(run[id]).toHaveBeenCalledTimes(1);
    }
  });

  it("are all enabled with changes on rows that can be edited", () => {
    expect(Object.values(enabled(facts())).every(Boolean)).toBe(true);
  });

  it("stage nothing new while the rows cannot be edited, but still undo, redo, discard and commit", () => {
    expect(enabled(facts({ editable: false }))).toEqual({
      addRow: false,
      deleteRows: false,
      setNull: false,
      commit: true,
      discardAll: true,
      undo: true,
      redo: true,
    });
  });

  it("act on the grid only while it is on screen, and on the staging from Columns or DDL too", () => {
    expect(enabled(facts({ shown: false }))).toEqual({
      addRow: false,
      deleteRows: false,
      setNull: false,
      commit: true,
      discardAll: true,
      undo: true,
      redo: true,
    });
  });

  it("commit and discard only with changes, commit only when Commit… could start", () => {
    expect(enabled(facts({ changes: 0 }))).toMatchObject({ commit: false, discardAll: false, addRow: true });
    expect(enabled(facts({ commitBlocked: "Wait for the rows to load" }))).toMatchObject({
      commit: false,
      discardAll: true,
    });
  });

  it("undo and redo as far as the staging's history goes", () => {
    expect(enabled(facts({ undoable: false }))).toMatchObject({ undo: false, redo: true });
    expect(enabled(facts({ redoable: false }))).toMatchObject({ undo: true, redo: false });
  });

  it("change nothing while a commit runs", () => {
    const during = enabled(facts({ applying: true, editable: false, commitBlocked: "A commit is running" }));
    expect(Object.values(during).some(Boolean)).toBe(false);
  });
});
