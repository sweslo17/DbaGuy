import { Text } from "@codemirror/state";
import { describe, expect, it } from "vitest";
import type { DocumentView } from "@/lib/api/types";
import {
  canEdit,
  initialModel,
  NEW_DOCUMENT_TEXT,
  panelOf,
  transition,
  type DocumentEffect,
  type DocumentEvent,
  type DocumentLoad,
  type DocumentMeta,
  type DocumentModel,
  type TabFacts,
} from "./documentState";

/*
 * The transitions that decide whether a document tab's unsaved edits survive (Q2, rulings C28-C30, C32). Each test
 * plays the hook's part against `Session`: it keeps the model, the tab as the store would, and the editor's text, runs
 * the effects the way `useDocument` does and answers the reads the model asks for.
 */

const doc = (text: string) => Text.of(text.split("\n"));
const view = (overrides: Partial<DocumentView> = {}): DocumentView => ({
  id: "u1",
  cas: "100",
  expiration: 0,
  type: "json",
  text: '{"a": 1}',
  size: 8,
  ...overrides,
});
const meta = (cas: string): DocumentMeta => ({ cas, expiration: 0, type: "json", size: 8 });
const failure = (code: string) => Object.assign(new Error(code), { code });

class Session {
  model: DocumentModel;
  tab: TabFacts;
  /** What the editor holds. */
  editor: string;
  /** The dirty-check timer is armed. */
  timer = false;
  effects: DocumentEffect[] = [];

  constructor(tab: Partial<TabFacts> = {}, editor = "") {
    this.tab = { isNew: false, cas: null, dirty: false, initialText: null, ...tab };
    this.model = initialModel(this.tab);
    this.editor = editor;
  }

  send(event: DocumentEvent): void {
    const next = transition(this.model, event, this.tab);
    this.model = next.model;
    for (const effect of next.effects) this.run(effect);
  }

  private run(effect: DocumentEffect): void {
    this.effects.push(effect);
    if (effect.kind === "patch-tab") this.tab = { ...this.tab, ...effect.patch };
    if (effect.kind === "check-dirty") this.timer = true;
    if (effect.kind === "put-text") {
      this.editor = effect.text;
      if (effect.baseline) this.send({ type: "text-put", doc: doc(this.editor) });
    }
  }

  get generation(): number {
    expect(this.model.request, "a read should be in flight").not.toBeNull();
    return this.model.request!.generation;
  }

  show = () => this.send({ type: "shown" });
  answer = (answer: DocumentView = view()) => this.send({ type: "loaded", generation: this.generation, view: answer });
  fail = (error: Error) => this.send({ type: "load-failed", generation: this.generation, error });
  missing = () => this.send({ type: "load-missing", generation: this.generation });
  retry = () => this.send({ type: "retry" });
  reload = () => this.send({ type: "reload" });

  /** The user types: the editor holds `text` and the tab hears of it. */
  type(text: string): void {
    this.editor = text;
    this.send({ type: "edited" });
  }

  /** `DIRTY_CHECK_MS` after the last edit. */
  settle(): void {
    if (!this.timer) return;
    this.timer = false;
    this.send({ type: "settled", doc: doc(this.editor) });
  }

  /** The tab's load as the user sees it (the tab being new wins, as in `useDocument`). */
  get load(): DocumentLoad {
    return this.tab.isNew ? { status: "new" } : this.model.load;
  }
}

/** A tab with unsaved edits "x" on top of CAS 100, as a restored workspace or an error boundary's retry gives it. */
const editedTab = () => new Session({ dirty: true, cas: "100", initialText: null }, "x edits");

