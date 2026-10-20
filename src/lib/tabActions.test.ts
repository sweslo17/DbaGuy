import { describe, expect, it, vi } from "vitest";
import { createTabActionRegistry, type TabAction } from "./tabActions";

/* The palette's registry of a mounted tab's own handlers (ruling R-02c-15), one per kind of tab. */

type Id = "run" | "stop";
const actions = (enabled: boolean, run = vi.fn()): Record<Id, TabAction> => ({
  run: { enabled, run },
  stop: { enabled: !enabled, run: vi.fn() },
});

describe("a tab action registry", () => {
  it("has nothing for a tab that is not mounted", () => {
    expect(createTabActionRegistry<Id>().read("t1")).toBeNull();
  });

  it("reads the tab's actions as they are when asked", () => {
    const registry = createTabActionRegistry<Id>();
    let now = actions(false);
    registry.register("t1", () => now);
    expect(registry.read("t1")?.run.enabled).toBe(false);
    now = actions(true);
    expect(registry.read("t1")?.run.enabled).toBe(true);
    registry.read("t1")?.run.run();
    expect(now.run.run).toHaveBeenCalledTimes(1);
  });

  it("removes only its own registration: a tab mounted again since stays", () => {
    const registry = createTabActionRegistry<Id>();
    const first = actions(false);
    const second = actions(true);
    const removeFirst = registry.register("t1", () => first);
    const removeSecond = registry.register("t1", () => second);
    removeFirst();
    expect(registry.read("t1")).toBe(second);
    removeSecond();
    expect(registry.read("t1")).toBeNull();
  });

  it("keeps each tab, and each kind of tab, apart", () => {
    const queryTabs = createTabActionRegistry<Id>();
    const tableTabs = createTabActionRegistry<Id>();
    const a = actions(true);
    queryTabs.register("t1", () => a);
    expect(queryTabs.read("t2")).toBeNull();
    expect(tableTabs.read("t1")).toBeNull();
  });
});
