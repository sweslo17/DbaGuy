import { describe, expect, it } from "vitest";
import type { EditColumn } from "@/lib/api/types";
import { isImeKeyEvent } from "@/lib/ime";
import {
  editorResult,
  editStart,
  INLINE_MAX_CHARS,
  nextWritable,
  setNullEnds,
  type EditCellInput,
} from "./editing";
import { cellEditorKey, gridEditKey, valueEditorKey, type ValueEditorKeyState } from "./editKeys";

/*
 * How a cell's edit starts and ends (spec ⑤ §3.6.3, R-05-8) and the editing keys (§3.6.4): NULL is never turned into
 * an empty string by an untouched Return, a cut value is never edited from its preview, and an input method's Enter
 * never stages or opens the preview.
 */

const column = (overrides: Partial<EditColumn> = {}): EditColumn => ({
  name: "note",
  type_name: "text",
  kind: "text",
  nullable: true,
  has_default: false,
  writable: true,
  compare: "equals",
  unicode: true,
  ...overrides,
});
const start = (overrides: Partial<EditCellInput>) =>
  editStart({ column: column(), name: "note", row: "loaded", staged: undefined, original: "hello", ...overrides });

describe("starting an edit", () => {
  it("starts with the value's text, inline", () => {
    expect(start({})).toMatchObject({ mode: "inline", initialText: "hello", placeholder: null });
    expect(start({ original: 42, column: column({ kind: "number" }) })).toMatchObject({ initialText: "42" });
  });

  it("starts empty over NULL and DEFAULT, saying which it is", () => {
    expect(start({ original: null })).toMatchObject({ initialText: "", placeholder: "NULL" });
    expect(start({ row: "new", original: undefined })).toMatchObject({ initialText: "", placeholder: "DEFAULT" });
    expect(start({ staged: { kind: "null" } })).toMatchObject({ initialText: "", placeholder: "NULL" });
    expect(start({ staged: { kind: "value", text: "staged" } })).toMatchObject({ initialText: "staged" });
    // a loaded row's cell that the result does not carry is NULL for the editor too, never an empty string
    expect(start({ original: undefined })).toMatchObject({ initialText: "", placeholder: "NULL" });
  });

  it("opens JSON, text with a line break and long text in the drawer", () => {
    expect(start({ column: column({ kind: "json" }), original: '{"a":1}' })).toMatchObject({ mode: "drawer" });
    expect(start({ original: "one\ntwo" })).toMatchObject({ mode: "drawer" });
    expect(start({ original: "x".repeat(INLINE_MAX_CHARS + 1) })).toMatchObject({ mode: "drawer" });
    expect(start({ original: "x".repeat(INLINE_MAX_CHARS) })).toMatchObject({ mode: "inline" });
    expect(start({ original: "one\rtwo" })).toMatchObject({ mode: "drawer" }); // a lone CR is a line break too
  });

  it("never starts from a preview: bytes are typed whole, a cut text only takes NULL", () => {
    expect(
      start({ column: column({ kind: "binary" }), original: { $type: "bytes", size: 100, preview: "ab".repeat(64) } }),
    ).toMatchObject({
      initialText: "",
      placeholder: "Hex to replace all 100 bytes",
    });
    expect(
      start({ column: column({ kind: "binary" }), original: { $type: "bytes", size: 2, preview: "dead" } }),
    ).toMatchObject({
      initialText: "0xdead",
    });
    expect(start({ original: { $type: "text", preview: "abc", size: 70_000, truncated: true } })).toEqual({
      refuse: "This value is too large to edit here (70,000 characters); Set NULL or use SQL",
    });
  });

  it("says why a cell cannot be edited", () => {
    expect(start({ column: column({ writable: false, read_only_reason: "generated column" }) })).toEqual({
      refuse: "note is read-only: generated column",
    });
    expect(start({ column: undefined })).toEqual({ refuse: "note is not a column of the table any more: Refresh" });
    expect(start({ row: "deleted" })).toEqual({ refuse: "This row is marked for deletion: restore it first (⌘⌫)" });
    expect(start({ row: "key_incomplete" })).toEqual({
      refuse: "This row cannot be edited: its key was not loaded completely",
    });
  });
});

