import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import type { StagedEditsFile } from "@/lib/api/types";
import { rowKeyOf } from "./overlay";
import {
  MAX_SAVED_CHARS,
  MAX_SAVED_VALUE_CHARS,
  readableEdits,
  restoredEdits,
  savedEdits,
  UNSAVEABLE_HINT,
  type Unsaveable,
} from "./savedEdits";
import {
  emptyStaged,
  MAX_CHANGES,
  rowKeyFromKey,
  stage,
  toChanges,
  type AppliedOutcome,
  type NewId,
  type StageAction,
  type StagedEdits,
} from "./staging";

/*
 * What `workspace.json` keeps of staged edits (spec ⑤ §3.7, B40): only what the backend's model takes, nothing at all
 * beyond its limits (so one large set never fails every save), and the same changes bound to the same rows on restore.
 */

const ref = (id: unknown) => ({ kind: "row", rowKey: rowKeyFromKey({ id }, ["id"]), key: { id } }) as const;

function staged(...actions: StageAction[]): StagedEdits {
  let n = 0;
  const newId: NewId = (prefix) => `${prefix}:${++n}`;
  return actions.reduce((state, action) => stage(state, action, newId), emptyStaged(["id"]));
}

const edit = (id: unknown, text: string, original: unknown = "a"): StageAction => ({
  type: "edit",
  cell: { row: ref(id), column: "label", original },
  input: { kind: "value", text },
});

