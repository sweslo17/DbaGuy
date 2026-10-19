import { describe, expect, it } from "vitest";
import type { SavedTab, SavedWorkspace } from "@/lib/api/types";
import type { EditorBuffer } from "@/stores/editorBuffers";
import type { DocumentTabState, Tab } from "@/stores/workspace";
import { fromSavedWorkspace, toSavedWorkspace } from "./workspaceFile";

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
