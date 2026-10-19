import { describe, expect, it } from "vitest";
import type { ColumnMeta, EditKind } from "@/lib/api/types";
import {
  bindRows,
  displayIndexOf,
  overlay,
  REPEATED_KEY_MARK,
  resultColumns,
  rowKeyOf,
  rowRefAt,
  type OverlayCache,
  type OverlayInput,
} from "./overlay";
import { emptyStaged, rowKeyFromKey, stage, type NewId, type StageAction, type StagedEdits } from "./staging";

/*
 * The overlay (spec ⑤ §3.6.2): changes found by key whatever the rows' order or page, untouched rows kept as the same
 * objects (Q1), new rows on top, marks for changed, new, deleted, DEFAULT, problems and conflicts.
 */

const COLUMNS: ColumnMeta[] = [
  { name: "id", type_name: "bigint", category: "number" },
  { name: "label", type_name: "text", category: "text" },
  { name: "flag", type_name: "boolean", category: "bool" },
];
const KINDS = new Map<string, EditKind>([
  ["id", "number"],
  ["label", "text"],
  ["flag", "bool"],
]);
const RESULT = resultColumns(COLUMNS, ["id"]);

const makeRows = (from: number, count: number): unknown[][] =>
  Array.from({ length: count }, (_, i) => [from + i, `row ${from + i}`, false]);
const ref = (id: number) => ({ kind: "row", rowKey: rowKeyFromKey({ id }, ["id"]), key: { id } }) as const;

function staged(...actions: StageAction[]): StagedEdits {
  let n = 0;
  const newId: NewId = (prefix) => `${prefix}:${++n}`;
  return actions.reduce((state, action) => stage(state, action, newId), emptyStaged(["id"]));
}

const editLabel = (id: number, text: string, original = `row ${id}`): StageAction => ({
  type: "edit",
  cell: { row: ref(id), column: "label", original },
  input: { kind: "value", text },
});

function draw(rows: unknown[][], state: StagedEdits | null, cache: OverlayCache | null = null) {
  const binding = bindRows(rows, RESULT.keyIndexes!, null);
  const input: OverlayInput = { rows, columns: COLUMNS, kinds: KINDS, staged: state, binding, result: RESULT };
  return overlay(input, cache);
}

describe("binding rows to keys", () => {
  it("keys a row as its staged change names it", () => {
    expect(rowKeyOf([7, "x", true], [0])).toBe(rowKeyFromKey({ id: 7 }, ["id"]));
    expect(resultColumns(COLUMNS, ["missing"]).keyIndexes).toBeNull();
  });

  it("keys only the new page after Load more, and starts over for another result", () => {
    const first = makeRows(1, 500);
    const binding = bindRows(first, [0], null);
    expect(binding.keyed).toBe(500);
    const grown = first.concat(makeRows(501, 500)); // what the session store does with a page: the same row objects
    const next = bindRows(grown, [0], binding);
    expect(next.keyed).toBe(500);
    expect(next.byKey).toBe(binding.byKey);
    expect(next.byKey.get(rowKeyFromKey({ id: 900 }, ["id"]))).toBe(899);
    expect(bindRows(grown, [0], next)).toBe(next);
    const rerun = makeRows(1, 1000); // a refresh: equal values, new objects
    expect(bindRows(rerun, [0], next).keyed).toBe(1000);
  });
});

