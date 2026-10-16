import { useQueryClient } from "@tanstack/react-query";
import { listen } from "@tauri-apps/api/event";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { saveWorkspaceNow } from "@/features/workspace/workspaceSync";
import { errorMessage } from "@/lib/api/client";
import { isTauriRuntime, restartBackend } from "@/lib/backend";

/** Shown when the sidecar exits unexpectedly (Tauri only). */
export function BackendBanner() {
  const qc = useQueryClient();
  const [exited, setExited] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isTauriRuntime()) return;
    const unlisten = listen("backend://exited", () => setExited(true));
    return () => {
      void unlisten.then((fn) => fn());
    };
  }, []);

  if (!exited) return null;

  const restart = async () => {
    setRestarting(true);
    setError(null);
    try {
      await restartBackend();
      saveWorkspaceNow(); // new port and token: what was typed while it was down is saved at once
      setExited(false);
      await qc.invalidateQueries();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setRestarting(false);
    }
  };

  return (
    <div className="flex items-center gap-3 border-b border-border bg-danger/10 px-3 py-1.5 text-[12px]">
      <span className="text-danger">The DbaGuy backend stopped unexpectedly. Your editor contents are kept.</span>
      <Button size="sm" onClick={restart} disabled={restarting}>
        {restarting ? "Restarting…" : "Restart backend"}
      </Button>
      {error && (
        <span className="text-danger" data-selectable>
          {error}
        </span>
      )}
    </div>
  );
}