describe("ending an edit", () => {
  it("stages nothing unless something was typed, and an erased value as the empty string", () => {
    expect(editorResult("", false)).toBeNull();
    expect(editorResult("hello", false)).toBeNull();
    expect(editorResult("", true)).toEqual({ kind: "value", text: "" });
    expect(editorResult("9007199254740993", true)).toEqual({ kind: "value", text: "9007199254740993" });
  });

  it("ends on Set NULL unless the owner refuses it: a NOT NULL column keeps the editor, and what was typed", () => {
    expect(setNullEnds(undefined)).toBe(true);
    expect(setNullEnds(true)).toBe(true);
    expect(setNullEnds(false)).toBe(false);
  });

  it("moves to the next or previous writable column, and stops at the row's ends", () => {
    const names = ["id", "total", "label", "note"];
    const writable = (name: string) => name !== "total";
    expect(nextWritable(names, writable, 0, 1)).toBe(2);
    expect(nextWritable(names, writable, 2, -1)).toBe(0);
    expect(nextWritable(names, writable, 3, 1)).toBe(-1);
  });
});

const key = (overrides: Partial<KeyboardEvent>) =>
  ({
    key: "",
    code: "",
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
    repeat: false,
    isComposing: false,
    keyCode: 0,
    ...overrides,
  }) as KeyboardEvent;

describe("the grid's editing keys", () => {
  it.each([
    [{ key: "Enter", code: "Enter" }, "edit"],
    [{ key: "F2", code: "F2" }, "edit"],
    [{ key: "Dead", code: "KeyN", metaKey: true, altKey: true }, "setNull"],
    [{ key: "Backspace", code: "Backspace", metaKey: true }, "deleteRows"],
    [{ key: "z", code: "KeyZ", metaKey: true }, "undo"],
    [{ key: "z", code: "KeyZ", metaKey: true, shiftKey: true }, "redo"],
    [{ key: "Backspace", code: "Backspace" }, null],
    [{ key: "a", code: "KeyA" }, null], // no typing to edit
  ])("%j → %s", (event, action) => {
    expect(gridEditKey(key(event))).toBe(action);
  });

  it("takes only the exact keys: Return or F2 with a modifier, ⌥⌘ with another key, ⌥N without ⌘ are not edits", () => {
    for (const modifier of [{ shiftKey: true }, { metaKey: true }, { altKey: true }, { ctrlKey: true }]) {
      expect(gridEditKey(key({ key: "Enter", code: "Enter", ...modifier }))).toBeNull();
      expect(gridEditKey(key({ key: "F2", code: "F2", ...modifier }))).toBeNull();
    }
    expect(gridEditKey(key({ key: "Dead", code: "KeyM", metaKey: true, altKey: true }))).toBeNull();
    expect(gridEditKey(key({ key: "Dead", code: "KeyN", altKey: true }))).toBeNull();
    expect(gridEditKey(key({ key: "n", code: "KeyN", metaKey: true }))).toBeNull();
    expect(gridEditKey(key({ key: "Backspace", code: "Backspace", metaKey: true, altKey: true }))).toBeNull();
  });

  it("ignores an input method's keys, the late Enter included", () => {
    expect(gridEditKey(key({ key: "Enter", isComposing: true }))).toBeNull();
    expect(gridEditKey(key({ key: "Enter", keyCode: 229 }))).toBeNull();
    expect(gridEditKey(key({ key: "Process" }))).toBeNull();
  });

  it("does nothing on a key held down: Return does not open and close the editor, ⌘⌫ does not flip the rows", () => {
    expect(gridEditKey(key({ key: "Enter", code: "Enter", repeat: true }))).toBeNull();
    expect(gridEditKey(key({ key: "F2", code: "F2", repeat: true }))).toBeNull();
    expect(gridEditKey(key({ key: "Backspace", code: "Backspace", metaKey: true, repeat: true }))).toBeNull();
    expect(gridEditKey(key({ key: "Dead", code: "KeyN", metaKey: true, altKey: true, repeat: true }))).toBeNull();
    // undo and redo are steps back and forward: holding them walks the history, as everywhere else
    expect(gridEditKey(key({ key: "z", code: "KeyZ", metaKey: true, repeat: true }))).toBe("undo");
    expect(gridEditKey(key({ key: "z", code: "KeyZ", metaKey: true, shiftKey: true, repeat: true }))).toBe("redo");
  });
});

