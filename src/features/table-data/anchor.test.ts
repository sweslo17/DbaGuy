import { describe, expect, it } from "vitest";
import type { ColumnMeta } from "@/lib/api/types";
import { resultColumns } from "../edits/overlay";
import { selectAll, selectCell, selectColumns, selectRows, type GridSelection } from "../grid/selection";
import { captureAnchor, placeAnchor, type Anchor, type AnchorInput } from "./anchor";

/*
 * A re-run of the same browse keeps the grid's place by key (05b K14): the view's row at the same offset on screen and
 * the selection on the same row, however the rows moved; a key that is gone keeps the position (Task 6's rule).
 */

const RH = 22;
const VIEWPORT = 440; // 20 rows
const COLUMNS: ColumnMeta[] = [
  { name: "id", type_name: "bigint", category: "number" },
  { name: "label", type_name: "text", category: "text" },
];
const KEY = resultColumns(COLUMNS, ["id"]).keyIndexes!;

const rowsOf = (ids: readonly number[]): unknown[][] => ids.map((id) => [id, `row ${id}`]);
const range = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => from + i);
const ROWS = rowsOf(range(1, 1_200)); // row index i holds id i + 1

function capture(fields: Partial<AnchorInput>): Anchor {
  const input: AnchorInput = {
    rows: ROWS,
    keyIndexes: KEY,
    selection: null,
    scrollTop: 0,
    viewportHeight: VIEWPORT,
    rowHeight: RH,
    leading: 0,
    ...fields,
  };
  const anchor = captureAnchor(input);
  expect(anchor).not.toBeNull();
  return anchor!;
}

const place = (anchor: Anchor, rows: unknown[][], leading = 0, maxScrollTop = 1e9, keyIndexes = KEY) =>
  placeAnchor(anchor, { rows, keyIndexes, rowHeight: RH, leading, maxScrollTop });

/** The selected row 900 (id 901), shown 5 rows below the viewport's top. */
const AT_900 = { selection: selectCell({ row: 900, col: 1 }), scrollTop: 895 * RH };

