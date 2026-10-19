import type { QueryClient } from "@tanstack/react-query";
import { create } from "zustand";
import { toast } from "@/components/ui/Toast";
import { api, ApiError, errorMessage, unwrap } from "@/lib/api/client";
import type { SavedWorkspace } from "@/lib/api/types";
import { getBackendInfo, isTauriRuntime, type BackendInfo } from "@/lib/backend";
import { forgetEditorBuffersExcept, onEdited, readEditorBuffer } from "@/stores/editorBuffers";
import { forgetStagedEditsExcept, onStagedEditsChanged, putStagedEdits, readStagedEdits } from "@/stores/stagedEdits";
import { useWorkspace } from "@/stores/workspace";
import { fetchProfiles, profilesKey } from "../connections/hooks";
import { restoredEdits, savedEdits } from "../edits/savedEdits";
import { clearPendingWorkspace, readPendingWorkspace, writePendingWorkspace } from "./pendingWorkspace";
import { fromSavedWorkspace, toSavedWorkspace, workspaceJson, type RestoredWorkspace } from "./workspaceFile";

/** A change is saved at most this long after it was made, so a crash loses at most the last half second of typing. */
const SAVE_DELAY_MS = 500;
const SAVE_TIMEOUT_MS = 10_000;
/** A failed save is retried after 1 s, 2 s, 4 s, then every 5 s: a refused localhost connection costs nothing. */
const RETRY_FIRST_MS = 1000;
const RETRY_MAX_MS = 5000;
/** Consecutive failures before the user is warned (once, until a save succeeds again). */
const WARN_AFTER_FAILURES = 3;
/** Loading at startup: the backend may still be starting, so a missing answer is retried. */
const LOAD_ATTEMPTS = 3;
const LOAD_TIMEOUT_MS = 4000;
/** `GET /profiles` can wait on a Keychain prompt: the restore does not wait for it longer than this. */
const PROFILES_TIMEOUT_MS = 5000;

/** Saving has failed repeatedly and no save has succeeded since: the status bar says "Not saved". */
const useSaveStatus = create<{ failing: boolean }>(() => ({ failing: false }));
export const useWorkspaceSaveFailing = (): boolean => useSaveStatus((status) => status.failing);

function setSaveFailing(failing: boolean): void {
  if (useSaveStatus.getState().failing !== failing) useSaveStatus.setState({ failing });
}

interface Serialized {
  file: SavedWorkspace;
  json: string;
}

/** A tab's staged edits as the file keeps them (`savedEdits`: null beyond the limits, B40). */
const stagedFile = (tabId: string) => savedEdits(readStagedEdits(tabId)).file;

/** The whole workspace as `workspace.json` holds it; the editors' text and the staged edits are read now. */
function serialize(): Serialized {
  const { tabs, activeTabId, sidebarTab } = useWorkspace.getState();
  const open = new Set(tabs.map((tab) => tab.id));
  forgetEditorBuffersExcept(open);
  forgetStagedEditsExcept(open);
  const file = toSavedWorkspace({ tabs, activeTabId, sidebarTab }, readEditorBuffer, stagedFile);
  return { file, json: workspaceJson(file) };
}

/**
 * A restored state's JSON, before any editor is mounted (comparable whatever it was restored from). A staged set is
 * normalized through the store's form: the backend's file adds defaults (`text: null`) that a local copy lacks (L3).
 */
const snapshotJson = ({ snapshot, staged }: RestoredWorkspace) =>
  workspaceJson(
    toSavedWorkspace(
      snapshot,
      () => null,
      (tabId) => {
        const file = staged.get(tabId);
        return file ? savedEdits(restoredEdits(file)).file : null;
      },
    ),
  );

/**
 * The one workspace writer (spec §4.8, rulings R-02c-4 and R-02c-7). Every store mutation and editor change
 * schedules a save, which runs at most 500 ms later (later changes do not push it back) and sends the whole workspace
 * with `PUT /workspace`: one request at a time, and only when it differs from what was last saved. A failed save is
 * retried with backoff and mirrored to local storage until a save succeeds. When the page goes away, unsaved state is
 * mirrored and sent in a keepalive request.
 */
