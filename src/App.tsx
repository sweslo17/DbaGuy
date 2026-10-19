import { useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { AppShell } from "./app/AppShell";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Sidebar } from "./app/Sidebar";
import { UnlockPromptHost } from "./features/connections/UnlockPromptHost";
import { PaletteHost } from "./features/palette/CommandPalette";
import { togglePalette } from "./features/palette/paletteStore";
import { Workspace } from "./features/workspace/Workspace";
import { restoreWorkspace } from "./features/workspace/workspaceSync";
import { isDialogOpen } from "./lib/dialogs";
import { isImeKeyEvent } from "./lib/ime";
import { isCommandKey } from "./lib/shortcuts";

/** Until the saved tabs are back: nothing renders that could open a session or write the workspace. */
function Restoring() {
  return (
    <div data-tauri-drag-region className="flex h-full items-center justify-center gap-2 bg-bg text-[12px] text-muted">
      <Loader2 size={14} className="animate-spin" aria-hidden />
      Restoring workspace…
    </div>
  );
}

/**
 * ⌘P quick open and ⌘⇧P commands (spec §4.10), anywhere in the window once the workspace is back: they open the
 * palette, switch it, or close it. Never the browser's Print; nothing while another dialog has the keyboard or an input
 * method is composing. (⌘, and ⌘W are handled by the settings dialog and the workspace.)
 */
function usePaletteShortcuts(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return;
    const onKey = (e: KeyboardEvent) => {
      if (isImeKeyEvent(e) || e.defaultPrevented) return;
      const commands = isCommandKey(e, "p", { shift: true });
      if (!commands && !isCommandKey(e, "p")) return;
      e.preventDefault();
      if (isDialogOpen({ exceptPalette: true })) return;
      togglePalette(commands ? "commands" : "quickOpen");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [enabled]);
}

export function App() {
  const queryClient = useQueryClient();
  const [restored, setRestored] = useState(false);
  useEffect(() => {
    void restoreWorkspace(queryClient).then(() => setRestored(true));
  }, [queryClient]);
  usePaletteShortcuts(restored);

  if (!restored) return <Restoring />;
  // An error anywhere in the app shows its message and a Reload (a tab's own error stays inside that tab).
  return (
    <ErrorBoundary name="App" title="DbaGuy hit an error" recover="reload">
      <AppShell sidebar={<Sidebar />} workspace={<Workspace />} />
      <PaletteHost />
      <UnlockPromptHost />
    </ErrorBoundary>
  );
}