describe("saving staged edits", () => {
  it("keeps nothing for nothing staged", () => {
    expect(savedEdits(null)).toEqual({ file: null, tooLarge: false, unsaveable: null });
    expect(savedEdits(emptyStaged(["id"]))).toEqual({ file: null, tooLarge: false, unsaveable: null });
  });

  it("keeps every change, the UPDATE under a DELETE included, with the key and an unknown outcome", () => {
    const state = stage(
      staged(
        edit("9007199254740993", "b"),
        { type: "deleteRows", rows: [ref("9007199254740993")] },
        { type: "addRow", id: "i:x" },
      ),
      { type: "applyOutcome", outcome: { outcome: "unknown", applied: [], failedChange: null, message: null } },
    );
    expect(savedEdits(state)).toEqual({
      tooLarge: false,
      unsaveable: null,
      file: {
        version: 1,
        key_columns: ["id"],
        updates: [
          {
            id: "u:1",
            key: { id: "9007199254740993" },
            set: { label: { kind: "value", text: "b" } },
            original: { label: "a" },
          },
        ],
        inserts: [{ id: "i:x", values: {} }],
        deletes: [{ id: "d:2", key: { id: "9007199254740993" } }],
        outcome_unknown: true,
      },
    });
  });

  it("is computed once per state", () => {
    const state = staged(edit(1, "b"));
    expect(savedEdits(state)).toBe(savedEdits(state));
  });

  it("keeps nothing, and says so, beyond one value's limit, the total, or the number of changes", () => {
    expect(savedEdits(staged(edit(1, "x".repeat(MAX_SAVED_VALUE_CHARS + 1)))).tooLarge).toBe(true);
    expect(savedEdits(staged(edit(1, "x".repeat(MAX_SAVED_VALUE_CHARS)))).file).not.toBeNull();
    const big = Array.from({ length: Math.ceil(MAX_SAVED_CHARS / MAX_SAVED_VALUE_CHARS) + 1 }, (_, i) =>
      edit(i, "x".repeat(MAX_SAVED_VALUE_CHARS)),
    );
    expect(savedEdits(staged(...big))).toEqual({ file: null, tooLarge: true, unsaveable: null });
    // Entries are counted as the file holds them: a DELETE and the UPDATE under it are two.
    const many = staged(...Array.from({ length: MAX_CHANGES / 2 + 1 }, (_, i) => edit(i, "b")), {
      type: "deleteRows",
      rows: Array.from({ length: MAX_CHANGES / 2 }, (_, i) => ref(i)),
    });
    expect(savedEdits(many).tooLarge).toBe(true);
    const exactly = staged({ type: "deleteRows", rows: Array.from({ length: MAX_CHANGES }, (_, i) => ref(i)) });
    expect(savedEdits(exactly).file?.deletes).toHaveLength(MAX_CHANGES); // the limit itself is kept
  });

  it("counts a new row's values too, and one value's limit holds for them", () => {
    const insert = (id: string, size: number): StageAction[] => [
      { type: "addRow", id },
      {
        type: "edit",
        cell: { row: { kind: "insert", id }, column: "label" },
        input: { kind: "value", text: "x".repeat(size) },
      },
    ];
    expect(savedEdits(staged(...insert("i:a", MAX_SAVED_VALUE_CHARS + 1))).tooLarge).toBe(true);
    const nine = Array.from({ length: 9 }, (_, i) => insert(`i:${i}`, MAX_SAVED_VALUE_CHARS)).flat();
    expect(savedEdits(staged(...nine)).tooLarge).toBe(true); // 9 MiB in all
    expect(savedEdits(staged(...nine.slice(0, 14))).file).not.toBeNull(); // 7 MiB
  });

  it("keeps nothing for a staging without a key: the file needs one", () => {
    const keyless = stage(emptyStaged([]), { type: "addRow", id: "i:x" });
    expect(savedEdits(keyless)).toEqual({ file: null, tooLarge: false, unsaveable: "other" });
  });

  it("writes each kind in the order the changes were staged, whatever order the maps hold them in", () => {
    const state = staged(
      edit(1, "b"),
      edit(2, "c"),
      edit(3, "d"),
      { type: "addRow", id: "i:a" },
      { type: "addRow", id: "i:b" },
    );
    const shuffled = {
      ...state,
      updates: Object.fromEntries(Object.entries(state.updates).reverse()),
      inserts: [...state.inserts].reverse(),
    };
    const file = savedEdits(shuffled).file!;
    expect(file.updates?.map((u) => u.key.id)).toEqual([1, 2, 3]);
    expect(file.inserts?.map((i) => i.id)).toEqual(["i:a", "i:b"]);
  });

  // Pre-flight M3: the size is the backend's `_text_chars` to the character, or a set just under the limit would be
  // dropped by the repair unseen. Eight updates; each one's key {id: n} counts "id" and 1 (3), its original
  // {label: …} the name "label" (5) and the original's own characters.
  describe("counts exactly as the backend does", () => {
    const BYTES = { $type: "bytes", size: 100, preview: "ab".repeat(64) }; // 5+5 + 4+1 + 7+128 = 150 characters
    const totalling = (total: number, original: unknown, originalChars: number) => {
      const overhead = 3 + 5 + originalChars;
      const last = total - 8 * overhead - 7 * MAX_SAVED_VALUE_CHARS;
      return staged(
        ...Array.from({ length: 8 }, (_, i) => edit(i, "x".repeat(i < 7 ? MAX_SAVED_VALUE_CHARS : last), original)),
      );
    };

    it.each([
      ["a string", "a", 1],
      ["a character beyond the BMP, which JavaScript's length counts as two", "\u{1F600}", 1],
      ["an object, whose names count too", BYTES, 150],
      ["an array, element by element", ["ab", 3, null, [true]], 5],
    ])("up to the character, with %s as the original", (_, original, chars) => {
      expect(savedEdits(totalling(MAX_SAVED_CHARS, original, chars)).file).not.toBeNull();
      expect(savedEdits(totalling(MAX_SAVED_CHARS + 1, original, chars)).tooLarge).toBe(true);
    });

    it("counts the column names of keys (10,000 deletes by a long key name are beyond the limit)", () => {
      const name = "k".repeat(900);
      const deletes = (count: number) => {
        const rows = Array.from({ length: count }, (_, i) => ({
          kind: "row" as const,
          rowKey: rowKeyFromKey({ [name]: i }, [name]),
          key: { [name]: i },
        }));
        return stage(emptyStaged([name]), { type: "deleteRows", rows });
      };
      expect(savedEdits(deletes(MAX_CHANGES)).tooLarge).toBe(true); // 10,000 x (900 + 1) = 9,010,000 characters
      expect(savedEdits(deletes(9_000)).file).not.toBeNull(); // 8,109,000
    });

    it("counts a text's characters, not its UTF-16 units", () => {
      const emoji = "\u{1F600}";
      expect(savedEdits(staged(edit(1, emoji.repeat(600_000)))).file).not.toBeNull(); // 1.2 million units
      expect(savedEdits(staged(edit(1, emoji.repeat(MAX_SAVED_VALUE_CHARS)))).file).not.toBeNull();
      expect(savedEdits(staged(edit(1, emoji.repeat(MAX_SAVED_VALUE_CHARS + 1)))).tooLarge).toBe(true);
    });
  });

  describe("an apply in flight (pre-flight H1)", () => {
    const outcome = (kind: AppliedOutcome["outcome"], applied: string[] = []): StageAction => ({
      type: "applyOutcome",
      outcome: { outcome: kind, applied, failedChange: null, message: null },
    });

    it("is saved as an unknown outcome and comes back waiting for fresh rows, never as unsent", () => {
      const file = savedEdits(stage(staged(edit(1, "b"), { type: "addRow", id: "i:x" }), { type: "applying" })).file!;
      expect(file.outcome_unknown).toBe(true);
      expect(file.updates).toHaveLength(1);
      expect(file.inserts).toHaveLength(1);
      const back = restoredEdits(JSON.parse(JSON.stringify(file)));
      expect(back.unknownOutcome).toBe("pending");
      expect(savedEdits(back).file).toEqual(file); // saved again as it was
    });

    it("keeps an unknown outcome even when nothing else is left staged", () => {
      const applying = stage(staged(edit(1, "b")), { type: "applying" });
      const file = savedEdits(stage(applying, outcome("unknown", ["u:1"]))).file;
      expect(file).toEqual({
        version: 1,
        key_columns: ["id"],
        updates: [],
        inserts: [],
        deletes: [],
        outcome_unknown: true,
      });
      expect(restoredEdits(file!).unknownOutcome).toBe("pending");
    });

    it("is cleared by the outcome: a commit leaves nothing to keep, a rollback keeps the changes as unsent", () => {
      const applying = stage(staged(edit(1, "b")), { type: "applying" });
      expect(savedEdits(stage(applying, outcome("committed", ["u:1"])))).toEqual({
        file: null,
        tooLarge: false,
        unsaveable: null,
      });
      const rolledBack = savedEdits(stage(applying, outcome("rolled_back"))).file!;
      expect(rolledBack.outcome_unknown).toBe(false);
      expect(rolledBack.updates).toHaveLength(1);
    });

    it("never writes what the apply in flight replaced (ruling K10): only that the outcome is unknown", () => {
      const refreshed = stage(
        stage(stage(staged(edit(1, "b")), { type: "applying" }), outcome("unknown")),
        { type: "refreshed" },
      );
      const applying = stage(refreshed, { type: "applying" });
      expect(applying.beforeApplying).toBe("refreshed");
      const file = savedEdits(applying).file!;
      expect(Object.keys(file).sort()).toEqual(
        ["deletes", "inserts", "key_columns", "outcome_unknown", "updates", "version"],
      );
      expect(file).toEqual(savedEdits(refreshed).file); // the same file as before the apply
      expect(JSON.stringify(file)).not.toMatch(/refreshed|applying|before/i);
      expect(restoredEdits(file)).toMatchObject({ unknownOutcome: "pending", beforeApplying: null });
    });
  });
});