describe("the first read", () => {
  it("is not made for a new document", () => {
    const session = new Session({ isNew: true, initialText: NEW_DOCUMENT_TEXT }, NEW_DOCUMENT_TEXT);
    session.show();
    expect(session.model.request).toBeNull();
    expect(session.load).toEqual({ status: "new" });
    expect(session.editor).toBe(NEW_DOCUMENT_TEXT);
  });

  it("waits until the tab is shown, and is made once however often it is shown", () => {
    const session = editedTab();
    expect(session.model.request).toBeNull();
    expect(session.effects).toEqual([]);
    session.show();
    const request = session.model.request;
    expect(request).toEqual({ generation: 1, mode: "first" });
    session.show(); // React's mount check sends it again
    expect(session.model.request).toBe(request);
  });

  it("puts the server's text in the editor of a clean tab, and that document is the baseline", () => {
    const session = new Session();
    session.show();
    session.answer(view({ cas: "100", text: '{"a": 1}' }));
    expect(session.editor).toBe('{"a": 1}');
    expect(session.tab).toMatchObject({ cas: "100", dirty: false, isNew: false });
    expect(session.model.load).toEqual({ status: "ready", meta: meta("100") });
    expect(session.model.baseline?.eq(doc('{"a": 1}'))).toBe(true);
    expect(session.model.changedOnServer).toBe(false);
  });

  it("puts nothing for a binary document but an empty text", () => {
    const session = new Session({}, "stale");
    session.show();
    session.answer(view({ type: "base64", text: null, size: 9 }));
    expect(session.editor).toBe("");
    expect(panelOf(session.load, session.tab.dirty)).toBe("binary");
  });

  it("is told apart from a clean tab by its edits, not by its initial text (a tab mounted again keeps them)", () => {
    const session = editedTab(); // loaded, edited, then its error boundary's Retry mounted it again: initialText null
    session.show();
    session.answer(view({ cas: "100", text: "from the server" }));
    expect(session.editor).toBe("x edits");
    expect(session.tab).toMatchObject({ dirty: true, cas: "100" });
    expect(session.model.load).toEqual({ status: "ready", meta: meta("100") });
    expect(session.model.baseline).toBeNull();
    expect(session.model.changedOnServer).toBe(false);
  });

  it("keeps a restored tab's edits and says when the document is not the one they were made from", () => {
    const session = editedTab();
    session.show();
    session.answer(view({ cas: "101" }));
    expect(session.editor).toBe("x edits");
    expect(session.tab).toMatchObject({ dirty: true, cas: "100" }); // a save will report the conflict
    expect(session.model.changedOnServer).toBe(true);
  });

  it("does not touch the editor when a new document was started meanwhile", () => {
    const session = new Session({}, "draft");
    session.show();
    session.tab = { ...session.tab, isNew: true };
    session.answer();
    expect(session.editor).toBe("draft");
    expect(session.model.request).toBeNull();
  });
});

describe("a failed read, then Retry", () => {
  it("reads again, and a clean tab then gets the server's text", () => {
    const session = new Session();
    session.show();
    session.fail(failure("query_failed"));
    expect(session.model.load).toMatchObject({ status: "error" });
    expect(session.model.request).toBeNull();
    session.retry();
    expect(session.model.request).toEqual({ generation: 2, mode: "first" });
    expect(session.load).toEqual({ status: "loading" });
    session.answer(view({ text: "text" }));
    expect(session.editor).toBe("text");
    expect(session.load).toMatchObject({ status: "ready" });
  });

  it("keeps a tab's edits (ruling C29)", () => {
    const session = editedTab();
    session.show();
    session.fail(failure("query_failed"));
    session.retry();
    session.answer(view({ text: "from the server" }));
    expect(session.editor).toBe("x edits");
    expect(session.tab.dirty).toBe(true);
  });

  it("is what the unlock dialog sends: the edits survive the password being given", () => {
    const session = editedTab(); // check 12: restored after a backend restart, its first read needs the password
    session.show();
    session.fail(failure("password_required"));
    expect(panelOf(session.load, session.tab.dirty)).toBe("error");
    session.retry(); // the password is given
    session.answer(view({ cas: "101", text: "from the server" }));
    expect(session.editor).toBe("x edits");
    expect(session.tab).toMatchObject({ dirty: true, cas: "100" });
    expect(session.load).toMatchObject({ status: "ready" });
    expect(session.model.changedOnServer).toBe(true);
  });

  it("does nothing unless the last read failed, and a second click finds the read already running", () => {
    const session = new Session();
    session.show();
    const loading = session.model;
    session.retry();
    expect(session.model).toBe(loading);
    session.fail(failure("query_failed"));
    session.retry();
    const retrying = session.model;
    session.retry();
    expect(session.model).toBe(retrying);
  });

  it("ignores an answer to a read that is not the current one", () => {
    const session = new Session();
    session.show();
    session.fail(failure("query_failed"));
    session.retry();
    session.send({ type: "loaded", generation: 1, view: view({ text: "stale" }) });
    expect(session.editor).toBe("");
    expect(session.model.request).toEqual({ generation: 2, mode: "first" });
    session.send({ type: "load-failed", generation: 1, error: failure("late") });
    expect(session.model.load).toEqual({ status: "loading" });
  });
});

