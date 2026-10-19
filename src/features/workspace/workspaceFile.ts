import type { PathSegment, SavedTab, SavedWorkspace, TextSelection } from "@/lib/api/types";
import { PARAM_NAME } from "@/lib/paramNames";
import { matchesFile } from "@/lib/scriptText";
import type { EditorBuffer } from "@/stores/editorBuffers";
import {
  startSelection,
  type DocumentTabState,
  type QueryTabState,
  type Tab,
  type WorkspaceSnapshot,
} from "@/stores/workspace";
import { keyspaceOfPath } from "../explorer/fieldSamples";

/*
 * `workspace.json` ↔ the tab store (spec §4.8). A query tab keeps its connection, script binding, editor text,
 * cursor/selection and auto-commit mode; a table or object tab keeps its connection and object path. A tab bound to a
 * script keeps no text while it holds just what its file does (`buffer: null`, ruling R-02c-21): it is restored from
 * the file as the file is then. A Couchbase query tab also keeps its query context: null is the profile's default,
 * "none" the user's choice of no context (ruling B14). A tab that uses SQL++ parameters keeps its panel's drafts, the
 * text as typed, so a crash or a restart loses none of them (`savedParams`). A Couchbase document tab keeps its
 * connection, its path (the collection, then the document), the CAS its text is based on, and its text only while it
 * is unsaved (a new document, or edited): a clean document is read again from the server (ruling C15).
 */

const WORKSPACE_VERSION = 1;

/**
 * Lone UTF-16 surrogates (a broken paste, an odd object name): the backend cannot write them as UTF-8, which would
 * fail every save.
 */
const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g;

const wellFormed = (text: string) => text.replace(LONE_SURROGATE, "\uFFFD");

/** The JSON `PUT /workspace` sends: every string well-formed (a title, a name or the text alike). */
export function workspaceJson(file: SavedWorkspace): string {
  return JSON.stringify(file, (_key, value: unknown) => (typeof value === "string" ? wellFormed(value) : value));
}

/** A key of `WorkspaceTab.params`: `$name` or `$n`, as the backend's model takes it (`domain/workspace.py`). */
const DRAFT_NAME = new RegExp(`^\\$(?:${PARAM_NAME}|[1-9][0-9]{0,2})$`);
const MAX_DRAFT_CHARS = 1_048_576;
const MAX_DRAFTS = 400;

/**
 * The drafts of a parameter panel as `workspace.json` keeps them, null when none is left. `PUT /workspace` validates
 * the whole state, so one draft the backend refuses would fail every save with a 422 and a crash would lose every
 * tab's unsaved text (Q2, ruling B40): only keys the backend's pattern accepts, texts of at most 1,048,576 characters
 * (counted in UTF-16 units, which never undercounts: a longer text is over the 1 MiB a literal may have anyway) and at
 * most 400 entries, the earliest ones, are written. It also reads what a restored file or local copy holds.
 */
export function savedParams(
  drafts: Readonly<Record<string, string>> | null | undefined,
): Record<string, string> | null {
  if (!drafts) return null;
  const kept: Record<string, string> = {};
  let count = 0;
  for (const [name, text] of Object.entries(drafts)) {
    if (count === MAX_DRAFTS) break;
    if (typeof text !== "string" || text.length > MAX_DRAFT_CHARS || !DRAFT_NAME.test(name)) continue;
    kept[name] = text;
    count += 1;
  }
  return count > 0 ? kept : null;
}

/**
 * What the backend takes as `document_cas` (`CAS_PATTERN` in `domain/couchbase.py`): decimal text, 1 to 19 digits, no
 * leading zero. `PUT /workspace` validates the whole state, so a value it refuses would fail every save and a crash
 * would lose every tab's unsaved text (Q2, rulings B40 and C33).
 */
const CAS = /^[1-9][0-9]{0,18}$/;

/** A document tab's CAS as `workspace.json` keeps it: null unless the backend accepts it. */
const savedCas = (cas: string | null): string | null => (cas !== null && CAS.test(cas) ? cas : null);

/** The tab's text and selection as last known: its editor's, else what it was restored with. */
function contentOf(tab: QueryTabState, buffer: EditorBuffer | null): { text: string; selection: TextSelection | null } {
  return buffer ?? { text: tab.initialText, selection: startSelection(tab) ?? null };
}

/**
 * The text `workspace.json` keeps for a query tab; null for a bound tab whose text is its file's. That includes a
 * restored one whose file is not read yet: its editor is an empty stand-in, never the user's text, whether it was
 * never shown, is mirrored to local storage or flushed as the page goes away.
 */
function bufferToSave(tab: QueryTabState, text: string): string | null {
  if (tab.scriptPath === null) return text;
  if (tab.textFromFile) return null;
  return tab.savedText !== null && matchesFile(text, tab.savedText) ? null : text;
}

/**
 * A document tab: the editor's text only when there is something to keep (a new document, or unsaved changes), with
 * the CAS it is based on; a clean document keeps neither a text nor a buffer read (a 20 MiB one is never read here).
 */
