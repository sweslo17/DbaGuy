import { describe, expect, it } from "vitest";
import type { EditColumn } from "@/lib/api/types";
import type { RowMark } from "../grid/marks";
import type { GridSelection, Rect } from "../grid/selection";
import { KEY_INCOMPLETE, MARKED_DELETED } from "./editing";
import type { GridEditKey } from "./editKeys";
import { REPEATED_KEY } from "./overlay";
import { savedEdits, UNSAVEABLE_HINT, type SavedEdits } from "./savedEdits";
import { emptyStaged, MAX_CHANGES, rowKeyFromKey, stage, type NewId, type RowRef, type StageAction, type StagedEdits } from "./staging";
import {
  asksBeforeDiscard,
  CHECKING,
  closeQuestion,
  discardQuestion,
  dispatchRefusal,
  editMenu,
  editMode,
  gridKeyAction,
  KEEP_MINE_TITLE,
  KEPT_WITHOUT_HIDDEN,
  KEY_CHANGED,
  keyComplete,
  notKeptHint,
  pickCells,
  pickRows,
  placeAfterTop,
  rowAfterTop,
  ROWS_LOADING,
  rowHasConflict,
  rowStateOf,
  sameLoadedRows,
  selectedRows,
  selectionFacts,
  stagedInputOf,
  stageRefusal,
  startEdit,
  targetView,
  TOO_LARGE_TO_KEEP,
  TOO_MANY_CHANGES,
  TOO_MANY_ROWS,
  typedNotStaged,
  wasValueOf,
  type EditGate,
  type EditMenuInput,
  type GridLookup,
  type GridRowState,
  type SelectionFacts,
  type ShownTop,
  type TargetRead,
} from "./tableEditing";

/*
 * What a table-data tab's grid lets the user stage, and when (spec ⑤ §3.6.3–§3.6.6): nothing new on held rows or while
 * a commit runs (K19), only reverts when the table cannot be edited now (L5, H13), never on a row whose key repeats
 * (K6), never over more than MAX_CHANGES rows (H15), column names looked up as own properties (K2).
 */

const OPEN: EditGate = { locked: null, waiting: null, readOnly: null };
const LOCKED = "A commit is running";

const loaded = (id: unknown): RowRef => ({ kind: "row", rowKey: rowKeyFromKey({ id }, ["id"]), key: { id } });
const fresh = (id: string): RowRef => ({ kind: "insert", id });

function staged(...actions: StageAction[]): StagedEdits {
  let n = 0;
  const newId: NewId = (prefix) => `${prefix}:${++n}`;
  return actions.reduce((state, action) => stage(state, action, newId), emptyStaged(["id"]));
}
const edit = (id: number, column: string, text: string): StageAction => ({
  type: "edit",
  cell: { row: loaded(id), column, original: "was" },
  input: { kind: "value", text },
});

const column = (name: string, overrides: Partial<EditColumn> = {}): EditColumn => ({
  name,
  type_name: "text",
  kind: "text",
  nullable: true,
  has_default: false,
  writable: true,
  compare: "equals",
  unicode: true,
  ...overrides,
});

/** A grid of `refs` (display order) over the columns `names`, with the table's `columns` and the row states given. */
function lookup(
  refs: (RowRef | null)[],
  names: string[],
  columns: Record<string, EditColumn>,
  states: Map<RowRef, GridRowState> = new Map(),
): GridLookup {
  return {
    refAt: (row) => refs[row] ?? null,
    stateOf: (ref) => states.get(ref) ?? (ref.kind === "insert" ? "new" : "loaded"),
    columnAt: (col) => (names[col] === undefined ? null : { name: names[col], column: columns[names[col]] }),
    loadedAt: (row, col) => `loaded ${row}:${col}`,
  };
}
const rect = (top: number, bottom: number, left = 0, right = 0): Rect => ({ top, bottom, left, right });