describe("Reload", () => {
  it("replaces the edits with the server's text and makes the tab clean", () => {
    const session = editedTab();
    session.show();
    session.answer(view({ cas: "101", text: "first" }));
    session.reload();
    expect(session.model.request).toEqual({ generation: 2, mode: "reload" });
    session.answer(view({ cas: "102", text: "second" }));
    expect(session.editor).toBe("second");
    expect(session.tab).toMatchObject({ dirty: false, cas: "102" });
    expect(session.model.changedOnServer).toBe(false);
    expect(session.model.baseline?.eq(doc("second"))).toBe(true);
  });

  it("keeps the edits in the editor when the read fails, and a Retry then keeps them too", () => {
    const session = editedTab();
    session.show();
    session.answer();
    session.reload();
    session.fail(failure("query_failed"));
    expect(session.editor).toBe("x edits");
    expect(session.tab.dirty).toBe(true);
    session.retry();
    session.answer(view({ text: "from the server" }));
    expect(session.editor).toBe("x edits");
  });

  it("supersedes a read in flight: only the newest answer counts", () => {
    const session = editedTab();
    session.show();
    session.reload();
    session.send({ type: "loaded", generation: 1, view: view({ text: "first read" }) });
    expect(session.editor).toBe("x edits");
    session.answer(view({ text: "reloaded" }));
    expect(session.editor).toBe("reloaded");
  });

  it("turns a new document into the existing one it collided with (Task 7: Open the existing document)", () => {
    const session = new Session({ isNew: true, dirty: true, initialText: "{}" }, "my draft");
    session.reload();
    session.answer(view({ cas: "7", text: "stored" }));
    expect(session.editor).toBe("stored");
    expect(session.tab).toMatchObject({ isNew: false, dirty: false, cas: "7" });
    expect(session.load).toMatchObject({ status: "ready" });
  });

  it("starts before the tab was ever shown, and the first read that follows does not override it", () => {
    const session = editedTab();
    session.reload();
    session.show();
    expect(session.model.request).toEqual({ generation: 1, mode: "reload" });
  });
});

