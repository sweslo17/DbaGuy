import { describe, expect, it } from "vitest";
import { REPEATED_KEY, REPEATED_KEY_MARK } from "../edits/overlay";
import {
  CHANGED_CELL,
  DELETED_ROW,
  NEW_ROW,
  PROBLEM_CELL,
  REPEATED_ROW,
  cellBackground,
  cellTitle,
  headerNotes,
  keyColumnSignature,
  keyColumnsOf,
  rowLabel,
  rowTint,
  rowTitle,
  type CellMark,
  type RowMark,
} from "./marks";

/*
 * What the grid draws for a staged change (spec ⑤ §3.6.2), as pure functions: one background per cell, opaque for a
 * pinned cell, the row labels, the tooltips.
 */

const KINDS: RowMark["kind"][] = ["new", "changed", "deleted", "repeated_key"];
const CHANGED: CellMark = { changed: true, was: "Was: 1" };
const UNCHANGED: CellMark = { changed: false };
const classesOf = (value: string | undefined) => (value ?? "").split(" ").filter(Boolean);
const backgrounds = (value: string | undefined) => classesOf(value).filter((name) => name.startsWith("bg-"));

describe("rowLabel", () => {
  it("numbers the loaded rows from after the new rows on top", () => {
    expect(rowLabel(undefined, 0, 0)).toBe("1");
    expect(rowLabel(undefined, 41, 0)).toBe("42");
    expect(rowLabel(undefined, 2, 2)).toBe("1");
    expect(rowLabel({ kind: "changed" }, 5, 2)).toBe("4");
  });

  it("labels a new row + and a row marked for deletion −, whatever its place", () => {
    expect(rowLabel({ kind: "new" }, 0, 3)).toBe("+");
    expect(rowLabel({ kind: "new" }, 2, 3)).toBe("+");
    expect(rowLabel({ kind: "deleted" }, 7, 0)).toBe("−");
    expect(rowLabel({ kind: "deleted" }, 7, 3)).toBe("−");
  });

  it("numbers a row that cannot be edited like any other", () => {
    expect(rowLabel(REPEATED_KEY_MARK, 9, 1)).toBe("9");
  });
});

describe("rowTint", () => {
  it("tints new rows, rows marked for deletion and rows that cannot be edited, not changed ones", () => {
    expect(rowTint(undefined)).toBeUndefined();
    expect(rowTint({ kind: "new" })).toBe(NEW_ROW);
    expect(rowTint({ kind: "deleted" })).toBe(DELETED_ROW);
    expect(rowTint({ kind: "changed" })).toBeUndefined(); // only its changed cells are tinted
    expect(rowTint(REPEATED_KEY_MARK)).toBe(REPEATED_ROW);
  });

  it("strikes through only a deleted row, and draws a repeated key without the error colour", () => {
    for (const kind of KINDS) {
      expect(classesOf(rowTint({ kind })).includes("line-through")).toBe(kind === "deleted");
    }
    expect(REPEATED_ROW).not.toContain("danger");
    expect(REPEATED_ROW).toContain("text-muted");
  });
});

