import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, type ReactNode } from "react";
import { Group, Panel, useDefaultLayout } from "react-resizable-panels";
import { PanelHandle } from "@/components/ui/PanelHandle";
import { Toaster } from "@/components/ui/Toast";
import { saveWorkspaceNow, useWorkspaceSaveFailing } from "@/features/workspace/workspaceSync";
import { api, unwrap } from "@/lib/api/client";
import { BackendBanner } from "./BackendBanner";

const LAYOUT_ID = "dbaguy-main";

export function AppShell({ sidebar, workspace }: { sidebar: ReactNode; workspace: ReactNode }) {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: async () => unwrap(await (await api()).GET("/system/health")),
    refetchInterval: 15_000,
  });
  // The backend answers again after it did not: what was typed meanwhile is saved now, not at the next retry.
  const unreachable = health.isError;
  const wasUnreachable = useRef(false);
  useEffect(() => {
    if (wasUnreachable.current && !unreachable) saveWorkspaceNow();
    wasUnreachable.current = unreachable;
  }, [unreachable]);
  const saveFailing = useWorkspaceSaveFailing();
  // Persists the sidebar width in localStorage (react-resizable-panels v4 has no autoSaveId).
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({ id: LAYOUT_ID });

  return (
    <div className="flex h-full flex-col">
      <div
        data-tauri-drag-region
        className="flex h-[38px] shrink-0 items-center border-b border-border bg-panel pl-[80px] pr-3"
      >
        <span data-tauri-drag-region className="text-[12px] font-semibold text-muted">
          DbaGuy
        </span>
      </div>
      <BackendBanner />
      <div className="min-h-0 flex-1">
        <Group
          id={LAYOUT_ID}
          orientation="horizontal"
          defaultLayout={defaultLayout}
          onLayoutChanged={onLayoutChanged}
        >
          <Panel id="sidebar" defaultSize="22%" minSize="15%" className="bg-panel">
            {sidebar}
          </Panel>
          <PanelHandle orientation="horizontal" />
          <Panel id="workspace" minSize="40%" className="bg-bg">
            {workspace}
          </Panel>
        </Group>
      </div>
      <div className="flex h-[22px] shrink-0 items-center gap-3 border-t border-border bg-panel px-3 text-[11px] text-muted">
        <span className={health.isError ? "text-danger" : ""}>
          {health.isError ? "Backend unreachable" : health.data ? `Backend v${health.data.version}` : "Connecting…"}
        </span>
        {saveFailing && (
          <span
            className="text-danger"
            title="Your open tabs could not be saved. They are kept in this window and retried."
          >
            Not saved
          </span>
        )}
      </div>
      <Toaster />
    </div>
  );
}
