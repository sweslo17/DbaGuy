import { useQueryClient } from "@tanstack/react-query";
import { listen } from "@tauri-apps/api/event";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { saveWorkspaceNow } from "@/features/workspace/workspaceSync";
import { errorMessage } from "@/lib/api/client";
import { isTauriRuntime, restartBackend } from "@/lib/backend";

/** Unexpected exits restarted without asking; one more within the window leaves it to the user (a crash loop). */
const AUTO_RESTART_LIMIT = 3;
const AUTO_RESTART_WINDOW_MS = 5 * 60_000;

type BannerState = "hidden" | "restarting" | "restarted" | "stopped";

/**
 * When the sidecar exits unexpectedly (Tauri only), it is restarted at once, up to `AUTO_RESTART_LIMIT` times in
 * `AUTO_RESTART_WINDOW_MS`; after that the banner offers a manual restart. Editor contents live in the frontend and
 * are saved again on the new backend; open results, transactions and sessions are gone (tabs offer Reconnect).
 */
export function BackendBanner() {
  const qc = useQueryClient();
  const [state, setState] = useState<BannerState>("hidden");
  const [error, setError] = useState<string | null>(null);
  const exits = useRef<number[]>([]);

  const restart = useRef(async (next: BannerState) => {
    setState("restarting");
    setError(null);
    try {
      await restartBackend();
      saveWorkspaceNow(); // new port and token: what was typed while it was down is saved at once
      setState(next);
      await qc.invalidateQueries();
    } catch (e) {
      setError(errorMessage(e));
      setState("stopped");
    }
  });

  useEffect(() => {
    if (!isTauriRuntime()) return;
    const unlisten = listen("backend://exited", () => {
      const now = Date.now();
      exits.current = [...exits.current.filter((t) => now - t < AUTO_RESTART_WINDOW_MS), now];
      if (exits.current.length <= AUTO_RESTART_LIMIT) void restart.current("restarted");
      else setState("stopped");
    });
    return () => {
      void unlisten.then((fn) => fn());
    };
  }, []);

  if (state === "hidden") return null;

  const text =
    state === "restarting"
      ? "The DbaGuy backend stopped unexpectedly — restarting it…"
      : state === "restarted"
        ? "The DbaGuy backend stopped unexpectedly and was restarted. Open results and transactions were lost; reconnect the tabs you need. Your editor contents are kept."
        : "The DbaGuy backend stopped unexpectedly. Your editor contents are kept.";

  return (
    <div className="flex items-center gap-3 border-b border-border bg-danger/10 px-3 py-1.5 text-[12px]">
      <span className="text-danger">{text}</span>
      {state === "stopped" && (
        <Button size="sm" onClick={() => void restart.current("hidden")}>
          Restart backend
        </Button>
      )}
      {state === "restarted" && (
        <Button size="sm" variant="ghost" onClick={() => setState("hidden")}>
          Dismiss
        </Button>
      )}
      {error && (
        <span className="text-danger" data-selectable>
          {error}
        </span>
      )}
    </div>
  );
}