describe("what may be staged now", () => {
  it("is everything, or only the reverts when the table cannot be edited now, or nothing while a commit runs", () => {
    expect(editMode(OPEN)).toBe("edit");
    expect(editMode({ ...OPEN, waiting: ROWS_LOADING })).toBe("revert");
    expect(editMode({ ...OPEN, readOnly: "Views are read-only here" })).toBe("revert");
    expect(editMode({ locked: LOCKED, waiting: ROWS_LOADING, readOnly: "x" })).toBe("locked");
  });

  it("refuses nothing new on held rows or a table that cannot be edited, and anything while a commit runs", () => {
    const waiting = { ...OPEN, waiting: ROWS_LOADING };
    expect(stageRefusal(OPEN, true)).toBeNull();
    expect(stageRefusal(waiting, true)).toBe(ROWS_LOADING);
    expect(stageRefusal({ ...OPEN, readOnly: "No key" }, true)).toBe("No key");
    expect(stageRefusal(waiting, false)).toBeNull(); // a revert, an undo
    expect(stageRefusal({ ...OPEN, locked: LOCKED }, false)).toBe(LOCKED);
    // a table that cannot be edited says so, rather than to wait for rows that will not change that
    expect(stageRefusal({ ...waiting, readOnly: "No key" }, true)).toBe("No key");
  });

  it("stages what an editor held when it ends on held rows (H12), but not while a commit runs", () => {
    expect(stageRefusal({ ...OPEN, waiting: ROWS_LOADING, readOnly: "x" }, true, true)).toBeNull();
    expect(stageRefusal({ ...OPEN, locked: LOCKED }, true, true)).toBe(LOCKED);
  });
});

describe("a refusal", () => {
  it("names why the store refused a dispatch, and nothing when it did not", () => {
    expect(dispatchRefusal("too_many")).toBe(TOO_MANY_CHANGES);
    expect(dispatchRefusal("key_changed")).toBe(KEY_CHANGED);
    expect(dispatchRefusal("staged")).toBeNull();
    expect(dispatchRefusal("unchanged")).toBeNull();
    expect(dispatchRefusal(null)).toBeNull();
  });

  it("of an editor's text says it was not staged, once, whatever the reason ends with (K20)", () => {
    expect(typedNotStaged(LOCKED)).toBe("A commit is running. What you typed was not staged.");
    expect(typedNotStaged("Done.")).toBe("Done. What you typed was not staged.");
    expect(typedNotStaged(TOO_MANY_CHANGES)).toBe(`${TOO_MANY_CHANGES}. What you typed was not staged.`);
  });
});

describe("a row's state", () => {
  const repeated = new Set([rowKeyFromKey({ id: 2 }, ["id"])]);
  const state = staged({ type: "deleteRows", rows: [loaded(2), loaded(3)] });

  it("tells new, repeated, marked, incomplete and loaded rows apart; a repeated key wins over a mark (K6)", () => {
    expect(rowStateOf(fresh("i:1"), state, repeated)).toBe("new");
    expect(rowStateOf(loaded(2), state, repeated)).toBe("repeated");
    expect(rowStateOf(loaded(3), state, repeated)).toBe("deleted");
    expect(rowStateOf(loaded(4), state, repeated)).toBe("loaded");
    expect(rowStateOf(loaded(4), null, null)).toBe("loaded");
    expect(rowStateOf(loaded(null), null, null)).toBe("key_incomplete");
  });

  it("calls a key incomplete when a cell is NULL, a cut text or bytes held only in part", () => {
    expect(keyComplete({ id: 1, region: "eu" })).toBe(true);
    expect(keyComplete({ id: 1, region: null })).toBe(false);
    expect(keyComplete({ id: { $type: "text", preview: "abc", size: 70_000 } })).toBe(false);
    expect(keyComplete({ id: { $type: "bytes", size: 100, preview: "ab" } })).toBe(false);
    expect(keyComplete({ id: { $type: "bytes", size: 4, preview: "deadbeef" } })).toBe(true);
  });
});