describe("a document that is not there", () => {
  it("keeps the edits in view when the tab holds unsaved text (ruling C30)", () => {
    const session = editedTab();
    session.show();
    session.missing();
    expect(session.load).toEqual({ status: "missing" });
    expect(panelOf(session.load, session.tab.dirty)).toBeNull();
    expect(session.editor).toBe("x edits");
    expect(session.tab.dirty).toBe(true);
    expect(session.model.baseline).toBeNull();
  });

  it("shows the missing panel when there is nothing to keep", () => {
    const session = new Session();
    session.show();
    session.missing();
    expect(panelOf(session.load, session.tab.dirty)).toBe("missing");
  });

  it("is also what a save finds when the document was deleted after it was opened", () => {
    const session = new Session();
    session.show();
    session.answer();
    session.type("edited");
    session.send({ type: "save-started" });
    session.send({ type: "deleted" });
    expect(session.load).toEqual({ status: "missing" });
    expect(session.model.saving).toBe(false);
    expect(session.editor).toBe("edited");
    expect(panelOf(session.load, session.tab.dirty)).toBeNull();
    // Undoing the edits never makes the tab clean again: the baseline is of a document that is gone.
    session.type('{"a": 1}');
    session.settle();
    expect(session.tab.dirty).toBe(true);
  });

  it("becomes a new document with the edits kept when it is created (Task 7: Create it)", () => {
    const session = editedTab();
    session.show();
    session.missing();
    session.send({ type: "created" });
    expect(session.editor).toBe("x edits");
    expect(session.tab).toMatchObject({ isNew: true, cas: null, dirty: true });
    expect(session.load).toEqual({ status: "new" });
    expect(session.model.baseline).toBeNull();
  });

  it("starts a clean tab as {} when it is created", () => {
    const session = new Session({}, "stale text of a document that is gone");
    session.show();
    session.missing();
    session.send({ type: "created" });
    expect(session.editor).toBe(NEW_DOCUMENT_TEXT);
    expect(session.tab).toMatchObject({ isNew: true, cas: null, dirty: false, initialText: NEW_DOCUMENT_TEXT });
    expect(session.model.baseline).toBeNull(); // a new document is saved even when it is still {}
  });
});

describe("what counts as unsaved", () => {
  const loaded = () => {
    const session = new Session();
    session.show();
    session.answer(view({ text: "one\ntwo" }));
    return session;
  };

  it("is anything typed, at once; typing it back to the baseline makes the tab clean once typing stops", () => {
    const session = loaded();
    session.type("one\ntwo!");
    expect(session.tab.dirty).toBe(true);
    session.settle();
    expect(session.tab.dirty).toBe(true);
    session.type("one\ntwo"); // as undo gives it back: an equal document, not the same one
    expect(session.tab.dirty).toBe(true); // not before typing stops
    session.settle();
    expect(session.tab.dirty).toBe(false);
  });

  it("changes nothing in the model for a keystroke (typing never renders the tab again)", () => {
    const session = loaded();
    session.type("x");
    const model = session.model;
    session.type("xy");
    expect(session.model).toBe(model);
  });

  it("only re-arms the check on a tab that is dirty already, and has none without a baseline", () => {
    const session = loaded();
    session.type("x");
    session.effects = [];
    session.type("xy");
    expect(session.effects).toEqual([{ kind: "check-dirty" }]);
    const restored = editedTab();
    restored.show();
    restored.answer();
    restored.effects = [];
    restored.type("more");
    expect(restored.effects).toEqual([]);
  });

  it("stays dirty for a restored tab, however the text is changed back: nothing to compare it with", () => {
    const session = editedTab();
    session.show();
    session.answer(view({ cas: "100", text: "stored" }));
    session.type("stored");
    session.settle();
    expect(session.tab.dirty).toBe(true);
  });

  it("stays dirty for a new document until it is saved", () => {
    const session = new Session({ isNew: true, initialText: NEW_DOCUMENT_TEXT }, NEW_DOCUMENT_TEXT);
    session.show();
    session.type('{"a": 1}');
    session.settle();
    expect(session.tab.dirty).toBe(true);
  });

  it("is never found out by a check that comes after the tab turned clean on its own", () => {
    const session = loaded();
    session.send({ type: "settled", doc: doc("one\ntwo") });
    expect(session.effects.filter((effect) => effect.kind === "patch-tab")).toHaveLength(1); // only the load's
  });
});

