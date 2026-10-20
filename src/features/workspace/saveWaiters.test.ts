import { describe, expect, it } from "vitest";
import { SaveWaiters } from "./saveWaiters";

/*
 * A grid commit waits for the save of its "applying" staging before it sends the apply (05b pre-flight H1): only a save
 * that started after the commit asked holds that state.
 */

/** Whether `promise` has settled, and with what, after the pending microtasks ran. */
async function settled<T>(promise: Promise<T>): Promise<{ value: T } | "pending"> {
  const pending = Symbol("pending");
  const value = await Promise.race([promise, Promise.resolve().then(() => pending)]);
  return value === pending ? "pending" : { value: value as T };
}

describe("waiting for a save of the workspace as it is now", () => {
  it("is answered by the next save that starts and ends", async () => {
    const waiters = new SaveWaiters();
    const saved = waiters.wait();
    const save = waiters.start();
    expect(await settled(saved)).toBe("pending");
    waiters.ended(save, true);
    expect(await settled(saved)).toEqual({ value: true });
  });

  it("is not answered by a save that was already running when it asked: that one holds the state from before", async () => {
    const waiters = new SaveWaiters();
    const running = waiters.start();
    const saved = waiters.wait();
    waiters.ended(running, true);
    expect(await settled(saved)).toBe("pending");
    const next = waiters.start();
    waiters.ended(next, true);
    expect(await settled(saved)).toEqual({ value: true });
  });

  it("says when that save failed, and answers each waiter once", async () => {
    const waiters = new SaveWaiters();
    const first = waiters.wait();
    const second = waiters.wait();
    const save = waiters.start();
    const later = waiters.wait(); // asked while it ran
    waiters.ended(save, false);
    expect([await settled(first), await settled(second), await settled(later)]).toEqual([
      { value: false },
      { value: false },
      "pending",
    ]);
    const retry = waiters.start();
    waiters.ended(retry, true);
    expect(await settled(later)).toEqual({ value: true });
    waiters.ended(waiters.start(), false); // nobody waits any more: nothing to answer
    expect(await settled(first)).toEqual({ value: false });
  });
});