describe("the overlay", () => {
  it("is the rows themselves when nothing is staged", () => {
    const rows = makeRows(1, 3);
    const { overlay: shown } = draw(rows, null);
    expect(shown.rows).toBe(rows);
    expect(shown).toMatchObject({ leading: 0, notShown: 0 });
  });

  it("replaces only the changed row, keeping every other row object", () => {
    const rows = makeRows(1, 3);
    const { overlay: shown } = draw(rows, staged(editLabel(2, "two")));
    expect(shown.rows[0]).toBe(rows[0]);
    expect(shown.rows[2]).toBe(rows[2]);
    expect(shown.rows[1]).toEqual([2, "two", false]);
    expect(rows[1]).toEqual([2, "row 2", false]); // the result is never written
    expect(shown.marks[0]).toBeUndefined();
    expect(shown.marks[1]).toEqual({
      kind: "changed",
      problem: undefined,
      cells: { 1: { changed: true, was: "Was: row 2", problem: undefined, conflict: undefined } },
    });
  });

  it("reuses a changed row's object while neither it nor its change changes", () => {
    const rows = makeRows(1, 3);
    const one = staged(editLabel(2, "two"));
    const first = draw(rows, one);
    const two = stage(one, editLabel(3, "three"));
    const second = draw(rows, two, first.cache);
    expect(second.overlay.rows[1]).toBe(first.overlay.rows[1]);
    expect(second.overlay.marks[1]).toBe(first.overlay.marks[1]);
    expect(second.overlay.rows[2]).toEqual([3, "three", false]);
  });

  it("finds a change after Load more, a sort and a refresh, and counts it while it is not shown", () => {
    const state = staged(editLabel(700, "seven hundred"));
    const firstPage = makeRows(1, 500);
    expect(draw(firstPage, state).overlay.notShown).toBe(1);
    const both = firstPage.concat(makeRows(501, 500));
    expect(draw(both, state).overlay.rows[699]).toEqual([700, "seven hundred", false]);
    const sorted = makeRows(1, 1000).reverse();
    const { overlay: resorted } = draw(sorted, state);
    expect(resorted.rows[300]).toEqual([700, "seven hundred", false]);
    expect(resorted.notShown).toBe(0);
  });

  it("marks a conflict once the server's value is not the one the change was staged on", () => {
    const state = staged(editLabel(2, "mine"));
    const reread = makeRows(1, 3);
    reread[1] = [2, "theirs", false];
    const cell = draw(reread, state).overlay.marks[1]?.cells?.[1];
    expect(cell).toMatchObject({ changed: true, was: "Was: row 2", conflict: "Changed on the server: now theirs" });
  });

  it("draws a deleted row's loaded values, its UPDATE hidden", () => {
    const rows = makeRows(1, 3);
    const { overlay: shown } = draw(rows, staged(editLabel(2, "two"), { type: "deleteRows", rows: [ref(2)] }));
    expect(shown.rows[1]).toBe(rows[1]);
    expect(shown.marks[1]).toEqual({ kind: "deleted", problem: undefined });
  });

  it("puts new rows on top, newest first, their unset cells marked DEFAULT", () => {
    const rows = makeRows(1, 2);
    const state = staged(
      { type: "addRow", id: "i:old" },
      { type: "addRow", id: "i:new" },
      {
        type: "edit",
        cell: { row: { kind: "insert", id: "i:new" }, column: "flag" },
        input: { kind: "value", text: "yes" },
      },
    );
    const { overlay: shown } = draw(rows, state);
    expect(shown.leading).toBe(2);
    expect(shown.rows.slice(0, 2)).toEqual([
      [null, null, true],
      [null, null, null],
    ]);
    expect(shown.rows[2]).toBe(rows[0]);
    expect(shown.marks[0]).toEqual({
      kind: "new",
      problem: undefined,
      cells: {
        0: { changed: false, isDefault: true, problem: undefined },
        1: { changed: false, isDefault: true, problem: undefined },
      },
    });
    expect(rowRefAt(shown, state, rows, 0, RESULT)).toEqual({ kind: "insert", id: "i:new" });
    expect(rowRefAt(shown, state, rows, 2, RESULT)).toEqual(ref(1));
    expect(displayIndexOf(shown, state, bindRows(rows, [0], null), ref(2))).toBe(3);
    expect(displayIndexOf(shown, state, null, { kind: "insert", id: "i:old" })).toBe(1);
  });

  it("puts a problem on its cell, or on the row when it is about the change as a whole", () => {
    const state = stage(staged(editLabel(1, "x")), {
      type: "setProblems",
      problems: {
        "u:1": [
          { column: "label", message: "Too long" },
          { column: null, message: "Row id = 1 was changed or deleted" },
        ],
      },
    });
    const mark = draw(makeRows(1, 1), state).overlay.marks[0];
    expect(mark?.problem).toBe("Row id = 1 was changed or deleted");
    expect(mark?.cells?.[1].problem).toBe("Too long");
  });

  it("keeps a staged number's digits as text", () => {
    const state = staged({
      type: "edit",
      cell: { row: ref(1), column: "label", original: "row 1" },
      input: { kind: "value", text: "9007199254740993" },
    });
    expect(draw(makeRows(1, 1), state).overlay.rows[0][1]).toBe("9007199254740993");
  });

  it("names an empty original and a NULL one in its tooltip", () => {
    const empty = draw([[1, "", false]], staged(editLabel(1, "x", ""))).overlay.marks[0]?.cells?.[1];
    expect(empty?.was).toBe("Was: (empty string)");
    const none = draw([[1, null, false]], staged(editLabel(1, "x", null as unknown as string))).overlay.marks[0]
      ?.cells?.[1];
    expect(none?.was).toBe("Was: NULL");
  });
});