describe("restoring staged edits", () => {
  it("gives back the same changes, bound by key to the rows they were staged on", () => {
    const state = staged(
      edit(2, "b"),
      {
        type: "edit",
        cell: { row: ref(2), column: "data", original: { $type: "bytes", size: 2, preview: "dead" } },
        input: { kind: "null" },
      },
      { type: "addRow", id: "i:x" },
      {
        type: "edit",
        cell: { row: { kind: "insert", id: "i:x" }, column: "label" },
        input: { kind: "value", text: "" },
      },
      { type: "deleteRows", rows: [ref(3)] },
    );
    const file = savedEdits(state).file!;
    const back = restoredEdits(JSON.parse(JSON.stringify(file)));
    expect(savedEdits(back).file).toEqual(file);
    expect(Object.keys(back.updates)).toEqual([rowKeyOf([2, "row"], [0])]); // the key the overlay binds the row by
    expect(back.inserts[0].values).toEqual({ label: { kind: "value", text: "" } });
    expect(back.unknownOutcome).toBeNull();
    expect(back.past).toEqual([]);
  });

  it("reads the file as the backend gives it back: defaults left out and `text: null` on NULL and DEFAULT cells", () => {
    const back = restoredEdits({
      version: 1,
      key_columns: ["id"],
      updates: [{ id: "u:1", key: { id: 1 }, set: { a: { kind: "null", text: null } }, original: { a: "x" } }],
      inserts: [{ id: "i:1", values: { a: { kind: "default", text: null }, b: { kind: "value", text: "" } } }],
      outcome_unknown: false,
    });
    expect(Object.values(back.updates)[0].set).toEqual({ a: { kind: "null" } });
    expect(back.inserts[0].values).toEqual({ a: { kind: "default" }, b: { kind: "value", text: "" } });
    expect(back.deletes).toEqual({});
  });

  it("comes to one form from a file with the backend's `text: null` and one without (L3)", () => {
    // `snapshotJson` compares a local copy with the backend's file through this form: a copy the backend holds after
    // all must not be offered as "unsaved changes" for the defaults the backend adds.
    const base = savedEdits(staged(edit(1, "b"))).file!;
    const update = base.updates![0];
    const written = { ...base, updates: [{ ...update, set: { label: { kind: "null" as const } } }] };
    const returned = { ...base, updates: [{ ...update, set: { label: { kind: "null" as const, text: null } } }] };
    const form = (file: StagedEditsFile) => JSON.stringify(savedEdits(restoredEdits(file)).file);
    expect(JSON.stringify(returned)).not.toBe(JSON.stringify(written));
    expect(form(returned)).toBe(form(written));
  });

  it("goes on numbering from the restored changes, so what is staged next is sent after them", () => {
    const back = restoredEdits(savedEdits(staged(edit(1, "b"), edit(2, "c"), { type: "addRow", id: "i:x" })).file!);
    const next = stage(back, {
      type: "edit",
      cell: { row: ref(3), column: "label", original: "a" },
      input: { kind: "value", text: "d" },
    });
    expect(savedEdits(next).file?.updates?.map((u) => u.key.id)).toEqual([1, 2, 3]);
  });

  it("keeps an UPDATE under its row's DELETE hidden, and a later save counts it (ruling K3)", () => {
    const state = staged(edit(5, "b"), { type: "deleteRows", rows: [ref(5)] });
    const file = savedEdits(state).file!;
    expect([file.updates?.length, file.deletes?.length]).toEqual([1, 1]);
    const back = restoredEdits(JSON.parse(JSON.stringify(file)));
    expect(Object.keys(back.updates)).toEqual(Object.keys(back.deletes));
    expect(savedEdits(back).file).toEqual(file);
  });

  it("brings an unknown outcome back as waiting for fresh rows", () => {
    const file = {
      version: 1 as const,
      key_columns: ["id"],
      updates: [],
      inserts: [],
      deletes: [],
      outcome_unknown: true,
    };
    expect(restoredEdits(file).unknownOutcome).toBe("pending");
  });
});

