import { useQuery } from "@tanstack/react-query";
import { Settings } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";
import { Group, Panel, useDefaultLayout } from "react-resizable-panels";
import symbol from "@/assets/symbol-64.png";
import { PanelHandle } from "@/components/ui/PanelHandle";
import { Toaster } from "@/components/ui/Toast";
import { DataToolsHost } from "@/features/data-tools/DataToolsHost";
import { syncJobs } from "@/features/data-tools/jobsStore";
import { useJobEndings } from "@/features/data-tools/useJobEndings";
import { OpenDocumentDialogHost } from "@/features/documents/OpenDocumentDialog";
import { openSettings, SettingsDialogHost } from "@/features/settings/SettingsDialog";
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
  // A reloaded window lists the backend's data-tools jobs and attaches to the running ones (R-06-15)
  useEffect(() => {
    void syncJobs();
  }, []);
  useJobEndings();
  // Persists the sidebar width in localStorage (react-resizable-panels v4 has no autoSaveId).
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({ id: LAYOUT_ID });

  return (
    <div className="flex h-full flex-col">
      <div
        data-tauri-drag-region
        className="flex h-[38px] shrink-0 items-center border-b border-border bg-panel pl-[80px] pr-3"
      >
        {/* Decorative, and part of the drag region: it must never swallow a window drag. */}
        <img
          src={symbol}
          alt=""
          draggable={false}
          data-tauri-drag-region
          className="pointer-events-none mr-1.5 h-[18px] w-[18px] shrink-0"
        />
        <span data-tauri-drag-region className="text-[12px] font-semibold text-muted">
          DbaGuy
        </span>
        <button
          type="button"
          title="Settings (⌘,)"
          aria-label="Settings"
          className="ml-auto flex h-6 w-6 items-center justify-center rounded text-muted outline-none hover:bg-hover hover:text-fg focus-visible:ring-2 focus-visible:ring-accent/50"
          onClick={openSettings}
        >
          <Settings size={14} />
        </button>
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
      <SettingsDialogHost />
      <OpenDocumentDialogHost />
      <DataToolsHost />
    </div>
  );
}
