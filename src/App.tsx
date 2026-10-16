import { useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { AppShell } from "./app/AppShell";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Sidebar } from "./app/Sidebar";
import { Workspace } from "./features/workspace/Workspace";
import { restoreWorkspace } from "./features/workspace/workspaceSync";

/** Until the saved tabs are back: nothing renders that could open a session or write the workspace. */
function Restoring() {
  return (
    <div data-tauri-drag-region className="flex h-full items-center justify-center gap-2 bg-bg text-[12px] text-muted">
      <Loader2 size={14} className="animate-spin" aria-hidden />
      Restoring workspace…
    </div>
  );
}

export function App() {
  const queryClient = useQueryClient();
  const [restored, setRestored] = useState(false);
  useEffect(() => {
    void restoreWorkspace(queryClient).then(() => setRestored(true));
  }, [queryClient]);

  if (!restored) return <Restoring />;
  // An error anywhere in the app shows its message and a Reload (a tab's own error stays inside that tab).
  return (
    <ErrorBoundary name="App" title="DbaGuy hit an error" recover="reload">
      <AppShell sidebar={<Sidebar />} workspace={<Workspace />} />
    </ErrorBoundary>
  );
}