describe("the cell editor's keys", () => {
  it.each([
    [{ key: "Enter" }, "commit"],
    [{ key: "Tab" }, "next"],
    [{ key: "Tab", shiftKey: true }, "previous"],
    [{ key: "Escape" }, "cancel"],
    [{ key: "Dead", code: "KeyN", metaKey: true, altKey: true }, "setNull"],
    [{ key: "s", code: "KeyS", metaKey: true }, "save"],
    [{ key: "Enter", shiftKey: true }, null],
  ])("%j → %s", (event, action) => {
    expect(cellEditorKey(key(event))).toBe(action);
  });

  it("takes only the exact keys: a plain s, ⌥⌘ with another key, Return or Escape with a modifier do nothing", () => {
    expect(cellEditorKey(key({ key: "s", code: "KeyS" }))).toBeNull();
    expect(cellEditorKey(key({ key: "s", code: "KeyS", metaKey: true, shiftKey: true }))).toBeNull();
    expect(cellEditorKey(key({ key: "Dead", code: "KeyM", metaKey: true, altKey: true }))).toBeNull();
    expect(cellEditorKey(key({ key: "Enter", metaKey: true }))).toBeNull();
    expect(cellEditorKey(key({ key: "Escape", shiftKey: true }))).toBeNull();
    expect(cellEditorKey(key({ key: "Tab", metaKey: true }))).toBeNull();
  });

  it("acts once on Return and on ⌥⌘N: the key repeat of the Return that opened the editor stages nothing", () => {
    expect(cellEditorKey(key({ key: "Enter", repeat: true }))).toBeNull();
    expect(cellEditorKey(key({ key: "Dead", code: "KeyN", metaKey: true, altKey: true, repeat: true }))).toBeNull();
    // moving along the row and dropping an edit are not held back
    expect(cellEditorKey(key({ key: "Tab", repeat: true }))).toBe("next");
    expect(cellEditorKey(key({ key: "Escape", repeat: true }))).toBe("cancel");
  });

  it("never stages on the Enter that picks a character", () => {
    expect(cellEditorKey(key({ key: "Enter", keyCode: 229 }))).toBeNull();
    expect(cellEditorKey(key({ key: "s", code: "KeyS", metaKey: true, isComposing: true }))).toBeNull();
  });

  it("never moves, cancels or stages on any key of an input method", () => {
    for (const name of ["Enter", "Tab", "Escape"]) {
      expect(cellEditorKey(key({ key: name, isComposing: true }))).toBeNull();
      expect(cellEditorKey(key({ key: name, keyCode: 229 }))).toBeNull();
    }
    expect(cellEditorKey(key({ key: "Process" }))).toBeNull();
    const setNull = { key: "Dead", code: "KeyN", metaKey: true, altKey: true };
    expect(cellEditorKey(key({ ...setNull, isComposing: true }))).toBeNull();
  });
});

