import { describe, expect, it } from "vitest";
import { MAX_VISIBLE, withToast, type ToastItem } from "./toastItems";

/* The toast stack: a toast about the same thing as one shown takes its place; any other is added, the oldest going. */

const item = (id: number, key?: string): ToastItem => ({ id, kind: "info", text: `t${id}`, ...(key ? { key } : {}) });

describe("a new toast", () => {
  it("takes the place of the one shown with its key, with its own id (its time anew)", () => {
    const shown = [item(1), item(2, "job:r1"), item(3)];
    expect(withToast(shown, item(4, "job:r1")).map((t) => [t.id, t.text])).toEqual([[1, "t1"], [4, "t4"], [3, "t3"]]);
  });

  it("is added after the others otherwise, the oldest going past the cap", () => {
    const full = Array.from({ length: MAX_VISIBLE }, (_, i) => item(i + 1));
    expect(withToast(full, item(9, "job:r1")).map((t) => t.id)).toEqual([2, 3, 4, 9]);
    expect(withToast([item(1, "job:r2")], item(2, "job:r1")).map((t) => t.id)).toEqual([1, 2]);
  });
});