class WorkspaceWriter {
  private timer: number | null = null;
  private saving = false;
  private changedWhileSaving = false;
  private saveNowRequested = false;
  private failures = 0;
  private warned = false;
  /** The backend as of the last save: the flush on unload cannot wait for it. */
  private backend: BackendInfo | null = null;
  /** What the last flush sent: `pagehide` and `beforeunload` both fire, and it is sent once. */
  private flushed: string | null = null;

  /**
   * `lastSaved`: the JSON the file holds already, or null to save the current state at once. `mirroredAt`: when the
   * local copy still present was written (it goes once a later state is saved), or null if there is none.
   */
  constructor(
    private lastSaved: string | null,
    private mirroredAt: number | null,
  ) {
    void this.refreshBackend();
  }

  start(): void {
    useWorkspace.subscribe((state, previous) => {
      const changed =
        state.tabs !== previous.tabs ||
        state.activeTabId !== previous.activeTabId ||
        state.sidebarTab !== previous.sidebarTab;
      if (changed) this.schedule();
    });
    onEdited(this.schedule);
    onStagedEditsChanged(this.schedule);
    window.addEventListener("pagehide", this.flush);
    window.addEventListener("beforeunload", this.flush);
    this.schedule(); // saves only if the state differs from `lastSaved`
  }

  readonly schedule = (): void => {
    if (this.saving) this.changedWhileSaving = true;
    else if (this.timer === null) this.timer = window.setTimeout(this.save, SAVE_DELAY_MS);
  };

  /** Saves at once, skipping any delay or pending retry (the backend is back, maybe on a new port and token). */
  readonly saveNow = async (): Promise<void> => {
    if (this.saving) {
      this.saveNowRequested = true;
      return;
    }
    this.clearTimer();
    await this.refreshBackend();
    await this.save();
  };

  private clearTimer(): void {
    if (this.timer !== null) window.clearTimeout(this.timer);
    this.timer = null;
  }

  private async refreshBackend(): Promise<void> {
    try {
      this.backend = await getBackendInfo();
    } catch {
      // not ready yet: the next save asks again
    }
  }

  private readonly save = async (): Promise<void> => {
    if (this.saving) {
      this.changedWhileSaving = true;
      return;
    }
    this.clearTimer();
    const serializedAt = Date.now();
    const { file, json } = serialize();
    if (json === this.lastSaved) {
      // The file already holds this state (e.g. a change was undone while saving failed): nothing is unsaved.
      if (this.failures > 0 || this.mirroredAt !== null) this.succeeded(serializedAt, false);
      return;
    }
    this.saving = true;
    this.changedWhileSaving = false;
    let retryInMs: number | null = null;
    try {
      this.backend = await getBackendInfo();
      const client = await api();
      // The body is serialized once: the same string is compared and sent.
      unwrap(
        await client.PUT("/workspace", {
          body: file,
          bodySerializer: () => json,
          signal: AbortSignal.timeout(SAVE_TIMEOUT_MS),
        }),
      );
      this.lastSaved = json;
      this.succeeded(serializedAt, true);
    } catch (error) {
      retryInMs = this.failed(json, error);
    } finally {
      this.saving = false;
    }
    if (this.saveNowRequested) {
      this.saveNowRequested = false;
      void this.saveNow();
    } else if (retryInMs !== null) this.timer = window.setTimeout(this.save, retryInMs);
    else if (this.changedWhileSaving) this.schedule();
  };

  /** The state serialized at `serializedAt` is saved: a local copy written before it is obsolete. */
  private succeeded(serializedAt: number, announce: boolean): void {
    if (this.mirroredAt !== null && this.mirroredAt <= serializedAt) {
      clearPendingWorkspace();
      this.mirroredAt = null;
    }
    if (this.warned && announce) toast("success", "Your open tabs are being saved again");
    this.failures = 0;
    this.warned = false;
    setSaveFailing(false);
  }

