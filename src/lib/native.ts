import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { isTauriRuntime } from "./backend";

export interface FileFilter {
  name: string;
  extensions: string[];
}

export function canChooseFiles(): boolean {
  return isTauriRuntime();
}

export async function chooseSavePath(defaultName: string, filters: FileFilter[]): Promise<string | null> {
  if (!isTauriRuntime()) return null;
  const { save } = await import("@tauri-apps/plugin-dialog");
  return (await save({ defaultPath: defaultName, filters })) ?? null;
}

/** A file picked in the native open panel (app only: null in browser mode or when cancelled); no filter: any file. */
export async function chooseOpenPath(filters: FileFilter[] = []): Promise<string | null> {
  if (!isTauriRuntime()) return null;
  const { open } = await import("@tauri-apps/plugin-dialog");
  return (await open({ directory: false, multiple: false, filters: filters.length ? filters : undefined })) ?? null;
}

/** The events the shell sends for the app menu's Quit (⌘Q) and a window's close (src-tauri/src/quit.rs). */
const QUIT_REQUESTED = "app://quit-requested";
const CLOSE_REQUESTED = "app://close-requested";

/**
 * Calls `handler` when the user chooses Quit (⌘Q, the app menu): the app quits only through `quitApp`. Each request is
 * acknowledged at once, or the shell quits by itself after 2 s (a page that does not answer never keeps the app open).
 * App only: a no-op in browser mode. Resolves to the function that stops listening.
 */
export async function onQuitRequested(handler: () => void): Promise<() => void> {
  if (!isTauriRuntime()) return () => undefined;
  return listen<number>(QUIT_REQUESTED, (event) => {
    void invoke("quit_ack", { id: event.payload });
    handler();
  });
}

/** Quits the app (the shell stops the backend). App only. */
export async function quitApp(): Promise<void> {
  if (!isTauriRuntime()) return;
  await invoke("quit_app");
}

/**
 * Asks `allow` before the window closes (the red button, ⌘⇧W, the menu's Close Window); false keeps it open. The shell
 * holds every close and sends it here; each request is acknowledged at once, or the shell closes the window by itself
 * after 2 s (a page that does not answer never keeps the window open). `allow` must settle: the window waits for it
 * (it closes once it resolves true). App only: a no-op in browser mode. Resolves to the function that stops listening.
 */
export async function onWindowCloseRequested(allow: () => Promise<boolean>): Promise<() => void> {
  if (!isTauriRuntime()) return () => undefined;
  return listen<number>(CLOSE_REQUESTED, async (event) => {
    void invoke("quit_ack", { id: event.payload });
    if (await allow()) await invoke("close_window");
  });
}

/** A folder picked in the native dialog (app only: null in browser mode or when cancelled). */
export async function chooseFolder(defaultPath?: string): Promise<string | null> {
  if (!isTauriRuntime()) return null;
  const { open } = await import("@tauri-apps/plugin-dialog");
  return (await open({ directory: true, multiple: false, defaultPath })) ?? null;
}

/**
 * A system notification. Permission is asked the first time one is sent (in the app by macOS, in browser mode by the
 * browser); once refused, nothing is shown.
 */
export async function notify(title: string, body: string): Promise<void> {
  try {
    if (isTauriRuntime()) {
      const n = await import("@tauri-apps/plugin-notification");
      let granted = await n.isPermissionGranted();
      if (!granted) granted = (await n.requestPermission()) === "granted";
      if (granted) n.sendNotification({ title, body });
    } else if (typeof Notification !== "undefined") {
      if (Notification.permission === "default") await Notification.requestPermission();
      if (Notification.permission === "granted") new Notification(title, { body });
    }
  } catch (error) {
    console.warn("Notification failed", error);
  }
}

export function appIsFocused(): boolean {
  return document.hasFocus();
}

/**
 * Puts text, or text still being produced (a formatter round trip), on the clipboard; every copy goes through here.
 * In the app it is written natively by the clipboard plugin, which has no user-gesture rule. In browser mode the
 * clipboard call starts synchronously, inside the user's gesture: WebKit only writes during one, and a round trip
 * outlives it, so the text goes in as a promise inside a ClipboardItem; engines that take no promise there get
 * `writeText` once the text is known (Chromium writes without a gesture).
 */
export async function writeClipboard(text: string | Promise<string>): Promise<void> {
  if (isTauriRuntime()) {
    const { writeText } = await import("@tauri-apps/plugin-clipboard-manager");
    await writeText(await text);
    return;
  }
  const pending = Promise.resolve(text);
  const clipboard = navigator.clipboard as Clipboard | undefined;
  if (!clipboard) throw new Error("The clipboard is not available");
  if (typeof ClipboardItem !== "undefined" && typeof clipboard.write === "function") {
    const blob = pending.then((value) => new Blob([value], { type: "text/plain" }));
    blob.catch(() => undefined); // a failure of the text itself is the caller's to report
    try {
      await clipboard.write([new ClipboardItem({ "text/plain": blob })]);
      return;
    } catch {
      // No promise support, or the text failed (then `await pending` below rethrows that error).
    }
  }
  await clipboard.writeText(await pending);
}