describe("cellBackground", () => {
  it("gives a cell one background: a changed cell's, else its row's, else a pinned cell's opaque one", () => {
    expect(cellBackground(CHANGED, undefined, false)).toBe(CHANGED_CELL);
    expect(cellBackground(CHANGED, NEW_ROW, true)).toBe(CHANGED_CELL);
    expect(cellBackground(UNCHANGED, NEW_ROW, false)).toBe(NEW_ROW);
    expect(cellBackground(undefined, DELETED_ROW, true)).toBe(DELETED_ROW);
    expect(cellBackground(UNCHANGED, undefined, true)).toBe("bg-bg");
    expect(cellBackground(UNCHANGED, undefined, false)).toBeUndefined();
    expect(cellBackground(undefined, undefined, false)).toBeUndefined();
  });

  it("never gives two backgrounds (there is no tailwind-merge), and a pinned cell always an opaque one", () => {
    for (const kind of [...KINDS, undefined]) {
      for (const mark of [undefined, UNCHANGED, CHANGED, { changed: false, isDefault: true }]) {
        for (const pinned of [false, true]) {
          const background = cellBackground(mark, rowTint(kind && { kind }), pinned);
          expect(backgrounds(background).length).toBeLessThanOrEqual(1);
          if (!pinned) continue;
          // Opaque: the page background itself, or a tint mixed into it (never a translucent colour over what scrolls under).
          const [only] = backgrounds(background);
          expect(only === "bg-bg" || only?.includes("var(--color-bg))]")).toBe(true);
        }
      }
    }
  });

  it("uses colour tokens only", () => {
    for (const value of [CHANGED_CELL, NEW_ROW, DELETED_ROW, REPEATED_ROW, PROBLEM_CELL]) {
      expect(value).not.toMatch(/#[0-9a-f]{3}|rgb|hsl|oklch\(/i);
      expect(value).toMatch(/var\(--color-|outline-danger/);
    }
  });
});

describe("rowTitle", () => {
  it("shows why the row's change was refused, else why the row cannot be edited", () => {
    expect(rowTitle(undefined)).toBeUndefined();
    expect(rowTitle({ kind: "new" })).toBeUndefined();
    expect(rowTitle({ kind: "changed", problem: "Not a number" })).toBe("Not a number");
    expect(rowTitle(REPEATED_KEY_MARK)).toBe(REPEATED_KEY);
    expect(rowTitle({ kind: "repeated_key", problem: "p", note: "n" })).toBe("p");
  });

  it("does not make a repeated key a problem (a problem turns the row number red)", () => {
    expect(REPEATED_KEY_MARK.problem).toBeUndefined();
  });
});

describe("cellTitle", () => {
  it("adds what the mark says under the value, one line each", () => {
    expect(cellTitle("empty string", undefined)).toBe("empty string");
    expect(cellTitle(undefined, undefined)).toBeUndefined();
    expect(cellTitle(undefined, UNCHANGED)).toBeUndefined();
    expect(cellTitle("v", { changed: true, was: "Was: 1" })).toBe("v\nWas: 1");
    expect(cellTitle(undefined, { changed: true, was: "Was: 1", conflict: "Changed on the server: now 2" })).toBe(
      "Was: 1\nChanged on the server: now 2",
    );
    expect(cellTitle("v", { changed: true, was: "Was: 1", conflict: "c", problem: "p" })).toBe("v\nWas: 1\nc\np");
  });
});

describe("headerNotes", () => {
  it("names a key column and why a column cannot be written", () => {
    expect(headerNotes(undefined)).toEqual([]);
    expect(headerNotes({ key: false, readOnly: null })).toEqual([]);
    expect(headerNotes({ key: true, readOnly: null })).toEqual(["Key column"]);
    expect(headerNotes({ key: false, readOnly: "generated column" })).toEqual(["Read-only: generated column"]);
    expect(headerNotes({ key: true, readOnly: "identity" })).toEqual(["Key column", "Read-only: identity"]);
  });
});

describe("keyColumnSignature", () => {
  const key = { key: true, readOnly: null };
  const plain = { key: false, readOnly: "generated" };

  it("is the key columns' indexes, empty when there are none", () => {
    expect(keyColumnSignature(undefined)).toBe("");
    expect(keyColumnSignature([])).toBe("");
    expect(keyColumnSignature([plain, undefined])).toBe("");
    expect(keyColumnSignature([key, plain, key, undefined, key])).toBe("0,2,4");
  });

  it("is the same for another array of marks with the same keys", () => {
    expect(keyColumnSignature([key, plain])).toBe(keyColumnSignature([{ ...key }, undefined]));
  });

  it("reads back to the set of columns", () => {
    expect([...keyColumnsOf("")]).toEqual([]);
    expect([...keyColumnsOf("3")]).toEqual([3]);
    expect([...keyColumnsOf(keyColumnSignature([key, plain, key]))]).toEqual([0, 2]);
  });
});
