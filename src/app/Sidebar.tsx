import { useState, type ReactNode } from "react";
import { Tabs } from "@/components/ui/Tabs";
import { cn } from "@/lib/cn";
import { useWorkspace } from "@/stores/workspace";
import { JobsButton } from "../features/data-tools/JobsButton";
import { Explorer } from "../features/explorer/Explorer";
import { HistoryPanel } from "../features/history/HistoryPanel";
import { HISTORY_VIEW } from "../features/history/useHistory";
import { ScriptsPanel } from "../features/scripts/ScriptsPanel";

type SidebarView = "connections" | "scripts" | typeof HISTORY_VIEW;

/** `render` gets whether the view is the one shown (a view stays mounted while hidden). */
const VIEWS: { value: SidebarView; label: string; render: (shown: boolean) => ReactNode }[] = [
  { value: "connections", label: "Connections", render: () => <Explorer /> },
  { value: "scripts", label: "Scripts", render: () => <ScriptsPanel /> },
  { value: HISTORY_VIEW, label: "History", render: (shown) => <HistoryPanel shown={shown} /> },
];

const isView = (value: string | null): value is SidebarView => VIEWS.some((view) => view.value === value);

/**
 * The sidebar: Connections | Scripts | History. The open view is part of the workspace (`sidebar_tab`); a missing or
 * unknown one shows the connections, and only a click changes it, so a workspace that failed to load is never
 * overwritten with a default. A view stays mounted once opened, keeping its tree's state while another is shown. At
 * the bottom, the data-tools Jobs while this window has any (it subscribes itself: a job's progress never renders this).
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
              {view.render(view.value === current)}
            </div>
          ),
      )}
      <JobsButton />
    </div>
  );
}