/*
 * Ruling K5: what the backend's model refuses besides size is never written, so one bad value cannot make every save
 * fail (B40) and the edit bar can say why nothing is kept. The staging itself is not touched.
 */

interface RefusalCase {
  name: string;
  accepted: boolean;
  reason: Unsaveable | null;
  file: StagedEditsFile;
}

// The backend's `test_local_stores.py` puts the same files through `StagedEditsFile`: what is kept here is what the
// model takes there, and what is refused here is what it refuses.
const CASES = JSON.parse(
  readFileSync(new URL("../../../backend/tests/fixtures/staged_edits_cases.json", import.meta.url), "utf8"),
) as RefusalCase[];

describe("keeping exactly what the backend's model takes (ruling K5)", () => {
  it("has cases of every kind, each named once", () => {
    expect(new Set(CASES.map((c) => c.name)).size).toBe(CASES.length);
    expect(new Set(CASES.map((c) => c.reason))).toEqual(new Set([null, "value", "name", "other"]));
    expect(new Set(CASES.map((c) => c.accepted))).toEqual(new Set([true, false]));
  });

  it.each(CASES)("$name", ({ file, accepted, reason }) => {
    const saved = savedEdits(restoredEdits(file));
    if (accepted) {
      expect(saved).toEqual({ file, tooLarge: false, unsaveable: null }); // written back as it is
    } else {
      expect(saved).toEqual({ file: null, tooLarge: false, unsaveable: reason });
    }
  });
});