describe("a cell's staged value and what it was", () => {
  it("looks a column up as an own property, whatever its name (K2)", () => {
    for (const name of ["constructor", "__proto__", "toString"]) {
      expect(stagedInputOf(staged(), loaded(1), name)).toBeUndefined();
      expect(stagedInputOf(staged(edit(1, "label", "x")), loaded(1), name)).toBeUndefined();
      const state = staged(edit(1, name, "mine"));
      expect(stagedInputOf(state, loaded(1), name)).toEqual({ kind: "value", text: "mine" });
      expect(wasValueOf(state, loaded(1), name, "server now")).toBe("was");
      expect(stagedInputOf(staged({ type: "addRow", id: "i:a" }), fresh("i:a"), name)).toBeUndefined();
    }
    expect(stagedInputOf(null, loaded(1), "label")).toBeUndefined();
  });

  it("reads a new row's value, and a loaded cell's was as staged against, else as loaded now", () => {
    const state = staged({ type: "addRow", id: "i:a" }, edit(1, "label", "mine"));
    const at = { row: fresh("i:a"), column: "label" };
    const withValue = stage(state, { type: "edit", cell: at, input: { kind: "value", text: "new" } });
    expect(stagedInputOf(withValue, fresh("i:a"), "label")).toEqual({ kind: "value", text: "new" });
    expect(stagedInputOf(withValue, fresh("i:b"), "label")).toBeUndefined();
    expect(wasValueOf(state, loaded(1), "label", "server now")).toBe("was");
    expect(wasValueOf(state, loaded(1), "note", "server now")).toBe("server now");
    expect(wasValueOf(state, loaded(2), "label", "server now")).toBe("server now");
  });
});

describe("starting an edit", () => {
  const cell = { column: column("label"), name: "label", staged: undefined, original: "hello" };

  it("refuses while a commit runs, the rows are held or the table cannot be edited, and on a repeated key", () => {
    expect(startEdit({ locked: LOCKED, waiting: ROWS_LOADING, readOnly: CHECKING }, "loaded", cell)).toEqual({
      refuse: LOCKED,
    });
    expect(startEdit({ ...OPEN, waiting: ROWS_LOADING, readOnly: CHECKING }, "loaded", cell)).toEqual({
      refuse: CHECKING,
    });
    expect(startEdit({ ...OPEN, waiting: ROWS_LOADING }, "loaded", cell)).toEqual({ refuse: ROWS_LOADING });
    expect(startEdit(OPEN, "repeated", cell)).toEqual({ refuse: REPEATED_KEY });
  });

  it("then asks the cell (editStart): marked, incomplete, read-only", () => {
    expect(startEdit(OPEN, "deleted", cell)).toEqual({ refuse: MARKED_DELETED });
    expect(startEdit(OPEN, "key_incomplete", cell)).toEqual({ refuse: KEY_INCOMPLETE });
    expect(startEdit(OPEN, "loaded", { ...cell, column: column("label", { writable: false, read_only_reason: "generated column" }) })).toEqual({
      refuse: "label is read-only: generated column",
    });
    expect(startEdit(OPEN, "loaded", cell)).toMatchObject({ mode: "inline", initialText: "hello" });
    expect(startEdit(OPEN, "new", { ...cell, original: undefined })).toMatchObject({ placeholder: "DEFAULT" });
  });
});

describe("the grid's keys", () => {
  const all: GridEditKey[] = ["edit", "setNull", "deleteRows", "undo", "redo"];

  it("all act while editing; otherwise only undo and redo, so Return opens the value viewer (L5, K19)", () => {
    expect(all.map((key) => gridKeyAction(key, "edit"))).toEqual(all);
    for (const mode of ["revert", "locked"] as const) {
      expect(all.map((key) => gridKeyAction(key, mode))).toEqual([null, null, null, "undo", "redo"]);
    }
    expect(gridKeyAction(null, "edit")).toBeNull();
  });
});

describe("a selection's rows (H15)", () => {
  it("are refused beyond MAX_CHANGES rows, before anything is built", () => {
    expect(selectedRows(null)).toEqual([]);
    expect(selectedRows(rect(3, 5))).toEqual([3, 4, 5]);
    expect(selectedRows(rect(0, MAX_CHANGES - 1))).toHaveLength(MAX_CHANGES);
    expect(selectedRows(rect(0, MAX_CHANGES))).toBeNull();
    expect(selectedRows(rect(100, 100 + MAX_CHANGES))).toBeNull();
  });

  it("are refused for every pick", () => {
    const grid = lookup([], ["label"], { label: column("label") });
    expect(pickCells(rect(0, MAX_CHANGES), grid, { writable: true, nullable: true })).toBeNull();
    expect(pickCells(rect(0, MAX_CHANGES), grid, { writable: false, nullable: false })).toBeNull();
    expect(pickRows(rect(0, MAX_CHANGES), grid)).toBeNull();
  });
});

