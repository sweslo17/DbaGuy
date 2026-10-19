import { redo, redoDepth, undo, undoDepth } from "@codemirror/commands";
import { EditorState, type Transaction, type TransactionSpec } from "@codemirror/state";
import { describe, expect, it, vi } from "vitest";
import { baseExtensions, reconfigureEffects, type EditorOptions } from "./extensions";
import { startingPoint } from "./startingPoint";

// The editor's theme asks the browser for the colour scheme as it is loaded: give it an answer (before the imports run).
vi.hoisted(() => {
  vi.stubGlobal("window", {
    matchMedia: () => ({ matches: false, addEventListener: () => undefined, removeEventListener: () => undefined }),
  });
});

/*
 * What matters: after the editor's text is set from outside (a document loaded or reloaded, a script read), Undo and Redo
 * must not bring back any piece of the old text. The editor is the real configuration (`baseExtensions`), as a state
 * (the pure part of an editor; no DOM here).
 */

const OPTIONS: EditorOptions = {
  fontSize: 13,
  tabSize: 2,
  wordWrap: false,
  vim: false,
  upperCaseKeywords: false,
  dialect: null,
  language: "json",
  readOnly: false,
};

const editorState = (doc: string, options = OPTIONS) =>
  EditorState.create({ doc, extensions: baseExtensions(options) });

/** The state after the transactions, applied one at a time as the view dispatches them. */
const applied = (state: EditorState, specs: TransactionSpec[]) => specs.reduce((s, spec) => s.update(spec).state, state);

/** The user types: replaces [from, to) with `insert`, as an undoable edit. */
const typed = (state: EditorState, from: number, to: number, insert: string) =>
  state.update({ changes: { from, to, insert }, userEvent: "input.type" }).state;

/** A command of the history keymap: it acts on a state and hands back the transaction it makes. */
type HistoryCommand = (target: { state: EditorState; dispatch: (tr: Transaction) => void }) => boolean;

/** Runs a history command as a keymap would: the state after it, and whether it did anything. */
function run(state: EditorState, command: HistoryCommand): { state: EditorState; did: boolean } {
  let next = state;
  const did = command({ state, dispatch: (tr) => (next = tr.state) });
  return { state: next, did };
}

const SERVER = '{"server": true}';

/** A document with some typing in its history: a piece deleted from `{"a": 1}`. */
const edited = () => typed(editorState('{"a": 1}'), 1, 5, "");

describe("startingPoint", () => {
  it("replaces the text and leaves nothing to undo or redo, as after a Reload", () => {
    const before = edited();
    expect(undoDepth(before)).toBe(1); // the edit is in the history to begin with

    const after = applied(before, startingPoint(before, SERVER));

    expect(after.doc.toString()).toBe(SERVER);
    expect(undoDepth(after)).toBe(0);
    expect(redoDepth(after)).toBe(0);
    // Undo and redo do nothing: no piece of the old text comes back inside the new one (`{"server": true}"a"`).
    const undone = run(after, undo);
    expect(undone.did).toBe(false);
    expect(undone.state.doc.toString()).toBe(SERVER);
    const redone = run(after, redo);
    expect(redone.did).toBe(false);
    expect(redone.state.doc.toString()).toBe(SERVER);
  });

  it("forgets what had been undone: redo after undo-then-replace does nothing", () => {
    // An insertion: redoing it would put the typed text into the new one (a deletion maps to nothing and vanishes).
    const insertion = typed(editorState('{"a": 1}'), 1, 1, '"b": 2, ');
    const undone = run(insertion, undo).state;
    expect(redoDepth(undone)).toBe(1); // the undone edit waits to be redone

    const after = applied(undone, startingPoint(undone, SERVER));

    expect(undoDepth(after)).toBe(0);
    expect(redoDepth(after)).toBe(0);
    const redone = run(after, redo);
    expect(redone.did).toBe(false);
    expect(redone.state.doc.toString()).toBe(SERVER);
  });

  it("keeps undo and redo working for what is typed afterwards, back to exactly the replaced text", () => {
    const before = edited();
    const start = applied(before, startingPoint(before, SERVER));

    const typedAfter = typed(start, SERVER.length - 1, SERVER.length - 1, ' ,"x": 2');
    expect(undoDepth(typedAfter)).toBe(1);

    const undone = run(typedAfter, undo);
    expect(undone.did).toBe(true);
    expect(undone.state.doc.toString()).toBe(SERVER);
    expect(undoDepth(undone.state)).toBe(0);

    const redone = run(undone.state, redo);
    expect(redone.did).toBe(true);
    expect(redone.state.doc.toString()).toBe(typedAfter.doc.toString());
  });

  it("starts over each time: a second replacement drops the typing since the first", () => {
    const first = applied(edited(), startingPoint(edited(), SERVER));
    const typedAfter = typed(first, 0, 0, " ");
    expect(undoDepth(typedAfter)).toBe(1);

    const second = applied(typedAfter, startingPoint(typedAfter, '{"again": 1}'));

    expect(second.doc.toString()).toBe('{"again": 1}');
    expect(undoDepth(second)).toBe(0);
    expect(run(second, undo).did).toBe(false);
  });

  it("is not blocked by the history that a text with no history has: a fresh editor works the same", () => {
    const fresh = editorState("old");
    const after = applied(fresh, startingPoint(fresh, SERVER));
    expect(after.doc.toString()).toBe(SERVER);
    expect(undoDepth(after)).toBe(0);
    expect(run(typed(after, 0, 0, " "), undo).state.doc.toString()).toBe(SERVER);
  });

  it("puts the selection as given, clamped to the new text, else at the start", () => {
    const state = editorState("0123456789");
    const at = (selection?: { anchor: number; head: number }) =>
      applied(state, startingPoint(state, "abcdef", selection)).selection.main;

    expect(at()).toMatchObject({ anchor: 0, head: 0 });
    expect(at({ anchor: 2, head: 4 })).toMatchObject({ anchor: 2, head: 4 });
    expect(at({ anchor: 99, head: 99 })).toMatchObject({ anchor: 6, head: 6 });
    expect(at({ anchor: -3, head: 2 })).toMatchObject({ anchor: 0, head: 2 });
  });

  it("counts the text as the editor does (line breaks normalised)", () => {
    const state = editorState("x");
    const after = applied(state, startingPoint(state, "a\r\nb\rc", { anchor: 99, head: 99 }));
    expect(after.doc.toString()).toBe("a\nb\nc");
    expect(after.selection.main.head).toBe(5);
  });

  it("leaves the other settings as they were, and the editor can still be reconfigured", () => {
    const readOnly = { ...OPTIONS, readOnly: true };
    const state = editorState("old", readOnly);
    expect(state.readOnly).toBe(true);

    const after = applied(state, startingPoint(state, SERVER));
    expect(after.readOnly).toBe(true); // reset only the history, not the read-only compartment

    const writable = after.update({ effects: reconfigureEffects(readOnly, OPTIONS) }).state;
    expect(writable.readOnly).toBe(false);
    expect(writable.doc.toString()).toBe(SERVER);
    // …and the history survives a reconfiguration of the other settings.
    const typedAfter = typed(writable, 0, 0, " ");
    const toggled = typedAfter.update({ effects: reconfigureEffects(OPTIONS, readOnly) }).state;
    expect(undoDepth(toggled)).toBe(1);
  });
});
