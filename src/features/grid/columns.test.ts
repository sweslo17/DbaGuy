import { describe, expect, it } from "vitest";
import type { ColumnMeta } from "@/lib/api/types";
import { autofitWidth, estimateWidths, KEY_ICON, KEY_ICON_SIZE, MIN_WIDTH, type TextMeasure } from "./columns";

/*
 * Column widths (spec ⑤ §3.6.2): a key column's header has an icon before its name, and the width the grid starts with
 * and the one autofit gives both leave room for it, so the name is not cut.
 */

/** 8 px per character in the headers and the cells, 6 in the type line: easy to add up by hand. */
const measure: TextMeasure = (text, font) => text.length * (font === "type" ? 6 : 8);
const LONG = "a_rather_long_column_name"; // 25 characters: 200 px
const COLUMNS: ColumnMeta[] = [
  { name: LONG, type_name: "bigint", category: "number" },
  { name: "id", type_name: "bigint", category: "number" },
  { name: "n", type_name: "text", category: "text" },
];
const ROWS: unknown[][] = [
  [1, 1, "x"],
  [2, 22, "a text much longer than the header"],
];
const NO_KEYS: ReadonlySet<number> = new Set();
const SORT_CONTROL = 24;
const PADDING = 17;

describe("estimateWidths", () => {
  it("gives a key column's header room for its icon, and only that column", () => {
    const plain = estimateWidths(COLUMNS, ROWS, measure, true, NO_KEYS);
    const keyed = estimateWidths(COLUMNS, ROWS, measure, true, new Set([0]));
    expect(plain[0]).toBe(200 + SORT_CONTROL + PADDING);
    expect(keyed[0] - plain[0]).toBe(KEY_ICON);
    expect(keyed.slice(1)).toEqual(plain.slice(1));
  });

  it("leaves a column alone when its rows are wider than the header", () => {
    const plain = estimateWidths(COLUMNS, ROWS, measure, true, NO_KEYS);
    const keyed = estimateWidths(COLUMNS, ROWS, measure, true, new Set([2]));
    expect(keyed).toEqual(plain); // "n" is 8 + 24 + 15 wide as a header, its cell 34 characters
  });

  it("counts the icon with or without the sort control, and never below the minimum width", () => {
    const unsortable = estimateWidths(COLUMNS, ROWS, measure, false, new Set([0]));
    expect(unsortable[0]).toBe(200 + KEY_ICON + PADDING);
    expect(estimateWidths(COLUMNS, [], measure, false, new Set([1]))[1]).toBe(MIN_WIDTH);
  });

  it("makes room for the icon the header draws and the gap after it", () => {
    expect(KEY_ICON).toBe(KEY_ICON_SIZE + 4);
  });
});

describe("autofitWidth", () => {
  it("fits a key column's header with its icon, as estimateWidths does", () => {
    const keyed = autofitWidth(COLUMNS[0], ROWS, 0, measure, true, true);
    const plain = autofitWidth(COLUMNS[0], ROWS, 0, measure, true, false);
    expect(plain).toBe(200 + SORT_CONTROL + PADDING);
    expect(keyed - plain).toBe(KEY_ICON);
    expect(keyed).toBe(estimateWidths(COLUMNS, ROWS, measure, true, new Set([0]))[0]);
  });

  it("follows the rows when they are wider than the header", () => {
    expect(autofitWidth(COLUMNS[2], ROWS, 2, measure, true, true)).toBe(
      autofitWidth(COLUMNS[2], ROWS, 2, measure, true, false),
    );
  });
});