describe("picking cells", () => {
  const names = ["id", "label", "total", "code", "gone"];
  const columns = {
    id: column("id", { kind: "number" }),
    label: column("label"),
    total: column("total", { writable: false, read_only_reason: "generated column" }),
    code: column("code", { nullable: false }),
  };
  const refs = [fresh("i:a"), loaded(1), loaded(2), loaded(3), loaded(4), null];
  const states = new Map<RowRef, GridRowState>([
    [refs[2]!, "deleted"],
    [refs[3]!, "repeated"],
    [refs[4]!, "key_incomplete"],
  ]);
  const grid = lookup(refs, names, columns, states);

  it("for Set NULL: writable, nullable cells of rows that can be edited; NOT NULL ones counted apart", () => {
    const picked = pickCells(rect(0, 5, 0, 4), grid, { writable: true, nullable: true })!;
    expect(picked.cells.map((cell) => [cell.row, cell.column])).toEqual([
      [refs[0], "id"],
      [refs[0], "label"],
      [refs[1], "id"],
      [refs[1], "label"],
    ]);
    expect(picked.notNull).toBe(2); // code, on the new row and on row 1
    expect(picked.cells[2].original).toBe("loaded 1:0");
  });

  it("says why the first cell left out was", () => {
    expect(pickCells(rect(1, 1, 2, 2), grid, { writable: true, nullable: true })!.refusal).toBe(
      "total is read-only: generated column",
    );
    expect(pickCells(rect(1, 1, 4, 4), grid, { writable: true, nullable: true })!.refusal).toBe(
      "gone is not a column of the table any more: Refresh",
    );
    expect(pickCells(rect(2, 2, 1, 1), grid, { writable: true, nullable: true })!.refusal).toBe(MARKED_DELETED);
    expect(pickCells(rect(3, 3, 1, 1), grid, { writable: true, nullable: true })!.refusal).toBe(REPEATED_KEY);
    expect(pickCells(rect(4, 4, 1, 1), grid, { writable: true, nullable: true })!.refusal).toBe(KEY_INCOMPLETE);
    expect(pickCells(rect(1, 1, 1, 1), grid, { writable: true, nullable: true })!.refusal).toBeNull();
  });

  it("for the reverts: any cell of a row a change can be on, a marked row's included, never a repeated key's", () => {
    const picked = pickCells(rect(1, 4, 1, 3), grid, { writable: false, nullable: false })!;
    expect(picked.cells.map((cell) => [cell.row, cell.column])).toEqual([
      [refs[1], "label"],
      [refs[1], "total"],
      [refs[1], "code"],
      [refs[2], "label"],
      [refs[2], "total"],
      [refs[2], "code"],
    ]);
    expect(picked.notNull).toBe(0);
    expect(picked.refusal).toBe(REPEATED_KEY);
  });
});

describe("picking rows", () => {
  it("takes new, loaded and marked rows, never a repeated or incomplete key", () => {
    const refs = [fresh("i:a"), loaded(1), loaded(2), loaded(3), loaded(4)];
    const states = new Map<RowRef, GridRowState>([
      [refs[2], "deleted"],
      [refs[3], "repeated"],
      [refs[4], "key_incomplete"],
    ]);
    const picked = pickRows(rect(0, 9), lookup(refs, ["id"], {}, states))!;
    expect(picked.rows).toEqual([refs[0], refs[1], refs[2]]);
    expect(picked.refusal).toBe(REPEATED_KEY);
    expect(pickRows(null, lookup(refs, ["id"], {}))).toEqual({ rows: [], refusal: null });
  });
});