describe("the drawer value editor's keys", () => {
  const state = (overrides: Partial<ValueEditorKeyState> = {}): ValueEditorKeyState => ({
    plain: true,
    vim: false,
    searching: false,
    typing: true,
    ...overrides,
  });
  const press = (event: Partial<KeyboardEvent>, overrides: Partial<ValueEditorKeyState> = {}) =>
    valueEditorKey(key(event), state(overrides));
  const setNull = { key: "Dead", code: "KeyN", metaKey: true, altKey: true };

  it.each([
    [{ key: "Enter", metaKey: true }, "apply"],
    [{ key: "s", code: "KeyS", metaKey: true }, "save"],
    [{ key: "Escape" }, "cancel"],
    [setNull, "setNull"],
    [{ key: "Enter" }, "newline"],
    [{ key: "Tab" }, "tab"],
    [{ key: "Tab", shiftKey: true }, "swallow"],
    [{ key: "Backspace" }, "backspace"],
    [{ key: "a" }, null],
    [{ key: "s", code: "KeyS" }, null],
    [{ key: "s", code: "KeyS", metaKey: true, shiftKey: true }, null],
    [{ key: "Tab", altKey: true }, null],
    [{ key: "Tab", ctrlKey: true, shiftKey: true }, null],
    [{ key: "Enter", shiftKey: true }, null],
    [{ key: "Enter", metaKey: true, shiftKey: true }, null],
    [{ key: "Escape", shiftKey: true }, null],
    [{ key: "Backspace", altKey: true }, null], // delete a word: CodeMirror's
    [{ key: "Dead", code: "KeyM", metaKey: true, altKey: true }, null],
  ])("plain text, %j → %s", (event, action) => {
    expect(press(event)).toBe(action);
  });

  it("leaves a JSON value its language's Return, Tab and Backspace", () => {
    for (const event of [{ key: "Enter" }, { key: "Tab" }, { key: "Tab", shiftKey: true }, { key: "Backspace" }]) {
      expect(press(event, { plain: false })).toBeNull();
    }
    expect(press({ key: "Enter", metaKey: true }, { plain: false })).toBe("apply");
    expect(press({ key: "Escape" }, { plain: false })).toBe("cancel");
  });

  it("never reads an input method's key, whatever it is", () => {
    for (const name of ["Enter", "Tab", "Escape", "Backspace", "s"]) {
      for (const event of [{ isComposing: true }, { keyCode: 229 }] as Partial<KeyboardEvent>[]) {
        expect(press({ key: name, code: name === "s" ? "KeyS" : "", metaKey: name === "s", ...event })).toBeNull();
        expect(press({ key: name, ...event }, { typing: false })).toBeNull();
      }
    }
    expect(press({ key: "Process" })).toBeNull();
    expect(press({ ...setNull, isComposing: true })).toBeNull();
    expect(press({ key: "Enter", metaKey: true, isComposing: true })).toBeNull();
    // a held Return from the grid is swallowed, but a repeating composition key is not Return
    expect(press({ key: "Enter", repeat: true, isComposing: true }, { typing: false })).toBeNull();
  });

  it("acts once on ⌘↵, ⌘S, Escape and ⌥⌘N: a held key repeats none of them", () => {
    expect(press({ key: "Enter", metaKey: true, repeat: true })).toBeNull();
    expect(press({ key: "s", code: "KeyS", metaKey: true, repeat: true })).toBeNull();
    expect(press({ key: "Escape", repeat: true })).toBeNull();
    expect(press({ ...setNull, repeat: true })).toBeNull();
  });

  it("types line breaks, tabs and deletions held down once a key has been pressed in the editor", () => {
    expect(press({ key: "Enter", repeat: true })).toBe("newline");
    expect(press({ key: "Tab", repeat: true })).toBe("tab");
    expect(press({ key: "Backspace", repeat: true })).toBe("backspace");
    expect(press({ key: "Enter", repeat: true }, { plain: false })).toBeNull(); // JSON: CodeMirror's own
  });

  it("swallows the Return held down from the grid, which opened the editor, until a key that does not repeat", () => {
    expect(press({ key: "Enter", repeat: true }, { typing: false })).toBe("swallow");
    expect(press({ key: "Enter", repeat: true }, { typing: false, plain: false })).toBe("swallow");
    expect(press({ key: "Enter", repeat: true }, { typing: false, vim: true })).toBe("swallow");
    expect(press({ key: "Enter", metaKey: true, repeat: true }, { typing: false })).toBe("swallow");
    // the first Return pressed in the editor itself is a line break, and then they repeat
    expect(press({ key: "Enter" }, { typing: false })).toBe("newline");
  });

  it("under vim, Escape, Return, Tab and Backspace are vim's; apply, save and Set NULL stay (K15)", () => {
    for (const event of [{ key: "Escape" }, { key: "Enter" }, { key: "Tab" }, { key: "Backspace" }]) {
      expect(press(event, { vim: true })).toBeNull();
    }
    expect(press({ key: "Tab", shiftKey: true }, { vim: true })).toBeNull();
    expect(press({ key: "Enter", metaKey: true }, { vim: true })).toBe("apply");
    expect(press({ key: "s", code: "KeyS", metaKey: true }, { vim: true })).toBe("save");
    expect(press(setNull, { vim: true })).toBe("setNull");
  });

  it("leaves Escape to the search panel while it is open", () => {
    expect(press({ key: "Escape" }, { searching: true })).toBeNull();
    expect(press({ key: "Escape" }, { searching: false })).toBe("cancel");
  });
});

describe("the input method's key check", () => {
  it("reads a plain object that carries the three signals (the key maps are tested with them)", () => {
    expect(isImeKeyEvent({ isComposing: true, key: "a", keyCode: 65 })).toBe(true);
    expect(isImeKeyEvent({ isComposing: false, key: "Process", keyCode: 0 })).toBe(true);
    expect(isImeKeyEvent({ isComposing: false, key: "Enter", keyCode: 229 })).toBe(true);
    expect(isImeKeyEvent({ isComposing: false, key: "Enter", keyCode: 13 })).toBe(false);
  });

  it("reads a React key event through its native event", () => {
    const native = { isComposing: true, key: "Enter", keyCode: 13 };
    expect(isImeKeyEvent({ nativeEvent: native } as unknown as Parameters<typeof isImeKeyEvent>[0])).toBe(true);
  });
});
