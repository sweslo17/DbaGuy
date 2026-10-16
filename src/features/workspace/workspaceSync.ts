import type { QueryClient } from "@tanstack/react-query";
import { toast } from "@/components/ui/Toast";
import { api, ApiError, errorMessage, unwrap } from "@/lib/api/client";
import type { SavedWorkspace } from "@/lib/api/types";
import { getBackendInfo, type BackendInfo } from "@/lib/backend";
import { forgetEditorBuffersExcept, onEdited, readEditorBuffer } from "@/stores/editorBuffers";
import { useWorkspace } from "@/stores/workspace";
import { fetchProfiles, profilesKey } from "../connections/hooks";
import { fromSavedWorkspace, toSavedWorkspace, type RestoredWorkspace } from "./workspaceFile";

/** A change is saved at most this long after it was made, so a crash loses at most the last half second of typing. */
const SAVE_DELAY_MS = 500;
const SAVE_TIMEOUT_MS = 10_000;
/** A failed save is retried after 1 s, 2 s, 4 s, … at most 30 s. */
const RETRY_FIRST_MS = 1000;
const RETRY_MAX_MS = 30_000;
/** Consecutive failures before the user is warned (once, until a save succeeds again). */
const WARN_AFTER_FAILURES = 3;
/** Loading at startup: the backend may still be starting, so a missing answer is retried. */
const LOAD_ATTEMPTS = 3;
const LOAD_TIMEOUT_MS = 4000;

interface Serialized {
  file: SavedWorkspace;
  json: string;
}

/** The whole workspace as `workspace.json` holds it; the editors' text is read now. */
function serialize(): Serialized {
  const { tabs, activeTabId, sidebarTab } = useWorkspace.getState();
  forgetEditorBuffersExcept(new Set(tabs.map((tab) => tab.id)));
  const file = toSavedWorkspace({ tabs, activeTabId, sidebarTab }, readEditorBuffer);
  return { file, json: JSON.stringify(file) };
}

/**
 * The one workspace writer (spec §4.8, ruling R-02c-4). Every store mutation and editor change schedules a save, which
 * runs at most 500 ms later (later changes do not push it back) and sends the whole workspace with `PUT /workspace`:
 * one request at a time, and only when it differs from what was last saved. A failed save is retried with backoff.
 * When the page goes away, the latest state goes out in a keepalive request.
 */
class WorkspaceWriter {
  private timer: number | null = null;
  private saving = false;
  private changedWhileSaving = false;
  private failures = 0;
  private warned = false;
  /** The backend as of the last save: the flush on unload cannot wait for it. */
  private backend: BackendInfo | null = null;
  /** What the last flush sent: `pagehide` and `beforeunload` both fire, and it is sent once. */
  private flushed: string | null = null;

  /** `lastSaved`: the JSON the file holds already, or null to save the current state at once. */
  constructor(private lastSaved: string | null) {
    void getBackendInfo().then(
      (info) => (this.backend ??= info),
      () => undefined, // the first save fetches it again
    );
  }

  readonly schedule = (): void => {
    if (this.saving) this.changedWhileSaving = true;
    else if (this.timer === null) this.timer = window.setTimeout(this.save, SAVE_DELAY_MS);
  };

  private readonly save = async (): Promise<void> => {
    this.timer = null;
    const { file, json } = serialize();
    if (json === this.lastSaved) return;
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
      this.succeeded();
    } catch (error) {
      retryInMs = this.failed(error);
    } finally {
      this.saving = false;
    }
    if (retryInMs !== null) this.timer = window.setTimeout(this.save, retryInMs);
    else if (this.changedWhileSaving) this.schedule();
  };

  private succeeded(): void {
    if (this.warned) toast("success", "Your tabs are being saved again");
    this.failures = 0;
    this.warned = false;
  }

  /** Counts the failure, warns once the third one in a row happened, and returns the delay before the retry. */
  private failed(error: unknown): number {
    this.failures += 1;
    console.warn(`Saving the workspace failed (${this.failures} in a row)`, error);
    if (this.failures >= WARN_AFTER_FAILURES && !this.warned) {
      this.warned = true;
      toast("warning", `Your open tabs could not be saved: ${errorMessage(error)}. Retrying in the background.`);
    }
    return Math.min(RETRY_FIRST_MS * 2 ** (this.failures - 1), RETRY_MAX_MS);
  }

  /** The page is going away: sends the latest state at once, in a request that outlives the page. */
  readonly flush = (): void => {
    const backend = this.backend;
    if (!backend) return;
    const { json } = serialize();
    if (json === this.lastSaved || json === this.flushed) return;
    this.flushed = json;
    // Keepalive bodies are limited to about 64 KB: a larger one may be dropped, and the last regular save stands
    // (normally at most 500 ms old).
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

function startWriter(lastSaved: string | null): void {
  const writer = new WorkspaceWriter(lastSaved);
  useWorkspace.subscribe((state, previous) => {
    const changed =
      state.tabs !== previous.tabs ||
      state.activeTabId !== previous.activeTabId ||
      state.sidebarTab !== previous.sidebarTab;
    if (changed) writer.schedule();
  });
  onEdited(writer.schedule);
  window.addEventListener("pagehide", writer.flush);
  window.addEventListener("beforeunload", writer.flush);
  writer.schedule(); // saves only if the state differs from `lastSaved`
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

function reportLostConnections({ closed, detached }: RestoredWorkspace): void {
  const lines = ["Some tabs' connections no longer exist."];
  if (closed.length) lines.push(`Closed: ${closed.join(", ")}`);
  if (detached.length) lines.push(`Reopened without a connection: ${detached.join(", ")}`);
  if (lines.length > 1) toast("warning", lines.join("\n"));
}

/**
 * Puts the saved tabs into the store. Returns the restored state's JSON as the writer's baseline, or null when the
 * restore changed it (tabs dropped or detached), so that the writer saves it at once.
 */
async function restoreSaved(queryClient: QueryClient): Promise<string | null> {
  const [saved, profiles] = await Promise.all([
    loadSavedWorkspace(),
    queryClient.fetchQuery({ queryKey: profilesKey, queryFn: fetchProfiles }).catch((error: unknown) => {
      console.warn("Could not load the profiles; every restored tab keeps its connection", error);
      return null;
    }),
  ]);
  const profileIds = profiles && new Set(profiles.map((entry) => entry.profile.id));
  const restored = fromSavedWorkspace(saved, (id) => !profileIds || profileIds.has(id));
  useWorkspace.getState().restore(restored.snapshot);
  console.info(
    `Restored ${restored.snapshot.tabs.length} tabs (${restored.closed.length} closed, ${restored.detached.length} without a connection)`,
  );
  reportLostConnections(restored);
  return restored.closed.length || restored.detached.length ? null : serialize().json;
}

async function restore(queryClient: QueryClient): Promise<void> {
  let lastSaved: string | null;
  try {
    lastSaved = await restoreSaved(queryClient);
  } catch (error) {
    // Start empty, and leave the file alone until the user changes something: the empty state is the baseline.
    console.error("Could not restore the workspace", error);
    toast(
      "error",
      `Could not restore your open tabs: ${errorMessage(error)}. They stay saved until you open or change a tab.`,
    );
    lastSaved = serialize().json;
  }
  startWriter(lastSaved);
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