describe("what the menu knows of a selection", () => {
  it("tells marked, changed and staged cells", () => {
    const state = staged(
      { type: "addRow", id: "i:a" },
      edit(1, "label", "one"),
      edit(2, "label", "two"),
      { type: "deleteRows", rows: [loaded(2), loaded(3)] },
    );
    const facts = (rows: RowRef[], columns = ["label"]) => selectionFacts(rows, columns, state);
    expect(facts([loaded(2), loaded(3)])).toEqual({ rows: 2, allDeleted: true, anyChanged: true, anyStagedCell: false });
    expect(facts([loaded(1)])).toEqual({ rows: 1, allDeleted: false, anyChanged: true, anyStagedCell: true });
    expect(facts([loaded(1)], ["note"]).anyStagedCell).toBe(false);
    expect(facts([loaded(3)])).toEqual({ rows: 1, allDeleted: true, anyChanged: true, anyStagedCell: false }); // marked only
    expect(facts([loaded(4)])).toEqual({ rows: 1, allDeleted: false, anyChanged: false, anyStagedCell: false });
    expect(facts([fresh("i:a")])).toMatchObject({ anyChanged: true, anyStagedCell: false }); // all DEFAULT
    expect(facts([fresh("i:gone")])).toMatchObject({ anyChanged: false });
    expect(selectionFacts([loaded(1)], ["label"], null)).toEqual({
      rows: 1,
      allDeleted: false,
      anyChanged: false,
      anyStagedCell: false,
    });
  });

  it("finds a staged cell whatever the column is named (K2)", () => {
    expect(selectionFacts([loaded(1)], ["constructor"], staged(edit(1, "label", "x"))).anyStagedCell).toBe(false);
    expect(selectionFacts([loaded(1)], ["constructor"], staged(edit(1, "constructor", "x"))).anyStagedCell).toBe(true);
  });

  it("sees a conflict on a changed row's cell", () => {
    const mark = (cells: RowMark["cells"], kind: RowMark["kind"] = "changed"): RowMark => ({ kind, cells });
    expect(rowHasConflict(mark({ 1: { changed: true, conflict: "Changed on the server: now x" } }))).toBe(true);
    expect(rowHasConflict(mark({ 1: { changed: true } }))).toBe(false);
    expect(rowHasConflict(mark({ 1: { changed: true, conflict: "x" } }, "deleted"))).toBe(false);
    expect(rowHasConflict(undefined)).toBe(false);
  });
});

describe("the edit menu", () => {
  const some: SelectionFacts = { rows: 1, allDeleted: false, anyChanged: true, anyStagedCell: true };
  const none: SelectionFacts = { rows: 1, allDeleted: false, anyChanged: false, anyStagedCell: false };
  const loadedCell = { refusal: null, isNew: false, conflict: false };
  const menu = (input: Partial<EditMenuInput>) =>
    editMenu({ mode: "edit", changes: 1, at: loadedCell, selection: some, ...input });
  const ids = (input: Partial<EditMenuInput>) => menu(input).map((entry) => entry.id);

  it("leads a changed cell's menu in the spec's order", () => {
    expect(ids({})).toEqual(["edit", "setNull", "revertCell", "revertRow", "deleteRows", "addRow"]);
    expect(menu({}).every((entry) => !entry.disabled)).toBe(true);
  });

  it("has Use default on a new row, Keep mine on a conflict, Restore rows when all are marked", () => {
    expect(ids({ at: { ...loadedCell, isNew: true } })).toEqual([
      "edit",
      "setNull",
      "useDefault",
      "revertCell",
      "revertRow",
      "deleteRows",
      "addRow",
    ]);
    expect(menu({ at: { ...loadedCell, conflict: true } })).toContainEqual({ id: "keepMine", title: KEEP_MINE_TITLE });
    expect(ids({ selection: { ...some, allDeleted: true } })).toContain("restoreRows");
    expect(ids({ selection: { ...some, allDeleted: true } })).not.toContain("deleteRows");
  });

  it("gives the row numbers' menu the row items only", () => {
    expect(ids({ at: null })).toEqual(["revertRow", "deleteRows", "addRow"]);
  });

  it("disables what has nothing to act on, and says why a cell cannot be edited", () => {
    const entries = menu({ selection: none, at: { ...loadedCell, refusal: "total is read-only: generated column" } });
    expect(entries.find((e) => e.id === "edit")).toEqual({
      id: "edit",
      disabled: true,
      title: "total is read-only: generated column",
    });
    expect(entries.filter((e) => e.disabled).map((e) => e.id)).toEqual(["edit", "revertCell", "revertRow"]);
    // a changed row whose selected cells hold nothing staged: Revert row, not Revert cell
    const otherCell = menu({ selection: { ...some, anyStagedCell: false } });
    expect(otherCell.filter((e) => e.disabled).map((e) => e.id)).toEqual(["revertCell"]);
    expect(menu({ at: null, selection: { ...none, rows: 0 } }).find((e) => e.id === "deleteRows")?.disabled).toBe(true);
  });

  it("disables every item over a selection of more than MAX_CHANGES rows, not the cell's own (H15)", () => {
    const entries = menu({ selection: null, at: { ...loadedCell, isNew: true } });
    expect(entries.filter((e) => !e.disabled).map((e) => e.id)).toEqual(["edit", "addRow"]);
    expect(entries.filter((e) => e.disabled).every((e) => e.title === TOO_MANY_ROWS)).toBe(true);
  });

  it("offers only the reverts when the table cannot be edited now, without the key ⌘⌫ there (L5, K19)", () => {
    expect(ids({ mode: "revert" })).toEqual(["revertCell", "revertRow"]);
    expect(ids({ mode: "revert", at: null })).toEqual(["revertRow"]);
    expect(menu({ mode: "revert", selection: { ...some, allDeleted: true } }).at(-1)).toEqual({
      id: "restoreRows",
      keyless: true,
    });
    expect(menu({ mode: "revert", selection: none }).every((e) => e.disabled)).toBe(true);
    expect(menu({ mode: "revert", selection: null })[0]).toEqual({ id: "revertCell", disabled: true, title: TOO_MANY_ROWS });
  });

  it("is empty when nothing could be reverted, and while a commit runs", () => {
    expect(menu({ mode: "revert", changes: 0 })).toEqual([]);
    expect(menu({ mode: "locked" })).toEqual([]);
  });
});

