import { describe, expect, it, vi } from "vitest";
import type { SavedTab, SavedWorkspace } from "@/lib/api/types";
import type { EditorBuffer } from "@/stores/editorBuffers";
import type { DocumentTabState, QueryTabState, Tab } from "@/stores/workspace";
import { fromSavedWorkspace, restoreLosses, toSavedWorkspace, workspaceJson } from "./workspaceFile";

/*
 * What `workspace.json` keeps of a Couchbase document tab (rulings C15, C33): unsaved text and the CAS it is based on,
 * nothing for a clean document, and never a CAS the backend would refuse, which would fail every save of the file.
 */

const PROFILE = "6f1c2c1e-0000-4000-8000-000000000001";
const PATH = [
  { kind: "bucket", name: "dbaguy" },
  { kind: "scope", name: "app" },
  { kind: "collection", name: "users" },
  { kind: "document", name: "u 1/😀" },
];

const documentTab = (overrides: Partial<DocumentTabState> = {}): DocumentTabState => ({
  id: "document:1",
  kind: "document",
  profileId: PROFILE,
  path: PATH,
  title: "u 1/😀",
  isNew: false,
  cas: "1792325990605258752",
  dirty: false,
  initialText: null,
  ...overrides,
});

const save = (tab: Tab, editor: string | null = null) => {
  const reads: string[] = [];
  const readBuffer = (id: string): EditorBuffer | null => {
    reads.push(id);
    return editor === null ? null : { text: editor, selection: { anchor: 0, head: 0 } };
  };
  const file = toSavedWorkspace({ tabs: [tab], activeTabId: tab.id, sidebarTab: null }, readBuffer);
  return { saved: file.tabs[0], reads };
};

const restore = (entry: Partial<SavedTab>, profileExists = true) => {
  const saved: SavedWorkspace = {
    version: 1,
    tabs: [{ id: "d1", kind: "document", title: "u1", profile_id: PROFILE, object_path: PATH, ...entry }],
    active_tab_id: null,
    sidebar_tab: null,
  };
  return fromSavedWorkspace(saved, () => profileExists);
};

describe("saving a document tab", () => {
  it("keeps no text for a clean document, and does not even read its editor", () => {
    const { saved, reads } = save(documentTab(), "a 20 MiB document");
    expect(saved).toMatchObject({ kind: "document", object_path: PATH, document_cas: "1792325990605258752", buffer: null });
    expect(reads).toEqual([]);
  });

  it("keeps the editor's text with the CAS it is based on once the document is edited", () => {
    const { saved } = save(documentTab({ dirty: true }), '{"edited": true}');
    expect(saved).toMatchObject({ buffer: '{"edited": true}', document_cas: "1792325990605258752" });
  });

  it("keeps a new document's text without a CAS, falling back to what the tab started with", () => {
    expect(save(documentTab({ isNew: true, cas: null }), "{}").saved).toMatchObject({ buffer: "{}", document_cas: null });
    expect(save(documentTab({ isNew: true, cas: null, initialText: "{}" })).saved.buffer).toBe("{}");
  });

  it.each(["", "0", "012", "12ab", " 12", "-5", "1".repeat(20), "1.5"])("writes no CAS the backend would refuse: %j", (cas) => {
    expect(save(documentTab({ dirty: true, cas }), "x").saved.document_cas).toBeNull();
  });

  it("writes the largest CAS the pattern takes", () => {
    expect(save(documentTab({ cas: "9".repeat(19) })).saved.document_cas).toBe("9".repeat(19));
  });
});

