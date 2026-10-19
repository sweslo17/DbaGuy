import { afterEach, describe, expect, it, vi } from "vitest";
import { changeCount, MAX_CHANGES, rowKeyFromKey } from "@/features/edits/staging";
import {
  dispatchStaged,
  forgetStagedEditsExcept,
  onStagedEditsChanged,
  putStagedEdits,
  readStagedEdits,
} from "./stagedEdits";

/* The staged edits by tab (spec ⑤ §3.6.1): the D-05-11 cap, the key they were staged on, the writer's notification. */

const rowRef = (id: number) => ({ kind: "row", rowKey: rowKeyFromKey({ id }, ["id"]), key: { id } }) as const;
const rows = (from: number, count: number) => Array.from({ length: count }, (_, i) => rowRef(from + i));

afterEach(() => forgetStagedEditsExcept(new Set()));

describe("dispatching", () => {
  it("stages on the table's key and notifies the workspace writer", () => {
    const listener = vi.fn();
    const stop = onStagedEditsChanged(listener);
    expect(dispatchStaged("t1", { type: "deleteRows", rows: [rowRef(1)] }, ["id"])).toBe("staged");
    expect(readStagedEdits("t1")?.keyColumns).toEqual(["id"]);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(dispatchStaged("t1", { type: "restoreRows", rows: [rowRef(9)] }, ["id"])).toBe("unchanged");
    expect(listener).toHaveBeenCalledTimes(1);
    stop();
  });

  it(`refuses an action that would stage more than ${MAX_CHANGES.toLocaleString()} changes, and keeps what was staged`, () => {
    expect(dispatchStaged("t1", { type: "deleteRows", rows: rows(0, MAX_CHANGES) }, ["id"])).toBe("staged");
    expect(dispatchStaged("t1", { type: "deleteRows", rows: rows(MAX_CHANGES, 1) }, ["id"])).toBe("too_many");
    expect(changeCount(readStagedEdits("t1")!)).toBe(MAX_CHANGES);
    // Taking changes away is always possible.
    expect(dispatchStaged("t1", { type: "restoreRows", rows: rows(0, 1) }, ["id"])).toBe("staged");
  });

  it("stages nothing new on changes made on another key, but still reverts and undoes them", () => {
    dispatchStaged("t1", { type: "deleteRows", rows: [rowRef(1)] }, ["id"]);
    expect(dispatchStaged("t1", { type: "deleteRows", rows: [rowRef(2)] }, ["code"])).toBe("key_changed");
    expect(dispatchStaged("t1", { type: "undo" }, ["code"])).toBe("staged");
    // Nothing staged any more: the table's key is taken.
    expect(dispatchStaged("t1", { type: "addRow", id: "i:1" }, ["code"])).toBe("staged");
    expect(readStagedEdits("t1")?.keyColumns).toEqual(["code"]);
  });

  it("leaves a tab with nothing staged alone without a key (an outcome, a refresh)", () => {
    expect(dispatchStaged("t2", { type: "refreshed" })).toBe("unchanged");
    expect(readStagedEdits("t2")).toBeNull();
  });

  it("leaves it alone for an apply too: there is nothing to send", () => {
    expect(dispatchStaged("t2", { type: "applying" })).toBe("unchanged");
    expect(readStagedEdits("t2")).toBeNull();
  });

  it("restores rows marked on another key (⌘⌫ over marked rows is a revert)", () => {
    dispatchStaged("t1", { type: "deleteRows", rows: [rowRef(1), rowRef(2)] }, ["id"]);
    expect(dispatchStaged("t1", { type: "deleteRows", rows: [rowRef(1)] }, ["code"])).toBe("staged");
    const staged = readStagedEdits("t1")!;
    expect(staged.keyColumns).toEqual(["id"]);
    expect(Object.keys(staged.deletes)).toEqual([rowRef(2).rowKey]);
  });

  it("never brings back changes of another key by redo once the table's key is taken", () => {
    dispatchStaged("t1", { type: "deleteRows", rows: [rowRef(1)] }, ["id"]);
    dispatchStaged("t1", { type: "undo" }, ["id"]);
    // Nothing is staged, so the table's new key is taken, without the old key's history.
    expect(dispatchStaged("t1", { type: "redo" }, ["code"])).toBe("unchanged");
    expect(dispatchStaged("t1", { type: "addRow", id: "i:1" }, ["code"])).toBe("staged");
    const staged = readStagedEdits("t1")!;
    expect(staged.keyColumns).toEqual(["code"]);
    expect(staged.future).toEqual([]);
    expect(staged.past).toEqual([{ updates: {}, inserts: [], deletes: {} }]);
  });
});

describe("keeping", () => {
  it("forgets the tabs that are closed", () => {
    dispatchStaged("open", { type: "addRow", id: "i:1" }, ["id"]);
    dispatchStaged("closed", { type: "addRow", id: "i:2" }, ["id"]);
    forgetStagedEditsExcept(new Set(["open"]));
    expect(readStagedEdits("closed")).toBeNull();
    expect(readStagedEdits("open")).not.toBeNull();
    putStagedEdits("open", null);
    expect(readStagedEdits("open")).toBeNull();
  });

  it("tells the writer nothing for the same staged edits put again, nor for a closed tab's drop", () => {
    dispatchStaged("t1", { type: "addRow", id: "i:1" }, ["id"]);
    const listener = vi.fn();
    const stop = onStagedEditsChanged(listener);
    putStagedEdits("t1", readStagedEdits("t1"));
    putStagedEdits("t9", null);
    forgetStagedEditsExcept(new Set());
    expect(readStagedEdits("t1")).toBeNull();
    expect(listener).not.toHaveBeenCalled();
    stop();
  });

  it("keeps an unknown outcome when the table's key is taken", () => {
    dispatchStaged("t1", { type: "addRow", id: "i:1" }, ["id"]);
    dispatchStaged("t1", {
      type: "applyOutcome",
      outcome: { outcome: "unknown", applied: ["i:1"], failedChange: null, message: null },
    });
    expect(dispatchStaged("t1", { type: "addRow", id: "i:2" }, ["code"])).toBe("staged");
    expect(readStagedEdits("t1")).toMatchObject({ keyColumns: ["code"], unknownOutcome: "pending" });
  });
});
