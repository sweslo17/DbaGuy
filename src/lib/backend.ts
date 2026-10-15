import { invoke, isTauri } from "@tauri-apps/api/core";

export interface BackendInfo {
  baseUrl: string;
  token: string;
}

let cached: Promise<BackendInfo> | null = null;

export function isTauriRuntime(): boolean {
  return isTauri();
}

/** Resolves the sidecar URL and token: from the Tauri shell, or from Vite env in browser mode. */
export function getBackendInfo(): Promise<BackendInfo> {
  if (!cached) {
    cached = isTauri()
      ? invoke<{ base_url: string; token: string }>("backend_info").then((r) => ({
          baseUrl: r.base_url,
          token: r.token,
        }))
      : Promise.resolve({
          baseUrl: import.meta.env.VITE_BACKEND_URL ?? "http://127.0.0.1:8765",
          token: import.meta.env.VITE_BACKEND_TOKEN ?? "dev-token",
        });
    cached.catch(() => {
      cached = null;
    });
  }
  return cached;
}

/** Forget the cached info (after the backend restarts with a new port/token). */
export function resetBackendInfo(): void {
  cached = null;
}

export async function restartBackend(): Promise<BackendInfo> {
  resetBackendInfo();
  const r = await invoke<{ base_url: string; token: string }>("restart_backend");
  const info = { baseUrl: r.base_url, token: r.token };
  cached = Promise.resolve(info);
  return info;
}