describe("restoring a document tab", () => {
  it("brings back unsaved edits to an existing document as a dirty tab with its CAS", () => {
    const { snapshot } = restore({ buffer: '{"edited": true}', document_cas: "100" });
    expect(snapshot.tabs).toEqual([
      documentTab({ id: "d1", title: "u1", cas: "100", dirty: true, isNew: false, initialText: '{"edited": true}' }),
    ]);
  });

  it("brings back a new document as new, and a clean one as a tab that reads again", () => {
    expect(restore({ buffer: "{}", document_cas: null }).snapshot.tabs[0]).toMatchObject({
      isNew: true,
      dirty: true,
      cas: null,
      initialText: "{}",
    });
    expect(restore({ buffer: null, document_cas: "100" }).snapshot.tabs[0]).toMatchObject({
      isNew: false,
      dirty: false,
      cas: "100",
      initialText: null,
    });
  });

  it("lists a tab whose connection is gone, or whose path names no document, as closed", () => {
    expect(restore({}, false)).toMatchObject({ snapshot: { tabs: [] }, closed: ["u1"] });
    expect(restore({ object_path: PATH.slice(0, 3) })).toMatchObject({ snapshot: { tabs: [] }, closed: ["u1"] });
    expect(restore({ object_path: PATH.slice(1) })).toMatchObject({ snapshot: { tabs: [] }, closed: ["u1"] });
    expect(restore({ object_path: null })).toMatchObject({ snapshot: { tabs: [] }, closed: ["u1"] });
  });

  it("round-trips what it saved", () => {
    const tab = documentTab({ dirty: true, initialText: null });
    const { saved } = save(tab, "typed");
    const { snapshot } = restore({ ...saved, id: tab.id });
    expect(snapshot.tabs[0]).toEqual({ ...tab, initialText: "typed" });
  });
});

describe("restoring a document tab whose connection is gone (M6)", () => {
  const DETACHED = "u 1/😀 (unsaved document text, in a query tab)";

  it.each([
    ["edits to an existing document", { buffer: '{"edited": true}', document_cas: "100" }, '{"edited": true}'],
    ["a new document", { buffer: "{}", document_cas: null }, "{}"],
  ])("reopens the unsaved text of %s in a query tab without a connection, titled after the ID", (_, entry, text) => {
    const { snapshot, closed, detached } = restore(entry, false);
    expect(snapshot.tabs).toEqual([
      {
        id: "d1",
        kind: "query",
        title: "u 1/😀",
        profileId: null,
        database: null,
        scriptPath: null,
        savedText: null,
        textFromFile: false,
        initialText: text,
        initialCursor: undefined,
        initialSelection: undefined,
        autocommit: true,
        queryContext: undefined,
        params: undefined,
      },
    ]);
    expect(snapshot.activeTabId).toBe("d1");
    expect({ closed, detached }).toEqual({ closed: [], detached: [DETACHED] });
  });

  it("keeps that text in the next save of the workspace", () => {
    const [tab] = restore({ buffer: '{"edited": true}', document_cas: "100" }, false).snapshot.tabs;
    expect(save(tab).saved).toMatchObject({ kind: "query", profile_id: null, buffer: '{"edited": true}' });
  });

  it("closes a clean one, and says so: the server has its text", () => {
    expect(restore({ buffer: null, document_cas: "100" }, false)).toMatchObject({
      snapshot: { tabs: [] },
      closed: ["u1"],
      detached: [],
    });
  });

  it("keeps the text of a tab saved without a connection too", () => {
    expect(restore({ profile_id: null, buffer: "{}" }).detached).toEqual([DETACHED]);
  });
});

/* A table tab's staged grid edits (spec ⑤ §3.7): kept beside the tab, restored into the staged-edits store. */

