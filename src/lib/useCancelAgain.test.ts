import { describe, expect, it } from "vitest";
import { CANCEL_AGAIN_MS, seededCancel } from "./useCancelAgain";

/* A job's view opened again right after its Cancel keeps "Cancelling…" for the rest of the window (06b T6 fix 6). */

describe("the cancel a view opens with", () => {
  it("is still being sent within the window, and over after it", () => {
    expect(seededCancel(10_000, 2, 10_000 + CANCEL_AGAIN_MS - 1)).toEqual({ at: 10_000, warnings: 2 });
    expect(seededCancel(10_000, 2, 10_000 + CANCEL_AGAIN_MS)).toBeNull();
  });

  it("is none when no Cancel was sent, or the clock went back", () => {
    expect(seededCancel(null, 0, 10_000)).toBeNull();
    expect(seededCancel(10_000, 0, 9_000)).toBeNull();
  });
});
