import { afterEach, describe, expect, it, vi } from "vitest";
import { countRowRender } from "./renderCount";

/* The dev-only counter of rows rendered (spec ⑤ §7, Q1): off unless the page sets the array, and off in production. */

afterEach(() => {
  delete globalThis.__gridRowRenders;
  vi.unstubAllEnvs();
});

describe("countRowRender", () => {
  it("does nothing until the page sets the array", () => {
    expect(() => countRowRender(3)).not.toThrow();
    expect(globalThis.__gridRowRenders).toBeUndefined();
  });

  it("appends the index of each row rendered", () => {
    globalThis.__gridRowRenders = [];
    countRowRender(3);
    countRowRender(4);
    countRowRender(3);
    expect(globalThis.__gridRowRenders).toEqual([3, 4, 3]);
  });

  it("counts nothing in a production build", () => {
    vi.stubEnv("DEV", false);
    globalThis.__gridRowRenders = [];
    countRowRender(3);
    expect(globalThis.__gridRowRenders).toEqual([]);
  });
});