describe("the edit target's read", () => {
  const read = (overrides: Partial<TargetRead>): TargetRead => ({
    rowEditing: true,
    status: "pending",
    fetching: true,
    passwordRequired: false,
    failedSinceOpen: false,
    ...overrides,
  });

  it("shows nothing for an engine without row editing, checking until it answers, then the target", () => {
    expect(targetView(read({ rowEditing: false, status: "success" }))).toBe("none");
    expect(targetView(read({}))).toBe("checking");
    expect(targetView(read({ fetching: false }))).toBe("checking"); // not asked yet
    expect(targetView(read({ status: "success", fetching: false }))).toBe("read");
    expect(targetView(read({ status: "success", fetching: true }))).toBe("read"); // a Refresh keeps it
  });

  it("shows an error with Retry, saying nothing while it is retried", () => {
    expect(targetView(read({ status: "error", fetching: false }))).toBe("error");
    expect(targetView(read({ status: "error", fetching: true }))).toBe("checking");
  });

  it("leaves a missing password to the session's unlock until the session opened, then asks for it (L13)", () => {
    const wanting = read({ status: "error", fetching: false, passwordRequired: true });
    expect(targetView(wanting)).toBe("checking");
    expect(targetView({ ...wanting, failedSinceOpen: true })).toBe("unlock");
    expect(targetView({ ...wanting, failedSinceOpen: true, fetching: true })).toBe("checking");
  });
});

describe("the edit bar's words", () => {
  it("says why staged changes are not kept across a restart (K5), or not all of them (K3b)", () => {
    expect(notKeptHint(savedEdits(staged(edit(1, "label", "x"))))).toBeNull();
    expect(notKeptHint({ file: null, tooLarge: true, unsaveable: null })).toBe(TOO_LARGE_TO_KEEP);
    expect(notKeptHint(savedEdits(staged(edit(1, "label", "\ud800"))))).toBe(UNSAVEABLE_HINT.value);
    const kept = savedEdits(staged(edit(1, "label", "x")));
    expect(notKeptHint({ ...kept, withoutHidden: true } as SavedEdits)).toBe(KEPT_WITHOUT_HIDDEN);
  });

  it("asks before discarding more than one change", () => {
    expect([0, 1, 2].map(asksBeforeDiscard)).toEqual([false, false, true]);
    expect(discardQuestion(3, "zz_05b_items")).toBe("Discard 3 staged changes to zz_05b_items? ⌘Z brings them back.");
  });

  it("asks before closing the tab without offering ⌘Z: the changes go with the tab", () => {
    expect(closeQuestion(1, "zz_05b_items")).toBe("Discard 1 staged change to zz_05b_items?");
    expect(closeQuestion(3, "zz_05b_items")).toBe("Discard 3 staged changes to zz_05b_items?");
  });
});

