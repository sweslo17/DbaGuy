import { describe, expect, it } from "vitest";
import { SQLITE_BUSY_NOTE } from "../features/edits/commitFlow";
import { isSqliteBusy, sqliteBusyNote } from "./sqliteBusy";

/* The words are pinned as literals: the grid's commit and Run SQL file share one note, and a changed sentence is a
   decision, not a side effect of a refactor. */

describe("the SQLite busy note", () => {
  it("is the sentence the grid has always shown, with the verb of each caller", () => {
    expect(SQLITE_BUSY_NOTE).toBe(
      "Another tab still has an open result of this database file, or another program is using it: finish or close that result, then commit again.",
    );
    expect(sqliteBusyNote("run the file again")).toBe(
      "Another tab still has an open result of this database file, or another program is using it: finish or close that result, then run the file again.",
    );
    expect(sqliteBusyNote("dump again")).toContain("then dump again.");
  });

  it("recognises SQLite's busy and locked codes, and nothing else", () => {
    expect(["SQLITE_BUSY", "SQLITE_BUSY_SNAPSHOT", "SQLITE_LOCKED", "SQLITE_LOCKED_SHAREDCACHE"].map(isSqliteBusy)).toEqual([true, true, true, true]);
    expect([isSqliteBusy("SQLITE_CONSTRAINT"), isSqliteBusy("42P01"), isSqliteBusy(null), isSqliteBusy(undefined), isSqliteBusy("")]).toEqual([
      false,
      false,
      false,
      false,
      false,
    ]);
  });
});