describe("the grid's place by key", () => {
  it("follows its row up when three rows above it were deleted", () => {
    const anchor = capture(AT_900);
    expect(anchor.view?.offset).toBe(5 * RH);
    const rows = rowsOf(range(1, 1_200).filter((id) => id < 100 || id > 102)); // ids 100–102 gone
    expect(place(anchor, rows)).toEqual({ scrollTop: 892 * RH, selection: selectCell({ row: 897, col: 1 }) });
  });

  it("follows its row down when rows were added above it", () => {
    const anchor = capture(AT_900);
    const rows = rowsOf([-3, -2, -1, 0, ...range(1, 1_200)]); // four new rows sort first
    expect(place(anchor, rows)).toEqual({ scrollTop: 899 * RH, selection: selectCell({ row: 904, col: 1 }) });
  });

  it("follows a row that moved far (PostgreSQL's heap order after an UPDATE)", () => {
    const anchor = capture(AT_900);
    const rows = rowsOf([...range(1, 1_200).filter((id) => id !== 901), 901]);
    expect(place(anchor, rows)).toEqual({ scrollTop: 1_194 * RH, selection: selectCell({ row: 1_199, col: 1 }) });
  });

  it("keeps the position when the row is gone (deleted, or no longer among the rows loaded)", () => {
    const anchor = capture(AT_900);
    expect(place(anchor, rowsOf(range(1, 1_200).filter((id) => id !== 901)))).toEqual({
      scrollTop: null,
      selection: null,
    });
    expect(place(anchor, rowsOf(range(1, 500)))).toEqual({ scrollTop: null, selection: null });
  });

  it("anchors the view on the first row in view when nothing is selected", () => {
    const anchor = capture({ scrollTop: 300.5 * RH }); // row 300 half under the header
    expect(anchor).toEqual({ view: { key: "[301]", offset: -0.5 * RH }, selection: null });
    expect(place(anchor, rowsOf(range(4, 1_200)))).toEqual({ scrollTop: 297.5 * RH, selection: null });
  });

  it("anchors the view on the first row in view when the selection is out of view, and still moves the selection", () => {
    const anchor = capture({ selection: selectCell({ row: 10, col: 0 }), scrollTop: 600 * RH });
    expect(anchor.view).toEqual({ key: "[601]", offset: 0 });
    const rows = rowsOf(range(1, 1_200).filter((id) => id < 300 || id > 302)); // below the selection, above the view
    expect(place(anchor, rows)).toEqual({ scrollTop: 597 * RH, selection: selectCell({ row: 10, col: 0 }) });
    expect(place(anchor, rowsOf(range(3, 1_200)))).toEqual({
      scrollTop: 598 * RH,
      selection: selectCell({ row: 8, col: 0 }),
    });
  });

  it("moves a range of cells or rows with its active row, and leaves whole columns and everything alone", () => {
    const cells: GridSelection = { anchor: { row: 905, col: 0 }, focus: { row: 900, col: 1 }, mode: "cells" };
    const deleted = rowsOf(range(4, 1_200));
    expect(place(capture({ ...AT_900, selection: cells }), deleted).selection).toEqual({
      anchor: { row: 902, col: 0 },
      focus: { row: 897, col: 1 },
      mode: "cells",
    });
    expect(place(capture({ ...AT_900, selection: selectRows(902, 900) }), deleted).selection).toEqual(
      selectRows(899, 897),
    );
    for (const selection of [selectColumns(0, 1), selectAll({ row: 900, col: 1 })]) {
      const anchor = capture({ ...AT_900, selection });
      expect(anchor.selection).toBeNull();
      expect(place(anchor, deleted)).toEqual({ scrollTop: 892 * RH, selection: null }); // the view still follows
    }
  });

  it("keeps the view inside the content", () => {
    // Row 5 shown 5 rows down from the top; three rows above it deleted: it cannot stay 5 rows down
    const top = capture({ selection: selectCell({ row: 5, col: 0 }), scrollTop: 0 });
    expect(place(top, rowsOf(range(4, 1_200)))).toEqual({ scrollTop: 0, selection: selectCell({ row: 2, col: 0 }) });
    // The last row, one row added above it: the content ends before the place it would need
    const bottom = capture({ selection: selectCell({ row: 1_199, col: 0 }), scrollTop: 1_190 * RH });
    expect(place(bottom, rowsOf([0, ...range(1, 1_200)]), 0, 1_185 * RH).scrollTop).toBe(1_185 * RH);
  });

  it("finds a composite key", () => {
    const columns: ColumnMeta[] = [
      { name: "region", type_name: "text", category: "text" },
      { name: "note", type_name: "text", category: "text" },
      { name: "n", type_name: "int", category: "number" },
    ];
    const key = resultColumns(columns, ["n", "region"]).keyIndexes!;
    expect(key).toEqual([2, 0]);
    const rows = ["eu", "us"].flatMap((region) => range(1, 50).map((n) => [region, `${region} ${n}`, n]));
    const input = { rows, keyIndexes: key, viewportHeight: VIEWPORT, rowHeight: RH, leading: 0 };
    const anchor = captureAnchor({ ...input, selection: selectCell({ row: 60, col: 1 }), scrollTop: 55 * RH })!;
    expect(anchor.selection?.key).toBe('[11,"us"]');
    const moved = [...rows.slice(0, 10), ...rows.slice(11, 30), ...rows.slice(31)]; // two rows above it deleted
    expect(place(anchor, moved, 0, 1e9, key)).toEqual({
      scrollTop: 53 * RH,
      selection: selectCell({ row: 58, col: 1 }),
    });
  });

  it("finds a key cell that holds null, and tells it from the text 'null'", () => {
    const rows = [[null, "a"], ["null", "b"], [1, "c"]];
    const anchor = captureAnchor({
      rows,
      keyIndexes: [0],
      selection: selectCell({ row: 0, col: 1 }),
      scrollTop: 0,
      viewportHeight: VIEWPORT,
      rowHeight: RH,
      leading: 0,
    })!;
    expect(anchor.selection?.key).toBe("[null]");
    const placeIn = (moved: unknown[][]) => place(anchor, moved, 0, 1e9, [0]).selection;
    expect(placeIn([["null", "b"], [1, "c"], [null, "a"]])).toEqual(selectCell({ row: 2, col: 1 }));
    expect(placeIn([["null", "b"], [1, "c"]])).toBeNull();
  });

  it("reads a key column named constructor", () => {
    const columns: ColumnMeta[] = [
      { name: "label", type_name: "text", category: "text" },
      { name: "constructor", type_name: "int", category: "number" },
    ];
    const key = resultColumns(columns, ["constructor"]).keyIndexes!;
    expect(key).toEqual([1]);
    const rows = range(1, 30).map((n) => [`row ${n}`, n]);
    const anchor = captureAnchor({
      rows,
      keyIndexes: key,
      selection: selectCell({ row: 10, col: 0 }),
      scrollTop: 2 * RH,
      viewportHeight: VIEWPORT,
      rowHeight: RH,
      leading: 0,
    })!;
    expect(anchor.selection?.key).toBe("[11]");
    const moved = rows.slice(1);
    expect(place(anchor, moved, 0, 1e9, key)).toEqual({
      scrollTop: RH,
      selection: selectCell({ row: 9, col: 0 }),
    });
  });

  it("counts new rows on top: they have no key, and the loaded rows come after them", () => {
    // Two new rows on top, the selection on loaded row 100 (display 102), in view.
    const anchor = capture({ selection: selectCell({ row: 102, col: 1 }), scrollTop: 95 * RH, leading: 2 });
    expect(anchor.selection?.key).toBe("[101]");
    expect(place(anchor, rowsOf(range(4, 1_200)), 2)).toEqual({
      scrollTop: 92 * RH,
      selection: selectCell({ row: 99, col: 1 }),
    });
    // A new row selected: nothing places it but its own position; the view anchors on the first loaded row.
    const onNew = capture({ selection: selectCell({ row: 0, col: 1 }), scrollTop: 0, leading: 2 });
    expect(onNew).toEqual({ view: { key: "[1]", offset: 2 * RH }, selection: null });
  });

  it("has nothing to anchor on without rows", () => {
    const empty = { rows: [], keyIndexes: KEY, selection: null, scrollTop: 0, viewportHeight: 1, rowHeight: RH };
    expect(captureAnchor({ ...empty, leading: 0 })).toBeNull();
    expect(captureAnchor({ ...empty, leading: 2 })).toBeNull(); // only new rows: none of them has a key
  });

  it("places 10,000 rows within a few milliseconds", () => {
    const rows = rowsOf(range(1, 10_000));
    const anchor = capture({ rows, selection: selectCell({ row: 9_000, col: 1 }), scrollTop: 8_990 * RH });
    const moved = rowsOf(range(1, 10_000).filter((id) => id !== 9_001)); // the worst case: one pass, nothing found
    const runs = Array.from({ length: 5 }, () => {
      const started = performance.now();
      expect(place(anchor, moved)).toEqual({ scrollTop: null, selection: null });
      return performance.now() - started;
    });
    expect(Math.min(...runs)).toBeLessThan(10); // about 1.5 ms measured
  });
});