  /** Mirrors the unsaved state, warns once the third failure in a row happened, and returns the delay to the retry. */
  private failed(json: string, error: unknown): number {
    this.failures += 1;
    this.mirror(json);
    console.warn(`Saving the workspace failed (${this.failures} in a row)`, error);
    if (this.failures >= WARN_AFTER_FAILURES && !this.warned) {
      this.warned = true;
      setSaveFailing(true);
      const reason = errorMessage(error);
      toast("error", `Your open tabs could not be saved: ${reason}. They are kept here and retried in the background.`);
    }
    return Math.min(RETRY_FIRST_MS * 2 ** (this.failures - 1), RETRY_MAX_MS);
  }

  /**
   * Staged grid edits ride along (up to 8 MiB of text), and the browser's storage quota is far smaller (about 5 MB), so
   * with a large staged set the copy may not fit and is not written at all. It is not written without them instead:
   * a copy is newer than `workspace.json`, so on the next start it would win and drop the staged edits the file
   * still holds (pre-flight L19; the known limit says so).
   */
  private mirror(json: string): void {
    const at = Date.now();
    if (writePendingWorkspace(json, at)) this.mirroredAt = at;
  }

  /** The page is going away: mirrors unsaved state and sends it at once, in a request that outlives the page. */
  readonly flush = (): void => {
    const { json } = serialize();
    if (json === this.lastSaved || json === this.flushed) return;
    this.flushed = json;
    this.mirror(json); // synchronous: it survives even if the request below is lost
    const backend = this.backend;
    if (!backend) return;
    // Keepalive bodies are limited to about 64 KB: a larger one may be dropped (the local copy has it).
    try {
      void fetch(`${backend.baseUrl}/workspace`, {
        method: "PUT",
        keepalive: true,
        headers: { Authorization: `Bearer ${backend.token}`, "Content-Type": "application/json" },
        body: json,
      }).catch(() => undefined);
    } catch {
      // see above
    }
  };
}

let writer: WorkspaceWriter | null = null;

/**
 * Saves the workspace at once, skipping any delay or pending retry: called when the backend is back (restarted, or
 * reachable again), so that what was typed meanwhile is not left waiting, and as a grid commit is sent, so that a
 * reload, crash or quit before its outcome finds it unknown (05b pre-flight H1). `why` is the log line's reason. Does
 * nothing before the workspace is restored.
 */
export function saveWorkspaceNow(why = "The backend is back"): void {
  if (!writer) return;
  console.info(`${why}: saving the workspace now`);
  void writer.saveNow();
}

/** `GET /workspace`; a missing answer is retried (the backend may still be starting), a refusal is not. */
async function loadSavedWorkspace(): Promise<SavedWorkspace> {
  for (let attempt = 1; ; attempt += 1) {
    try {
      const client = await api();
      return unwrap(await client.GET("/workspace", { signal: AbortSignal.timeout(LOAD_TIMEOUT_MS) }));
    } catch (error) {
      const refused = error instanceof ApiError && error.status < 500;
      if (refused || attempt >= LOAD_ATTEMPTS) throw error;
      console.warn(`Loading the workspace failed (attempt ${attempt}), retrying`, error);
      await new Promise((resolve) => window.setTimeout(resolve, 500 * attempt));
    }
  }
}

/** The ids of the existing profiles, or null when unknown (failed or too slow): then every tab keeps its connection. */
async function knownProfileIds(queryClient: QueryClient): Promise<Set<string> | null> {
  let timer = 0;
  const timedOut = new Promise<"timeout">((resolve) => {
    timer = window.setTimeout(() => resolve("timeout"), PROFILES_TIMEOUT_MS);
  });
  try {
    const profiles = await Promise.race([
      queryClient.fetchQuery({ queryKey: profilesKey, queryFn: fetchProfiles }),
      timedOut,
    ]);
    if (profiles === "timeout") {
      console.warn(`No profiles after ${PROFILES_TIMEOUT_MS} ms; every restored tab keeps its connection`);
      return null;
    }
    return new Set(profiles.map((entry) => entry.profile.id));
  } catch (error) {
    console.warn("Could not load the profiles; every restored tab keeps its connection", error);
    return null;
  } finally {
    window.clearTimeout(timer);
  }
}