describe("a table tab's staged edits", () => {
  const TABLE_PATH = [
    { kind: "schema", name: "public" },
    { kind: "folder", name: "tables" },
    { kind: "table", name: "orders" },
  ];
  const tableTab: Tab = { id: "table:1", kind: "table", profileId: PROFILE, path: TABLE_PATH, title: "orders" };
  const file = {
    version: 1 as const,
    key_columns: ["id"],
    updates: [],
    inserts: [{ id: "i:1", values: {} }],
    deletes: [{ id: "d:2", key: { id: 7 } }],
    outcome_unknown: false,
  };
  const workspace = (entry: Partial<SavedTab>): SavedWorkspace => ({
    version: 1,
    tabs: [{ id: "table:1", kind: "table", title: "orders", profile_id: PROFILE, object_path: TABLE_PATH, ...entry }],
    active_tab_id: null,
    sidebar_tab: null,
  });

  it("are saved from the staged-edits store, and only when there are some", () => {
    const save = (staged: typeof file | null) =>
      toSavedWorkspace(
        { tabs: [tableTab], activeTabId: null, sidebarTab: null },
        () => null,
        () => staged,
      ).tabs[0];
    expect(save(file)).toMatchObject({ kind: "table", staged_edits: file });
    expect(save(null)).not.toHaveProperty("staged_edits");
  });

  it("are restored beside their tab", () => {
    const restored = fromSavedWorkspace(workspace({ staged_edits: file }), () => true);
    expect(restored.snapshot.tabs).toEqual([tableTab]);
    expect(restored.staged.get("table:1")).toEqual(file);
  });

  it("go with a tab whose connection is gone, and the closed list says how many", () => {
    const restored = fromSavedWorkspace(workspace({ staged_edits: file }), () => false);
    expect(restored.closed).toEqual(["orders (2 staged changes discarded)"]);
    expect(restored.staged.size).toBe(0);
  });

  it("count an UPDATE under its row's DELETE once in that list, as the close question does (L20)", () => {
    const hidden = {
      ...file,
      updates: [{ id: "u:1", key: { id: 7 }, set: { a: { kind: "null" as const } }, original: { a: 1 } }],
    };
    const restored = fromSavedWorkspace(workspace({ staged_edits: hidden }), () => false);
    expect(restored.closed).toEqual(["orders (2 staged changes discarded)"]); // the new row and the delete, not three
  });

  it("that do not read (a local copy is only checked for its tabs) cost that tab its staged edits, not the restore", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      const secret = "secret-value";
      // a key missing, as a damaged or older local copy could have it
      const malformed = {
        ...file,
        updates: [{ id: "u:1", set: { a: { kind: "value", text: secret } }, original: { a: 1 } }],
      };
      const saved: SavedWorkspace = {
        version: 1,
        tabs: [
          {
            id: "table:bad",
            kind: "table",
            title: "bad",
            profile_id: PROFILE,
            object_path: TABLE_PATH,
            staged_edits: malformed as never,
          },
          {
            id: "table:1",
            kind: "table",
            title: "orders",
            profile_id: PROFILE,
            object_path: TABLE_PATH,
            staged_edits: file,
          },
          {
            id: "table:gone",
            kind: "table",
            title: "gone",
            profile_id: null,
            object_path: TABLE_PATH,
            staged_edits: malformed as never,
          },
        ],
        active_tab_id: null,
        sidebar_tab: null,
      };
      const restored = fromSavedWorkspace(saved, (id) => id === PROFILE);
      expect(restored.snapshot.tabs.map((tab) => tab.id)).toEqual(["table:bad", "table:1"]); // the tab stays
      expect([...restored.staged.keys()]).toEqual(["table:1"]); // the good set stays, the malformed one is dropped
      expect(restored.closed).toEqual(["gone"]); // a dropped tab with a set that does not read: no count to give
      // the restore's toast names the tab that lost its staged edits, beside the closed one, and never a value
      expect(restored.dropped).toEqual(["bad: its staged changes could not be read and were dropped"]);
      expect(restoreLosses(restored)).toBe(
        "Some tabs' connections no longer exist.\nClosed: gone\nbad: its staged changes could not be read and were dropped",
      );
      expect(restoreLosses({ ...restored, closed: [] })).toBe(
        "bad: its staged changes could not be read and were dropped",
      );
      expect(restoreLosses({ ...restored, closed: [], dropped: [] })).toBeNull();
      const said = warn.mock.calls.flat().join(" ");
      expect(warn).toHaveBeenCalledTimes(2);
      expect(said).toContain("TypeError"); // the kind of error
      expect(said).not.toContain(secret); // never a value
    } finally {
      warn.mockRestore();
    }
  });

  it("are restored for a table tab only", () => {
    const restored = fromSavedWorkspace(workspace({ kind: "object", staged_edits: file }), () => true);
    expect(restored.snapshot.tabs).toHaveLength(1);
    expect(restored.staged.size).toBe(0);
  });
});