describe("new rows coming and going on top of the same loaded rows", () => {
  const top = (...newIds: string[]): ShownTop => ({ leading: newIds.length, newIds });
  const cells = (anchor: number, focus = anchor, col = 2): GridSelection => ({
    anchor: { row: anchor, col },
    focus: { row: focus, col },
    mode: "cells",
  });
  const H = 22;

  it("are the same rows when they are, or a page more of them", () => {
    const rows = [[1], [2]];
    expect(sameLoadedRows(rows, rows)).toBe(true);
    expect(sameLoadedRows(rows, [...rows, [3]])).toBe(true);
    expect(sameLoadedRows(rows, [[1], [2]])).toBe(false); // read again: the anchor's to place
    expect(sameLoadedRows([], [[1]])).toBe(false);
  });

  it("move a loaded row by the new rows above it and find a new row by its id", () => {
    expect(rowAfterTop(5, top("b"), top("c", "b"))).toBe(6);
    expect(rowAfterTop(0, top("b"), top("c", "b"))).toBe(1);
    expect(rowAfterTop(1, top("c", "b"), top("b"))).toBe(0);
    expect(rowAfterTop(0, top("c", "b"), top("b"))).toBeNull(); // c went
    expect(rowAfterTop(3, top("c", "b"), top())).toBe(1);
  });

  it("keep the selection on its row and the loaded rows where they were on screen (Add row, its undo)", () => {
    const added = placeAfterTop({ selection: cells(10), scrollTop: 5 * H }, top(), top("a"), H);
    expect(added).toEqual({ selection: cells(11), clearSelection: false, scrollTop: 6 * H });
    const undone = placeAfterTop({ selection: cells(11, 13), scrollTop: 6 * H }, top("a"), top(), H);
    expect(undone).toEqual({ selection: cells(10, 12), clearSelection: false, scrollTop: 5 * H });
    const rows: GridSelection = { anchor: { row: 4, col: 0 }, focus: { row: 6, col: 0 }, mode: "rows" };
    expect(placeAfterTop({ selection: rows, scrollTop: H }, top("a", "b"), top("b"), H).selection).toEqual({
      ...rows,
      anchor: { row: 3, col: 0 },
      focus: { row: 5, col: 0 },
    });
  });

  it("clear a selection on a new row that went, never moving it onto another row (K14b)", () => {
    expect(placeAfterTop({ selection: cells(0), scrollTop: 3 * H }, top("a"), top(), H)).toEqual({
      selection: null,
      clearSelection: true,
      scrollTop: 2 * H,
    });
    expect(placeAfterTop({ selection: cells(0, 4), scrollTop: 0 }, top("a"), top(), H).clearSelection).toBe(true);
    // a new row that stays is followed
    expect(placeAfterTop({ selection: cells(1), scrollTop: 0 }, top("b", "a"), top("a"), H).selection).toEqual(
      cells(0),
    );
  });

  it("stay at the very top when the view was there, so new rows show; whole columns keep their selection", () => {
    expect(placeAfterTop({ selection: null, scrollTop: 0 }, top(), top("a"), H)).toEqual({
      selection: null,
      clearSelection: false,
      scrollTop: 0,
    });
    expect(placeAfterTop({ selection: null, scrollTop: 10 }, top("a", "b"), top(), H).scrollTop).toBe(0);
    const columns: GridSelection = { anchor: { row: 0, col: 1 }, focus: { row: 0, col: 2 }, mode: "columns" };
    expect(placeAfterTop({ selection: columns, scrollTop: H }, top(), top("a"), H)).toEqual({
      selection: null,
      clearSelection: false,
      scrollTop: 2 * H,
    });
  });
});
