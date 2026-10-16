import { useState, type ReactNode } from "react";
import { Tabs } from "@/components/ui/Tabs";
import { cn } from "@/lib/cn";
import { useWorkspace } from "@/stores/workspace";
import { Explorer } from "../features/explorer/Explorer";
import { HistoryPanel } from "../features/history/HistoryPanel";
import { ScriptsPanel } from "../features/scripts/ScriptsPanel";

type SidebarView = "connections" | "scripts" | "history";

const VIEWS: { value: SidebarView; label: string; render: () => ReactNode }[] = [
  { value: "connections", label: "Connections", render: () => <Explorer /> },
  { value: "scripts", label: "Scripts", render: () => <ScriptsPanel /> },
  { value: "history", label: "History", render: () => <HistoryPanel /> },
];

const isView = (value: string | null): value is SidebarView => VIEWS.some((view) => view.value === value);

/**
 * The sidebar: Connections | Scripts | History. The open view is part of the workspace (`sidebar_tab`); a missing or
 * unknown one shows the connections, and only a click changes it, so a workspace that failed to load is never
 * overwritten with a default. A view stays mounted once opened, keeping its tree's state while another is shown.
 */
export function Sidebar() {
  const stored = useWorkspace((s) => s.sidebarTab);
  const setSidebarTab = useWorkspace((s) => s.setSidebarTab);
  const current: SidebarView = isView(stored) ? stored : "connections";
  const [visited, setVisited] = useState<ReadonlySet<SidebarView>>(() => new Set([current]));
  if (!visited.has(current)) setVisited(new Set([...visited, current]));

  return (
    <div className="flex h-full flex-col">
      <Tabs value={current} onValueChange={setSidebarTab} items={VIEWS} className="shrink-0 px-1 pt-1" />
      {VIEWS.map(
        (view) =>
          visited.has(view.value) && (
            <div
              key={view.value}
              role="tabpanel"
              aria-label={view.label}
              className={cn("min-h-0 flex-1", view.value !== current && "hidden")}
            >
              {view.render()}
            </div>
          ),
      )}
    </div>
  );
}