function reportLostConnections({ closed, detached }: RestoredWorkspace): void {
  const lines = ["Some tabs' connections no longer exist."];
  if (closed.length) lines.push(`Closed: ${closed.join(", ")}`);
  if (detached.length) lines.push(`Reopened without a connection: ${detached.join(", ")}`);
  if (lines.length > 1) toast("warning", lines.join("\n"));
}

function describeTime(at: number): string {
  const when = new Date(at);
  return when.toDateString() === new Date().toDateString() ? when.toLocaleTimeString() : when.toLocaleString();
}

/** What the writer starts from (see `WorkspaceWriter`'s constructor). */
interface WriterStart {
  lastSaved: string | null;
  mirroredAt: number | null;
}

/**
 * Puts the saved tabs into the store. A local copy of unsaved changes wins over the backend's file (and stands in for
 * it when it cannot be read), unless the backend holds the same state already (the flush on unload got through).
 */
async function restoreSaved(queryClient: QueryClient): Promise<WriterStart> {
  const pending = readPendingWorkspace();
  const [saved, profileIds] = await Promise.all([
    loadSavedWorkspace().catch((error: unknown) => {
      if (!pending) throw error;
      console.warn("Could not load the workspace; restoring the unsaved local copy", error);
      return null;
    }),
    knownProfileIds(queryClient),
  ]);
  const profileExists = (id: string) => !profileIds || profileIds.has(id);
  const fromFile = saved && fromSavedWorkspace(saved, profileExists);
  let fromPending = pending && fromSavedWorkspace(pending.file, profileExists);
  if (fromPending && fromFile && snapshotJson(fromPending) === snapshotJson(fromFile)) {
    clearPendingWorkspace(); // the backend has it after all
    fromPending = null;
  }
  const restored = fromPending ?? fromFile;
  if (!restored) throw new Error("Nothing to restore"); // unreachable: no file means there is a local copy
  // Before the tabs: a table tab finds its staged edits when it mounts (spec ⑤ §3.7).
  for (const [tabId, file] of restored.staged) putStagedEdits(tabId, restoredEdits(file));
  useWorkspace.getState().restore(restored.snapshot);
  const source = fromPending ? "the unsaved local copy" : "workspace.json";
  const { tabs } = restored.snapshot;
  console.info(
    `Restored ${tabs.length} tabs from ${source} ` +
      `(${restored.closed.length} closed, ${restored.detached.length} without a connection)`,
  );
  reportLostConnections(restored);
  if (fromPending && pending) {
    toast("info", `Restored unsaved changes from ${describeTime(pending.at)}`);
    return { lastSaved: null, mirroredAt: pending.at }; // pushed to the backend at once
  }
  const changed = restored.closed.length > 0 || restored.detached.length > 0;
  return { lastSaved: changed ? null : serialize().json, mirroredAt: null };
}

async function restore(queryClient: QueryClient): Promise<void> {
  let start: WriterStart;
  try {
    start = await restoreSaved(queryClient);
  } catch (error) {
    // Start empty, and leave the file alone until the user changes something: the empty state is the baseline.
    console.error("Could not restore the workspace", error);
    const retry = isTauriRuntime() ? " Restart DbaGuy to try again." : "";
    const kept = "They stay saved until you open or change a tab.";
    toast("error", `Could not restore your open tabs: ${errorMessage(error)}. ${kept}${retry}`);
    start = { lastSaved: serialize().json, mirroredAt: null };
  }
  writer = new WorkspaceWriter(start.lastSaved, start.mirroredAt);
  writer.start();
}

let restoring: Promise<void> | null = null;

/**
 * Restores the saved workspace (spec §4.8) and then starts the writer; once per page, as StrictMode runs effects
 * twice. Never rejects. Sessions are not opened here: each tab opens its own the first time it is shown.
 */
export function restoreWorkspace(queryClient: QueryClient): Promise<void> {
  restoring ??= restore(queryClient);
  return restoring;
}

// Dev only: this module holds the app's one writer. A hot update would run a second one beside it, so the page reloads
// instead (the replacement module stays unused); the writer's flush on unload keeps the latest state.
if (import.meta.hot) {
  import.meta.hot.accept();
  import.meta.hot.dispose(() => window.location.reload());
}