describe("staged edits that cannot be kept (ruling K5)", () => {
  const editColumn = (column: string, text: string): StageAction => ({
    type: "edit",
    cell: { row: ref(1), column, original: "a" },
    input: { kind: "value", text },
  });

  it("says why: a character that cannot be saved, or a column name", () => {
    expect(savedEdits(staged(edit(1, "a\ud800b")))).toMatchObject({ file: null, tooLarge: false, unsaveable: "value" });
    expect(savedEdits(staged(editColumn("bad\udc00", "b")))).toMatchObject({ file: null, unsaveable: "name" });
    expect(savedEdits(staged(editColumn("", "b")))).toMatchObject({ file: null, unsaveable: "name" });
    expect(savedEdits(staged(editColumn("k".repeat(1025), "b")))).toMatchObject({ file: null, unsaveable: "name" });
    expect(savedEdits(staged(editColumn("k".repeat(1024), "b"))).file).not.toBeNull();
    expect(savedEdits(staged(edit("\ud800", "b")))).toMatchObject({ file: null, unsaveable: "value" }); // in the key
    for (const reason of ["value", "name", "other"] as const) expect(UNSAVEABLE_HINT[reason]).toMatch(/^Can't keep/);
  });

  it("is not too large, and too large is not unsaveable", () => {
    const tooLarge = savedEdits(staged(edit(1, "x".repeat(MAX_SAVED_VALUE_CHARS + 1))));
    expect(tooLarge).toMatchObject({ file: null, tooLarge: true, unsaveable: null });
    expect(savedEdits(staged(edit(1, "a\ud800b"))).tooLarge).toBe(false);
  });

  it("leaves the staging as it is, still committable, and keeps it again once the text is fixed", () => {
    const state = staged(edit(1, "a\ud800b"), { type: "addRow", id: "i:x" });
    const before = structuredClone(state);
    expect(savedEdits(state).unsaveable).toBe("value");
    expect(state).toEqual(before);
    expect(toChanges(state).updates).toHaveLength(1); // a commit would send it
    const fixed = stage(state, edit(1, "ab"));
    expect(savedEdits(fixed).file?.updates?.[0].set).toEqual({ label: { kind: "value", text: "ab" } });
  });

  it("finds a repeated id, whatever kinds hold it", () => {
    const state = staged(edit(1, "b"), { type: "addRow", id: "u:1" }); // the update's id is u:1
    expect(savedEdits(state)).toMatchObject({ file: null, unsaveable: "other" });
  });

  it("finds a cell nested more than 64 deep (the backend's MAX_CELL_LEVELS), and takes 64", () => {
    // 254 levels were taken by the model and written, and the file then could not be read back (B40)
    const nested = (levels: number) => Array.from({ length: levels }).reduce<unknown>((cell) => [cell], 1);
    const deep = (levels: number) => staged(edit(1, "b", nested(levels)));
    expect(savedEdits(deep(63)).file).not.toBeNull();
    expect(savedEdits(deep(64)).file).not.toBeNull();
    expect(savedEdits(deep(65))).toMatchObject({ file: null, tooLarge: false, unsaveable: "other" });
    expect(savedEdits(deep(254))).toMatchObject({ file: null, unsaveable: "other" });
  });
});

describe("reading a set from a local copy", () => {
  const good = savedEdits(staged(edit(1, "b"), { type: "addRow", id: "i:x" })).file!;
  const warned = (run: () => unknown) => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      return { result: run(), said: warn.mock.calls.flat().join(" "), calls: warn.mock.calls.length };
    } finally {
      warn.mockRestore();
    }
  };

  it("gives the staged edits of a set that reads, silently", () => {
    const { result, calls } = warned(() => readableEdits(good));
    expect(result).toEqual(restoredEdits(good));
    expect(calls).toBe(0);
  });

  it.each([
    ["a key missing", { ...good, updates: [{ ...good.updates![0], key: undefined }] }],
    ["an original missing", { ...good, updates: [{ ...good.updates![0], original: undefined }] }],
    ["a set that is null", { ...good, updates: [{ ...good.updates![0], set: null }] }],
    [
      "the key columns missing, with an outcome to keep",
      { ...good, updates: [], inserts: [], key_columns: undefined, outcome_unknown: true },
    ],
    ["nothing at all", null],
  ])("drops one that does not read (%s), with a warning that names no value", (_, malformed) => {
    const { result, said, calls } = warned(() => readableEdits(malformed as unknown as StagedEditsFile));
    expect(result).toBeNull();
    expect(calls).toBe(1);
    expect(said).toMatch(/Dropped a tab's staged edits.*\((TypeError|RangeError)\)/);
    expect(said).not.toContain('"b"'); // no part of the set is in it
  });
});