describe("saving", () => {
  const readyTab = () => {
    const session = new Session();
    session.show();
    session.answer(view({ cas: "100", text: "one" }));
    return session;
  };

  it("makes what was sent the clean state, with the new CAS", () => {
    const session = readyTab();
    session.type("two");
    session.send({ type: "save-started" });
    expect(session.model.saving).toBe(true);
    session.send({ type: "saved", meta: meta("200"), baseline: doc("two"), doc: doc("two") });
    expect(session.tab).toMatchObject({ cas: "200", dirty: false, isNew: false });
    expect(session.model).toMatchObject({ saving: false, changedOnServer: false });
    expect(session.load).toEqual({ status: "ready", meta: meta("200") });
    session.type("three");
    session.type("two");
    session.settle();
    expect(session.tab.dirty).toBe(false); // back to what was saved
  });

  it("leaves the tab dirty when the text is not what was sent", () => {
    const session = readyTab();
    session.type("two");
    session.send({ type: "save-started" });
    session.send({ type: "saved", meta: meta("200"), baseline: doc("two"), doc: doc("two and more") });
    expect(session.tab).toMatchObject({ cas: "200", dirty: true });
  });

  it("makes a new document an existing one", () => {
    const session = new Session({ isNew: true, initialText: NEW_DOCUMENT_TEXT }, NEW_DOCUMENT_TEXT);
    session.show();
    session.send({ type: "save-started" });
    session.send({ type: "saved", meta: meta("5"), baseline: doc("{}"), doc: doc("{}") });
    expect(session.tab).toMatchObject({ isNew: false, cas: "5", dirty: false });
    expect(session.load).toMatchObject({ status: "ready" });
  });

  it("reports a conflict without losing the text, and says so until a reload", () => {
    const session = readyTab();
    session.type("two");
    session.send({ type: "save-started" });
    session.send({ type: "conflict" });
    expect(session.model).toMatchObject({ saving: false, changedOnServer: true });
    expect(session.editor).toBe("two");
    expect(session.tab).toMatchObject({ dirty: true, cas: "100" });
    session.reload();
    session.answer(view({ cas: "101", text: "theirs" }));
    expect(session.model.changedOnServer).toBe(false);
    expect(session.editor).toBe("theirs");
  });

  it("starts no read while a write is in flight, and a write that ended without result frees it", () => {
    const session = readyTab();
    session.type("two");
    session.send({ type: "save-started" });
    const saving = session.model;
    session.reload();
    session.retry();
    expect(session.model).toBe(saving);
    session.send({ type: "save-ended" });
    session.reload();
    expect(session.model.request).toMatchObject({ mode: "reload" });
  });
});

describe("what hides the editor, and when it can be typed in", () => {
  const ready = (type: DocumentMeta["type"]): DocumentLoad => ({ status: "ready", meta: { ...meta("1"), type } });

  it("hides it behind a panel while loading, after an error, for a missing or a binary document", () => {
    expect(panelOf({ status: "loading" }, false)).toBe("loading");
    expect(panelOf({ status: "loading" }, true)).toBe("loading");
    expect(panelOf({ status: "error", error: new Error("e") }, true)).toBe("error");
    expect(panelOf({ status: "missing" }, false)).toBe("missing");
    expect(panelOf(ready("base64"), false)).toBe("binary");
  });

  it("shows it for a document, a new one, and a missing one that holds unsaved text", () => {
    expect(panelOf(ready("json"), false)).toBeNull();
    expect(panelOf({ status: "new" }, false)).toBeNull();
    expect(panelOf({ status: "missing" }, true)).toBeNull();
  });

  it("allows typing for a JSON document or a new one, never on a read-only connection", () => {
    expect(canEdit(ready("json"), false)).toBe(true);
    expect(canEdit({ status: "new" }, false)).toBe(true);
    expect(canEdit(ready("json"), true)).toBe(false);
    expect(canEdit({ status: "new" }, true)).toBe(false);
    expect(canEdit(ready("base64"), false)).toBe(false);
    expect(canEdit({ status: "missing" }, false)).toBe(false);
    expect(canEdit({ status: "loading" }, false)).toBe(false);
  });
});
