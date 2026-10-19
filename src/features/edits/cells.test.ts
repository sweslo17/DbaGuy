import { describe, expect, it } from "vitest";
import { displayCell, editText, inputMatches, sameCell } from "./cells";

/* Staged values against loaded cells (spec ⑤ R-05-8, D-05-10): text as typed, NULL ≠ "", bytes as hex. */

const bytes = (size: number, preview: string) => ({ $type: "bytes", size, preview });
const cut = { $type: "text", preview: "abc", size: 70_000, truncated: true };

describe("the text an editor starts with", () => {
  it.each([
    [null, "", true],
    ["", "", true],
    [42, "42", true],
    ["9007199254740993", "9007199254740993", true],
    [true, "true", true],
    [bytes(2, "dead"), "0xdead", true],
    [bytes(100, "ab".repeat(64)), `0x${"ab".repeat(64)}`, false],
    [cut, "abc", false],
  ])("%j → %j (complete: %s)", (cell, text, complete) => {
    expect(editText(cell)).toEqual({ text, complete });
  });
});

describe("whether a staged value changes the loaded one", () => {
  it("NULL matches NULL only; the empty string matches the empty string only", () => {
    expect(inputMatches({ kind: "null" }, null)).toBe(true);
    expect(inputMatches({ kind: "null" }, "")).toBe(false);
    expect(inputMatches({ kind: "value", text: "" }, "")).toBe(true);
    expect(inputMatches({ kind: "value", text: "" }, null)).toBe(false);
  });

  it("compares the text the editor started with, never a preview", () => {
    expect(inputMatches({ kind: "value", text: "42" }, 42)).toBe(true);
    expect(inputMatches({ kind: "value", text: "42.0" }, 42)).toBe(false);
    expect(inputMatches({ kind: "value", text: "abc" }, cut)).toBe(false);
    expect(inputMatches({ kind: "value", text: "0xdead" }, bytes(2, "dead"))).toBe(true);
    expect(inputMatches({ kind: "default" }, null)).toBe(false);
  });

  it("reads a bool's words and hex's case as the backend does, with the column's kind", () => {
    const typed = (text: string) => ({ kind: "value", text }) as const;
    for (const word of ["yes", "t", "1", "TRUE", " Yes "]) expect(inputMatches(typed(word), true, "bool")).toBe(true);
    for (const word of ["no", "f", "0", "false"]) expect(inputMatches(typed(word), false, "bool")).toBe(true);
    expect(inputMatches(typed("no"), true, "bool")).toBe(false);
    expect(inputMatches(typed("maybe"), true, "bool")).toBe(false);
    expect(inputMatches(typed("yes"), 1, "bool")).toBe(true); // SQLite keeps a BOOLEAN as an integer
    expect(inputMatches(typed("yes"), 2, "bool")).toBe(false);
    expect(inputMatches(typed("maybe"), 2, "bool")).toBe(false); // neither is a bool: the text decides
    expect(inputMatches(typed("0xDEADBEEF"), bytes(4, "deadbeef"), "binary")).toBe(true);
    expect(inputMatches(typed("DeadBeef"), bytes(4, "deadbeef"), "binary")).toBe(true);
    expect(inputMatches(typed("0xdeadbe"), bytes(4, "deadbeef"), "binary")).toBe(false);
    expect(inputMatches(typed("0xdeadbeef"), bytes(100, "deadbeef".repeat(16)), "binary")).toBe(false); // a preview
    // without the kind, and for every other kind, the text decides: `1.0` and `1` may differ on the server
    expect(inputMatches(typed("yes"), true)).toBe(false);
    expect(inputMatches(typed("0xDEADBEEF"), bytes(4, "deadbeef"))).toBe(false);
    expect(inputMatches(typed("1.0"), 1, "number")).toBe(false);
    expect(inputMatches(typed("1"), true, "text")).toBe(false);
  });
});

describe("cells read again", () => {
  it("are the same when they carry the same value", () => {
    expect(sameCell(bytes(2, "dead"), bytes(2, "dead"))).toBe(true);
    expect(sameCell(bytes(2, "dead"), bytes(2, "beef"))).toBe(false);
    expect(sameCell(null, undefined)).toBe(true);
    expect(sameCell("1", 1)).toBe(false);
  });
});

describe("a staged value as the grid draws it", () => {
  it("keeps text, numbers' digits included, as a string", () => {
    expect(displayCell({ kind: "value", text: "9007199254740993" }, "number")).toBe("9007199254740993");
    expect(displayCell({ kind: "value", text: '{"a": 1.10}' }, "json")).toBe('{"a": 1.10}');
  });

  it("reads bools and hex as the backend will", () => {
    expect(displayCell({ kind: "value", text: " Yes " }, "bool")).toBe(true);
    expect(displayCell({ kind: "value", text: "f" }, "bool")).toBe(false);
    expect(displayCell({ kind: "value", text: "maybe" }, "bool")).toBe("maybe");
    expect(displayCell({ kind: "value", text: "0xDEAD" }, "binary")).toEqual(bytes(2, "dead"));
    expect(displayCell({ kind: "value", text: "abc" }, "binary")).toBe("abc");
    expect(displayCell({ kind: "value", text: "ab".repeat(100) }, "binary")).toEqual(bytes(100, "ab".repeat(64)));
  });

  it("draws NULL and DEFAULT as null", () => {
    expect(displayCell({ kind: "null" }, "text")).toBeNull();
    expect(displayCell({ kind: "default" }, "number")).toBeNull();
  });
});
