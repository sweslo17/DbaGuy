import { describe, expect, it } from "vitest";
import type { EditTarget } from "@/lib/api/types";
import { firstBrowse, firstSorts, NOTHING_ASKED, stableSorts, type TargetRead } from "./stableOrder";

/* A PostgreSQL table tab's first browse (05b K13): sorted by the key when the target says so, never waiting long. */

const PATH = [
  { kind: "database", name: "dbaguy" },
  { kind: "schema", name: "public" },
  { kind: "folder", name: "tables" },
  { kind: "table", name: "items" },
] as EditTarget["path"];

const target = (fields: Partial<EditTarget>): EditTarget => ({
  path: PATH,
  editable: true,
  key: ["id"],
  columns: [],
  transactional: true,
  backslash_escapes: true,
  stable_order: ["id"],
  ...fields,
});
const success = (fields: Partial<EditTarget> = {}): TargetRead => ({ status: "success", target: target(fields) });
const PENDING: TargetRead = { status: "pending" };
const FAILED: TargetRead = { status: "error", passwordRequired: false };
const NEEDS_PASSWORD: TargetRead = { status: "error", passwordRequired: true };
const BY_ID = [{ column: "id", descending: false }];

describe("the first browse's sort", () => {
  it("is the stable order, every column ascending", () => {
    expect(firstSorts(true, success(), false)).toEqual(BY_ID);
    expect(firstSorts(true, success({ key: ["a", "b"], stable_order: ["a", "b"] }), false)).toEqual([
      { column: "a", descending: false },
      { column: "b", descending: false },
    ]);
  });

  it("is none without a stable order, for a table that is not editable and for a failed target", () => {
    expect(firstSorts(true, success({ stable_order: null }), false)).toEqual([]);
    expect(firstSorts(true, success({ stable_order: undefined }), false)).toEqual([]);
    // read-only connection, view, no key: not editable (the backend gives no order then either)
    expect(firstSorts(true, success({ editable: false, reason: "This connection is read-only" }), false)).toEqual([]);
    expect(firstSorts(true, FAILED, false)).toEqual([]);
  });

  it("waits for the target, and for a target that needed the password to be read again", () => {
    expect(firstSorts(true, PENDING, false)).toBeNull();
    expect(firstSorts(true, NEEDS_PASSWORD, false)).toBeNull();
  });

  it("never waits once the time is up, whatever the target", () => {
    for (const read of [PENDING, NEEDS_PASSWORD, success()]) expect(firstSorts(true, read, true)).toEqual([]);
    expect(firstSorts(null, PENDING, true)).toEqual([]);
  });

  it("does not wait on an engine whose unsorted order is stable, and waits until the engine is known", () => {
    for (const read of [PENDING, NEEDS_PASSWORD, success()]) expect(firstSorts(false, read, false)).toEqual([]);
    expect(firstSorts(null, success(), false)).toBeNull();
  });

  it("reads a column named constructor like any other", () => {
    expect(stableSorts(target({ key: ["constructor"], stable_order: ["constructor"] }))).toEqual([
      { column: "constructor", descending: false },
    ]);
  });
});

describe("what the user asks for while the first browse waits", () => {
  const BY_LABEL = [{ column: "label", descending: true }];

  it("waits with a filter, then browses with it and the table's order", () => {
    const asked = { ...NOTHING_ASKED, filter: "id > 3" };
    expect(firstBrowse(null, asked)).toBeNull();
    expect(firstBrowse(BY_ID, asked)).toEqual({ filter: "id > 3", sorts: BY_ID, newFilter: true });
    // the target failed, or the wait is over
    expect(firstBrowse([], asked)).toEqual({ filter: "id > 3", sorts: [], newFilter: true });
  });

  it("browses as before when nothing was asked", () => {
    expect(firstBrowse(null, NOTHING_ASKED)).toBeNull();
    expect(firstBrowse(BY_ID, NOTHING_ASKED)).toEqual({ filter: "", sorts: BY_ID, newFilter: false });
  });

  it("takes a cleared filter as asked for (the empty filter applies)", () => {
    expect(firstBrowse(BY_ID, { filter: "", sorts: null })).toEqual({ filter: "", sorts: BY_ID, newFilter: true });
  });

  it("lets a sort the user picked win over the table's order, without waiting", () => {
    expect(firstBrowse(null, { filter: null, sorts: BY_LABEL })).toEqual({
      filter: "",
      sorts: BY_LABEL,
      newFilter: false,
    });
    expect(firstBrowse(BY_ID, { filter: "a = 1", sorts: BY_LABEL })).toEqual({
      filter: "a = 1",
      sorts: BY_LABEL,
      newFilter: true,
    });
    // a sort cleared before the first browse: the server's order
    expect(firstBrowse(BY_ID, { filter: null, sorts: [] })).toEqual({ filter: "", sorts: [], newFilter: false });
  });
});