/*
 * Ruling K7: a lone surrogate (half an emoji, from a broken paste) cannot be written as UTF-8, and one in a tab's text
 * would fail the backend's write of `workspace.json` for every tab (Q2, B40). The stored copy has U+FFFD in its place;
 * the open editor keeps its exact text, and no other tab changes.
 */

describe("a lone surrogate in a tab's text", () => {
  const queryTab = (id: string, overrides: Partial<QueryTabState> = {}): QueryTabState => ({
    id,
    kind: "query",
    title: id,
    profileId: PROFILE,
    database: null,
    scriptPath: null,
    savedText: null,
    textFromFile: false,
    initialText: "",
    autocommit: true,
    ...overrides,
  });
  const EMOJI = "\u{1F600}";
  const editors: Record<string, EditorBuffer> = {
    broken: { text: `a\uD800b${EMOJI}\uD83D`, selection: { anchor: 0, head: 0 } },
    fine: { text: `SELECT '${EMOJI}'`, selection: { anchor: 0, head: 0 } },
  };

  /** What the backend is sent, as it reads it. */
  const stored = (tabs: Tab[]) => {
    const file = toSavedWorkspace({ tabs, activeTabId: null, sidebarTab: null }, (id) => editors[id] ?? null);
    const json = workspaceJson(file);
    return { file, json, saved: JSON.parse(json) as SavedWorkspace };
  };

  it("is stored as U+FFFD, in the stored copy only, and the other tabs are untouched", () => {
    const { file, json, saved } = stored([queryTab("broken"), queryTab("fine")]);
    expect(saved.tabs.map((tab) => tab.buffer)).toEqual([`a\uFFFDb${EMOJI}\uFFFD`, `SELECT '${EMOJI}'`]);
    expect(json).not.toMatch(/\\ud[89a-f]/i); // not even as an escape: the backend would decode it to the same surrogate
    expect(editors.broken.text).toBe(`a\uD800b${EMOJI}\uD83D`); // the open editor keeps its exact text
    expect(file.tabs[0].buffer).toBe(`a\uD800b${EMOJI}\uD83D`); // the state handed over is the editor's, as it is
    expect(saved.tabs[1]).toEqual(JSON.parse(JSON.stringify(file.tabs[1]))); // nothing else changed
  });

  it("comes back with U+FFFD where the broken half was", () => {
    const { saved } = stored([queryTab("broken")]);
    const restored = fromSavedWorkspace(saved, () => true);
    expect(restored.snapshot.tabs[0]).toMatchObject({ kind: "query", initialText: `a\uFFFDb${EMOJI}\uFFFD` });
  });

  it("is replaced in every kind of text: a script's, a document's and a parameter draft", () => {
    const scripted = queryTab("script", { scriptPath: "a.sql", savedText: "SELECT 1" });
    const withParams = queryTab("params", { params: { $a: "x\uDC00y" } });
    const document = documentTab({ isNew: true, cas: null, initialText: '{"a": "\uD800"}' });
    editors.script = { text: "SELECT '\uDC00'", selection: { anchor: 0, head: 0 } };
    editors[document.id] = { text: '{"a": "\uD800"}', selection: { anchor: 0, head: 0 } };
    const { saved } = stored([scripted, withParams, document]);
    expect(saved.tabs[0].buffer).toBe("SELECT '\uFFFD'");
    expect(saved.tabs[1].params).toEqual({ $a: "x\uFFFDy" });
    expect(saved.tabs[2].buffer).toBe('{"a": "\uFFFD"}');
  });
});
