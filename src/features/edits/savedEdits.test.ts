import { describe, expect, it } from "vitest";
import type { StagedEditsFile } from "@/lib/api/types";
import { rowKeyOf } from "./overlay";
import { MAX_SAVED_CHARS, MAX_SAVED_VALUE_CHARS, restoredEdits, savedEdits } from "./savedEdits";
import {
  emptyStaged,
  MAX_CHANGES,
  rowKeyFromKey,
  stage,
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
    expect(savedEdits(null)).toEqual({ file: null, tooLarge: false });
    expect(savedEdits(emptyStaged(["id"]))).toEqual({ file: null, tooLarge: false });
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
    expect(savedEdits(staged(edit(1, "x".repeat(MAX_SAVED_VALUE_CHARS)))).tooLarge).toBe(false);
    const big = Array.from({ length: Math.ceil(MAX_SAVED_CHARS / MAX_SAVED_VALUE_CHARS) + 1 }, (_, i) =>
      edit(i, "x".repeat(MAX_SAVED_VALUE_CHARS)),
    );
    expect(savedEdits(staged(...big))).toEqual({ file: null, tooLarge: true });
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
    expect(savedEdits(staged(...nine.slice(0, 14))).tooLarge).toBe(false); // 7 MiB
  });

  it("keeps nothing for a staging without a key: the file needs one", () => {
    const keyless = stage(emptyStaged([]), { type: "addRow", id: "i:x" });
    expect(savedEdits(keyless)).toEqual({ file: null, tooLarge: true });
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
      expect(savedEdits(totalling(MAX_SAVED_CHARS, original, chars)).tooLarge).toBe(false);
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
      expect(savedEdits(deletes(9_000)).tooLarge).toBe(false); // 8,109,000
    });

    it("counts a text's characters, not its UTF-16 units", () => {
      const emoji = "\u{1F600}";
      expect(savedEdits(staged(edit(1, emoji.repeat(600_000)))).tooLarge).toBe(false); // 1.2 million units
      expect(savedEdits(staged(edit(1, emoji.repeat(MAX_SAVED_VALUE_CHARS)))).tooLarge).toBe(false);
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
      expect(savedEdits(stage(applying, outcome("committed", ["u:1"])))).toEqual({ file: null, tooLarge: false });
      const rolledBack = savedEdits(stage(applying, outcome("rolled_back"))).file!;
      expect(rolledBack.outcome_unknown).toBe(false);
      expect(rolledBack.updates).toHaveLength(1);
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
