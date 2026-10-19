import { describe, expect, it } from "vitest";
import type { ChangeCounts, DocumentChange } from "@/lib/api/types";
import { summarizePreview } from "./previewSummary";

/*
 * The preview's first line (B2): `counts` cannot tell a number that loses digits from one that is only respelled, so the
 * value changes are counted from the listed changes and put first, and never counted twice.
 */

const change = (kind: DocumentChange["kind"], valueChanged = false, path = "$"): DocumentChange => ({
  path,
  kind,
  before: "b",
  after: "a",
  value_changed: valueChanged,
});
const counts = (some: Partial<ChangeCounts>): ChangeCounts => ({ key_order: 0, number: 0, duplicate_key: 0, other: 0, ...some });

describe("the summary of a preview", () => {
  it("names each kind that changed, and no value change when none of the listed changes alters a value", () => {
    const summary = summarizePreview({
      changes: [change("key_order"), change("key_order", false, "$.a"), change("number")],
      counts: counts({ key_order: 2, number: 1 }),
    });
    expect(summary).toEqual({ values: null, others: ["2 keys reordered", "1 number written differently"] });
  });

  it("says one thing at a time: a single key, a single number, duplicates and the rest", () => {
    const summary = summarizePreview({
      changes: [change("key_order"), change("number"), change("duplicate_key"), change("other")],
      counts: counts({ key_order: 1, number: 1, duplicate_key: 1, other: 1 }),
    });
    expect(summary.others).toEqual(["1 key reordered", "1 number written differently", "1 duplicate key", "1 other change"]);
  });

  it("leads with a value change among others, and does not count it again under its kind", () => {
    // $.pi 3.14159…279 → 3.141592653589793, with the key order and a respelled number besides.
    const summary = summarizePreview({
      changes: [change("number", true, "$.pi"), change("key_order"), change("number", false, "$.a")],
      counts: counts({ key_order: 1, number: 2 }),
    });
    expect(summary).toEqual({ values: "1 value changes", others: ["1 key reordered", "1 number written differently"] });
  });

  it("leaves a kind out once all of its changes alter values", () => {
    const summary = summarizePreview({
      changes: [change("number", true, "$.a"), change("number", true, "$.b")],
      counts: counts({ number: 2 }),
    });
    expect(summary).toEqual({ values: "2 values change", others: [] });
  });

  it("counts value changes of any kind (a duplicate key loses the first value)", () => {
    const summary = summarizePreview({
      changes: [change("duplicate_key", true), change("number", true), change("other")],
      counts: counts({ duplicate_key: 1, number: 1, other: 1 }),
    });
    expect(summary).toEqual({ values: "2 values change", others: ["1 other change"] });
  });

  it("says at least when every listed change alters a value and more were found than are listed", () => {
    const listed = Array.from({ length: 3 }, (_, index) => change("number", true, `$.n${index}`));
    const summary = summarizePreview({ changes: listed, counts: counts({ number: 250 }) });
    expect(summary.values).toBe("at least 3 values change");
    expect(summary.others).toEqual(["247 numbers written differently"]);
  });

  it("says at least for any incomplete list: an unlisted change may be a value change of a kind that hit its cap", () => {
    const summary = summarizePreview({
      changes: [change("number", true), change("key_order")],
      counts: counts({ number: 150, key_order: 1 }),
    });
    expect(summary.values).toBe("at least 1 value changes");
  });

  it("is exact when the list is complete, however long", () => {
    const listed = Array.from({ length: 200 }, (_, index) => change("number", true, `$.n${index}`));
    expect(summarizePreview({ changes: listed, counts: counts({ number: 200 }) }).values).toBe("200 values change");
  });
});
