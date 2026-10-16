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

export async function notify(title: string, body: string): Promise<void> {
  try {
    if (isTauriRuntime()) {
      const n = await import("@tauri-apps/plugin-notification");
      let granted = await n.isPermissionGranted();
      if (!granted) granted = (await n.requestPermission()) === "granted";
      if (granted) n.sendNotification({ title, body });
    } else if (typeof Notification !== "undefined" && Notification.permission === "granted") {
      new Notification(title, { body });
    }
  } catch (error) {
    console.warn("Notification failed", error);
  }
}

export function appIsFocused(): boolean {
  return document.hasFocus();
}