describe("the overlay, beyond the plan", () => {
  it("starts over unless the rows are the old ones and a page, on the same key; the first of a key wins", () => {
    const first = makeRows(1, 3);
    const binding = bindRows(first, [0], null);
    const other = [[9, "row 9", false], ...first.slice(1)]; // same length and last row, another first row
    expect(bindRows(other, [0], binding).byKey.get(rowKeyFromKey({ id: 9 }, ["id"]))).toBe(0);
    const byLabel = bindRows(first, [1], binding);
    expect(byLabel.keyed).toBe(3);
    expect(byLabel.byKey.get(JSON.stringify(["row 2"]))).toBe(1);
    const repeated = bindRows([...first, [1, "again", true]], [0], null);
    expect(repeated.byKey.get(rowKeyFromKey({ id: 1 }, ["id"]))).toBe(0);
  });

  it("re-draws a cached row once its own change changes or it is marked for deletion", () => {
    const rows = makeRows(1, 3);
    const one = staged(editLabel(2, "two"));
    const first = draw(rows, one);
    const again = stage(one, editLabel(2, "deux"));
    const second = draw(rows, again, first.cache);
    expect(second.overlay.rows[1]).toEqual([2, "deux", false]);
    const marked = stage(again, { type: "deleteRows", rows: [ref(2)] });
    expect(draw(rows, marked, second.cache).overlay.marks[1]?.kind).toBe("deleted");
  });

  it("re-draws a reused change over a re-read row, new problems and the kinds once they arrive", () => {
    const rows = makeRows(1, 3);
    const state = staged(editLabel(2, "mine"), {
      type: "edit",
      cell: { row: ref(3), column: "flag", original: false },
      input: { kind: "value", text: "yes" },
    });
    const before = draw(rows, state);
    const reread = makeRows(1, 3);
    reread[1] = [2, "theirs", false];
    const after = draw(reread, state, before.cache);
    expect(after.overlay.marks[1]?.cells?.[1]?.conflict).toBe("Changed on the server: now theirs");
    const problems = stage(state, { type: "setProblems", problems: { "u:1": [{ column: "label", message: "No" }] } });
    expect(draw(reread, problems, after.cache).overlay.marks[1]?.cells?.[1]?.problem).toBe("No");
    // A restored staging is drawn before the edit target (and so the kinds) is known.
    const binding = bindRows(rows, [0], null);
    const input: OverlayInput = { rows, columns: COLUMNS, kinds: new Map(), staged: state, binding, result: RESULT };
    const unknown = overlay(input, null);
    expect(unknown.overlay.rows[2][2]).toBe("yes");
    const known = overlay({ ...input, kinds: KINDS }, unknown.cache);
    expect(known.overlay.rows[2][2]).toBe(true);
  });

  it("gives one new row object for one more staged cell among 1,000 rows", () => {
    const rows = makeRows(1, 1000);
    const one = staged(editLabel(10, "ten"), editLabel(500, "five hundred"));
    const first = draw(rows, one);
    const second = draw(rows, stage(one, editLabel(900, "nine hundred")), first.cache);
    const replaced = second.overlay.rows.filter((row, i) => row !== first.overlay.rows[i]);
    expect(replaced).toEqual([[900, "nine hundred", false]]);
    const remarked = second.overlay.marks.filter((mark, i) => mark !== first.overlay.marks[i]);
    expect(remarked).toHaveLength(1);
  });

  it("puts a key's problem on the key cell of an UPDATE that does not set it and of a DELETE", () => {
    const state = stage(staged(editLabel(1, "x"), { type: "deleteRows", rows: [ref(2)] }), {
      type: "setProblems",
      problems: {
        "u:1": [{ column: "id", message: "Key id cannot be matched" }],
        "d:2": [{ column: "id", message: "Key id cannot be matched" }],
      },
    });
    const { overlay: shown } = draw(makeRows(1, 2), state);
    expect(shown.marks[0]?.cells?.[0]).toEqual({ changed: false, problem: "Key id cannot be matched" });
    expect(shown.marks[0]?.cells?.[1]).toMatchObject({ changed: true, problem: undefined });
    expect(shown.marks[0]?.problem).toBeUndefined();
    expect(shown.marks[1]).toEqual({
      kind: "deleted",
      problem: undefined,
      cells: { 0: { changed: false, problem: "Key id cannot be matched" } },
    });
  });

  it("reads a column named like an Object property as the change's own entry only", () => {
    const columns: ColumnMeta[] = [
      { name: "id", type_name: "bigint", category: "number" },
      { name: "constructor", type_name: "text", category: "text" },
      { name: "toString", type_name: "text", category: "text" },
    ];
    const result = resultColumns(columns, ["id"]);
    const rows = [[1, "a", "b"]];
    const rowKey = rowKeyFromKey({ id: 1 }, ["id"]);
    // An UPDATE whose original lacks its column (a hand-made staging): NULL, not Object.prototype.toString.
    const update = { id: "u:x", key: { id: 1 }, set: { toString: { kind: "value", text: "c" } }, original: {}, seq: 1 };
    const state: StagedEdits = { ...staged({ type: "addRow", id: "i:1" }), updates: { [rowKey]: update as never } };
    const binding = bindRows(rows, [0], null);
    const { overlay: shown } = overlay({ rows, columns, kinds: new Map(), staged: state, binding, result }, null);
    const unset = { changed: false, isDefault: true, problem: undefined };
    expect(shown.marks[0]).toEqual({ kind: "new", problem: undefined, cells: { 0: unset, 1: unset, 2: unset } });
    expect(shown.rows[1]).toEqual([1, "a", "c"]);
    expect(shown.marks[1]?.cells?.[2]).toMatchObject({ was: "Was: NULL", conflict: "Changed on the server: now b" });
  });

  it("never places a row an older display does not have, after its binding grew in place", () => {
    const first = makeRows(1, 500);
    const binding = bindRows(first, [0], null);
    const state = staged(editLabel(700, "x"));
    const input: OverlayInput = { rows: first, columns: COLUMNS, kinds: KINDS, staged: state, binding, result: RESULT };
    const old = overlay(input, null);
    bindRows([...first, ...makeRows(501, 500), [7, "seven again", false]], [0], binding); // extends `binding` in place
    const again = overlay(input, null);
    expect(again.overlay.rows).toBe(first);
    expect(again.overlay.notShown).toBe(1);
    expect(again.overlay.marks).toHaveLength(500); // the repeat at index 1,000 is not one of these rows
    expect(displayIndexOf(old.overlay, state, binding, ref(700))).toBe(-1);
  });

  it("binds no change to a key two rows have, and marks those rows not editable", () => {
    const rows = [
      [1, "a", false],
      [2, "b", false],
      [1, "c", false],
    ];
    const binding = bindRows(rows, [0], null);
    expect([...binding.repeated]).toEqual([rowKeyFromKey({ id: 1 }, ["id"])]);
    expect(binding.repeatedRows).toEqual([0, 2]);
    // Editing the third row: its key is the first row's too, so the change is shown on neither (not "now a").
    const state = staged(editLabel(1, "x", "c"), editLabel(2, "y", "b"));
    const input: OverlayInput = { rows, columns: COLUMNS, kinds: KINDS, staged: state, binding, result: RESULT };
    const { overlay: shown } = overlay(input, null);
    expect(shown.rows[0]).toBe(rows[0]);
    expect(shown.rows[2]).toBe(rows[2]);
    expect(shown.rows[1]).toEqual([2, "y", false]);
    expect(shown.marks[0]).toBe(REPEATED_KEY_MARK);
    expect(shown.marks[2]).toBe(REPEATED_KEY_MARK);
    expect(shown.marks[1]?.kind).toBe("changed");
    expect(shown.notShown).toBe(1);
    expect(displayIndexOf(shown, state, binding, ref(1))).toBe(-1);
    expect(displayIndexOf(shown, state, binding, ref(2))).toBe(1);
    // With nothing staged, the rows stay the result's array and are still marked.
    const { overlay: plain } = overlay({ ...input, staged: null }, null);
    expect(plain.rows).toBe(rows);
    expect(plain.marks).toEqual([REPEATED_KEY_MARK, undefined, REPEATED_KEY_MARK]);
  });

  it("finds a key repeated by the page Load more appends, and keeps those of the first page", () => {
    const result = RESULT;
    const first = [...makeRows(1, 3), [3, "twice", false]];
    const binding = bindRows(first, [0], null);
    expect(binding.repeatedRows).toEqual([2, 3]);
    const grown = first.concat([[2, "again", true]]);
    const next = bindRows(grown, [0], binding);
    expect(next.keyed).toBe(1);
    expect([...next.repeated]).toEqual([rowKeyFromKey({ id: 3 }, ["id"]), rowKeyFromKey({ id: 2 }, ["id"])]);
    expect(next.repeatedRows).toEqual([2, 3, 1, 4]);
    const state = staged({ type: "addRow", id: "i:1" });
    const input: OverlayInput = { rows: grown, columns: COLUMNS, kinds: KINDS, staged: state, binding: next, result };
    const kinds = overlay(input, null).overlay.marks.map((mark) => mark?.kind);
    expect(kinds).toEqual(["new", undefined, "repeated_key", "repeated_key", "repeated_key", "repeated_key"]);
  });

  it("takes a binding of another array, or of another key, as no binding", () => {
    const rows = makeRows(1, 3);
    const reversed = makeRows(1, 3).reverse(); // the same length, other rows
    const state = staged(editLabel(1, "one"));
    const binding = bindRows(rows, [0], null);
    const input: OverlayInput = { rows, columns: COLUMNS, kinds: KINDS, staged: state, binding, result: RESULT };
    const { overlay: shown } = overlay({ ...input, rows: reversed }, null);
    expect(shown.rows).toBe(reversed);
    expect(shown.notShown).toBe(1);
    // Bound by `label`, whose cells here are the other row's ids: id 1's change must not land on id 2.
    const swapped = [
      [1, 2, false],
      [2, 1, false],
    ];
    const byLabel = bindRows(swapped, [1], null);
    const { overlay: byLabelShown } = overlay({ ...input, rows: swapped, binding: byLabel }, null);
    expect(byLabelShown).toMatchObject({ rows: swapped, notShown: 1 });
    expect(overlay(input, null).overlay.rows[0]).toEqual([1, "one", false]);
    // `bindRows` hands back a binding of the very array it was given, also for the same rows in another array.
    const copy = rows.slice();
    const again = bindRows(copy, [0], binding);
    expect(again.rows).toBe(copy);
    expect(again.keyed).toBe(0);
    expect(overlay({ ...input, rows: copy, binding: again }, null).overlay.rows[0]).toEqual([1, "one", false]);
  });

  it("keeps a cached row and shows a change the new page has, with a binding grown by Load more", () => {
    const first = makeRows(1, 500);
    const binding = bindRows(first, [0], null);
    const state = staged(editLabel(10, "ten"), editLabel(700, "seven hundred"));
    const input: OverlayInput = { rows: first, columns: COLUMNS, kinds: KINDS, staged: state, binding, result: RESULT };
    const before = overlay(input, null);
    expect(before.overlay.notShown).toBe(1);
    const grown = first.concat(makeRows(501, 500));
    const after = overlay({ ...input, rows: grown, binding: bindRows(grown, [0], binding) }, before.cache);
    expect(after.overlay.rows[9]).toBe(before.overlay.rows[9]);
    expect(after.overlay.marks[9]).toBe(before.overlay.marks[9]);
    expect(after.overlay.rows[699]).toEqual([700, "seven hundred", false]);
    expect(after.overlay.rows[600]).toBe(grown[600]);
    expect(after.overlay.notShown).toBe(0);
  });
});