function savedDocument(tab: DocumentTabState, readBuffer: (tabId: string) => EditorBuffer | null): SavedTab {
  const keepsText = tab.isNew || tab.dirty;
  return {
    id: tab.id,
    kind: "document",
    title: tab.title,
    profile_id: tab.profileId,
    object_path: tab.path,
    document_cas: savedCas(tab.cas),
    buffer: keepsText ? (readBuffer(tab.id)?.text ?? tab.initialText ?? "") : null,
  };
}

function savedTab(tab: Tab, readBuffer: (tabId: string) => EditorBuffer | null): SavedTab {
  if (tab.kind === "document") return savedDocument(tab, readBuffer);
  if (tab.kind !== "query") {
    return { id: tab.id, kind: tab.kind, title: tab.title, profile_id: tab.profileId, object_path: tab.path };
  }
  // Waiting for its file, the empty editor's cursor means nothing: the restored one is kept.
  const { text, selection } = contentOf(tab, tab.textFromFile ? null : readBuffer(tab.id));
  return {
    id: tab.id,
    kind: "query",
    title: tab.title,
    profile_id: tab.profileId,
    database: tab.database,
    script_path: tab.scriptPath,
    buffer: bufferToSave(tab, text),
    cursor: selection?.head ?? null,
    // Only a real range: a bare cursor is `cursor`.
    selection: selection && selection.anchor !== selection.head ? selection : null,
    autocommit: tab.autocommit,
    query_context: tab.queryContext ?? null,
    params: savedParams(tab.params),
  };
}

/** The workspace as `PUT /workspace` takes it; `readBuffer` gives a query tab's current editor content. */
export function toSavedWorkspace(
  snapshot: WorkspaceSnapshot,
  readBuffer: (tabId: string) => EditorBuffer | null,
): SavedWorkspace {
  return {
    version: WORKSPACE_VERSION,
    tabs: snapshot.tabs.map((tab) => savedTab(tab, readBuffer)),
    active_tab_id: snapshot.activeTabId,
    sidebar_tab: snapshot.sidebarTab,
  };
}

export interface RestoredWorkspace {
  snapshot: WorkspaceSnapshot;
  /** Titles of the table, object and document tabs dropped because their connection or path no longer exists. */
  closed: string[];
  /** Titles of the query tabs reopened without a connection because theirs no longer exists. */
  detached: string[];
}

/**
 * A document tab as saved, or null when its path does not name a document (a collection and an ID). Its text, when
 * kept, is unsaved: with a CAS it is edits to that document, without one a new document.
 */
function restoredDocument(entry: SavedTab, profileId: string): DocumentTabState | null {
  const path: PathSegment[] = entry.object_path ?? [];
  const id = path.at(-1);
  if (keyspaceOfPath(path) === null || id?.kind !== "document") return null;
  const cas = entry.document_cas ?? null;
  const text = entry.buffer ?? null;
  return {
    id: entry.id,
    kind: "document",
    profileId,
    path,
    title: entry.title || id.name,
    isNew: cas === null && text !== null,
    cas,
    dirty: text !== null,
    initialText: text,
  };
}

/**
 * The saved workspace as store tabs, keeping their ids. A query tab whose profile is gone reopens without a
 * connection (nor its query context, but it keeps its parameter drafts); a table, object or document tab whose profile
 * is gone is dropped. A saved auto-commit of null means on: the tab's mode must be definite, or nothing could run.
 */
export function fromSavedWorkspace(saved: SavedWorkspace, profileExists: (id: string) => boolean): RestoredWorkspace {
  const tabs: Tab[] = [];
  const closed: string[] = [];
  const detached: string[] = [];
  const seen = new Set<string>();
  for (const entry of saved.tabs) {
    if (seen.has(entry.id)) continue; // React keys and close guards need unique ids
    seen.add(entry.id);
    const profileId = entry.profile_id ?? null;
    const connected = profileId !== null && profileExists(profileId);
    if (entry.kind === "query") {
      if (profileId !== null && !connected) detached.push(entry.title);
      const scriptPath = entry.script_path ?? null;
      tabs.push({
        id: entry.id,
        kind: "query",
        title: entry.title || "Query",
        profileId: connected ? profileId : null,
        database: connected ? (entry.database ?? null) : null,
        scriptPath,
        savedText: null, // unknown until the script is read again
        textFromFile: scriptPath !== null && entry.buffer == null,
        initialText: entry.buffer ?? "",
        initialCursor: entry.cursor ?? undefined,
        initialSelection: entry.selection ?? undefined,
        autocommit: entry.autocommit ?? true,
        queryContext: connected ? (entry.query_context ?? undefined) : undefined,
        params: savedParams(entry.params) ?? undefined,
      });
    } else if (entry.kind === "document") {
      const document = connected ? restoredDocument(entry, profileId) : null;
      if (document) tabs.push(document);
      else closed.push(entry.title);
    } else if (connected && entry.object_path?.length) {
      tabs.push({ id: entry.id, kind: entry.kind, profileId, path: entry.object_path, title: entry.title });
    } else {
      closed.push(entry.title);
    }
  }
  const active = saved.active_tab_id ?? null;
  return {
    snapshot: {
      tabs,
      activeTabId: tabs.some((tab) => tab.id === active) ? active : (tabs[0]?.id ?? null),
      sidebarTab: saved.sidebar_tab ?? null,
    },
    closed,
    detached,
  };
}
