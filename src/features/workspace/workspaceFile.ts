import type { PathSegment, SavedTab, SavedWorkspace, StagedEditsFile, TextSelection } from "@/lib/api/types";
import { plural } from "@/lib/format";
import { PARAM_NAME } from "@/lib/paramNames";
import { matchesFile } from "@/lib/scriptText";
import { wellFormed } from "@/lib/unicodeText";
import type { EditorBuffer } from "@/stores/editorBuffers";
import {
  startSelection,
  type DocumentTabState,
  type QueryTabState,
  type Tab,
  type WorkspaceSnapshot,
} from "@/stores/workspace";
import { restoredEdits } from "../edits/savedEdits";
import { changeCount } from "../edits/staging";
import { keyspaceOfPath } from "../explorer/fieldSamples";

/*
 * `workspace.json` ↔ the tab store (spec §4.8). A query tab keeps its connection, script binding, editor text,
 * cursor/selection and auto-commit mode; a table or object tab keeps its connection and object path. A tab bound to a
 * script keeps no text while it holds just what its file does (`buffer: null`, ruling R-02c-21): it is restored from
 * the file as the file is then. A Couchbase query tab also keeps its query context: null is the profile's default,
 * "none" the user's choice of no context (ruling B14). A tab that uses SQL++ parameters keeps its panel's drafts, the
 * text as typed, so a crash or a restart loses none of them (`savedParams`). A Couchbase document tab keeps its
 * connection, its path (the collection, then the document), the CAS its text is based on, and its text only while it
 * is unsaved (a new document, or edited): a clean document is read again from the server (ruling C15). Unsaved document
 * text whose connection is gone comes back in a query tab without a connection (M6). A table tab keeps its staged grid
 * edits (spec ⑤ §3.7) as `savedEdits` gives them: they are not tab state, so they are read and restored beside it.
 */

const WORKSPACE_VERSION = 1;

/**
 * The JSON `PUT /workspace` sends: every string well-formed (a title, a name or the text alike), each lone surrogate
 * replaced by U+FFFD (a lone surrogate fails the backend's write of the file, for every tab). The stored copy only: the
 * open editor keeps its exact text, and a restore shows the replacement where the broken half was.
 */
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

/** A table tab's staged edits as `workspace.json` keeps them (`savedEdits`), or null. */
export type ReadStaged = (tabId: string) => StagedEditsFile | null;

function savedTab(tab: Tab, readBuffer: (tabId: string) => EditorBuffer | null, readStaged: ReadStaged): SavedTab {
  if (tab.kind === "document") return savedDocument(tab, readBuffer);
  if (tab.kind === "table") {
    const saved = { id: tab.id, kind: tab.kind, title: tab.title, profile_id: tab.profileId, object_path: tab.path };
    const staged = readStaged(tab.id);
    return staged ? { ...saved, staged_edits: staged } : saved;
  }
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

/**
 * The workspace as `PUT /workspace` takes it; `readBuffer` gives a query tab's current editor content, `readStaged` a
 * table tab's staged edits.
 */
export function toSavedWorkspace(
  snapshot: WorkspaceSnapshot,
  readBuffer: (tabId: string) => EditorBuffer | null,
  readStaged: ReadStaged = () => null,
): SavedWorkspace {
  return {
    version: WORKSPACE_VERSION,
    tabs: snapshot.tabs.map((tab) => savedTab(tab, readBuffer, readStaged)),
    active_tab_id: snapshot.activeTabId,
    sidebar_tab: snapshot.sidebarTab,
  };
}

export interface RestoredWorkspace {
  snapshot: WorkspaceSnapshot;
  /**
   * Titles of the table, object and document tabs dropped because their connection or path no longer exists (a
   * document tab only when it held no unsaved text, or its path names no document).
   */
  closed: string[];
  /**
   * Titles of the query tabs reopened without a connection because theirs no longer exists, and of the document tabs
   * whose unsaved text was reopened that way (said as such).
   */
  detached: string[];
  /** The staged edits of the table tabs restored, by tab id (they go into `stores/stagedEdits.ts`). */
  staged: ReadonlyMap<string, StagedEditsFile>;
}

/** The changes a staged set holds as the close question counts them (L20): an UPDATE under its DELETE is not one. */
const stagedCount = (file: StagedEditsFile) => changeCount(restoredEdits(file));

/** A query tab as saved; with `profileId` null it reopens without a connection (nor its database or query context). */
function restoredQuery(entry: SavedTab, profileId: string | null): QueryTabState {
  const connected = profileId !== null;
  const scriptPath = entry.script_path ?? null;
  return {
    id: entry.id,
    kind: "query",
    title: entry.title || "Query",
    profileId,
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
  };
}

/**
 * A document tab whose connection is gone, holding unsaved text (a new document, or edits): that text in a query tab
 * without a connection, titled after the document's ID, so the edits can still be copied and the next save of the
 * workspace keeps them (Q2, M6). Null for a clean one: the server has its text, nothing is lost.
 */
function detachedDocumentText(entry: SavedTab): QueryTabState | null {
  if (entry.buffer == null) return null;
  const id = entry.object_path?.at(-1);
  const title = id?.kind === "document" ? id.name : entry.title;
  return restoredQuery({ id: entry.id, kind: "query", title, buffer: entry.buffer }, null);
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
 * connection (nor its query context, but it keeps its parameter drafts); so does a document tab's unsaved text, in a
 * query tab (`detachedDocumentText`); a table or object tab, or a document tab with nothing unsaved, whose profile is
 * gone is dropped. A saved auto-commit of null means on: the tab's mode must be definite, or nothing could run.
 */
export function fromSavedWorkspace(saved: SavedWorkspace, profileExists: (id: string) => boolean): RestoredWorkspace {
  const tabs: Tab[] = [];
  const closed: string[] = [];
  const detached: string[] = [];
  const staged = new Map<string, StagedEditsFile>();
  const seen = new Set<string>();
  for (const entry of saved.tabs) {
    if (seen.has(entry.id)) continue; // React keys and close guards need unique ids
    seen.add(entry.id);
    const profileId = entry.profile_id ?? null;
    const connected = profileId !== null && profileExists(profileId);
    if (entry.kind === "query") {
      if (profileId !== null && !connected) detached.push(entry.title);
      tabs.push(restoredQuery(entry, connected ? profileId : null));
    } else if (entry.kind === "document") {
      const document = connected ? restoredDocument(entry, profileId) : null;
      const kept = connected ? null : detachedDocumentText(entry);
      if (document) {
        tabs.push(document);
      } else if (kept) {
        tabs.push(kept);
        detached.push(`${kept.title} (unsaved document text, in a query tab)`);
      } else {
        closed.push(entry.title);
      }
    } else if (connected && entry.object_path?.length) {
      tabs.push({ id: entry.id, kind: entry.kind, profileId, path: entry.object_path, title: entry.title });
      if (entry.kind === "table" && entry.staged_edits) staged.set(entry.id, entry.staged_edits);
    } else {
      // R-05-11: a table tab's staged edits go with it, and the list of closed tabs says so.
      const lost = entry.staged_edits ? stagedCount(entry.staged_edits) : 0;
      closed.push(lost ? `${entry.title} (${plural(lost, "staged change")} discarded)` : entry.title);
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
    staged,
  };
}
