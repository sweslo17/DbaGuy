import { X } from "lucide-react";
import { useEffect } from "react";
import { cn } from "@/lib/cn";
import { useWorkspace, type Tab } from "@/stores/workspace";
import { QueryConsole } from "../console/QueryConsole";
import { ObjectDetailView } from "../explorer/ObjectDetailView";
import { relationOf } from "../table-data/filterValue";
import { TableDataTab } from "../table-data/TableDataTab";

/** A tab's content: a relation opens as table data, any other object as its detail, a console as a query console. */
function TabContent({ tab, active }: { tab: Tab; active: boolean }) {
  if (tab.kind === "console") return <QueryConsole tab={tab} />;
  const relation = relationOf(tab.path);
  return relation ? <TableDataTab tab={tab} relation={relation} active={active} /> : <ObjectDetailView tab={tab} />;
}

export function Workspace() {
  const tabs = useWorkspace((s) => s.tabs);
  const activeTabId = useWorkspace((s) => s.activeTabId);
  const activate = useWorkspace((s) => s.activate);
  const closeTab = useWorkspace((s) => s.closeTab);
  const active = tabs.find((t) => t.id === activeTabId) ?? null;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey && e.key === "w" && activeTabId) {
        e.preventDefault();
        closeTab(activeTabId);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [activeTabId, closeTab]);

  return (
    <div className="flex h-full flex-col">
      <div role="tablist" className="flex h-8 shrink-0 items-end gap-px overflow-x-auto border-b border-border bg-panel px-1">
        {tabs.map((tab) => (
          <div
            key={tab.id}
            role="tab"
            aria-selected={tab.id === activeTabId}
            onClick={() => activate(tab.id)}
            onAuxClick={(e) => {
              if (e.button === 1) closeTab(tab.id);
            }}
            className={cn(
              "group flex h-7 max-w-[220px] shrink-0 items-center gap-1.5 rounded-t-md px-2.5 text-[12px]",
              tab.id === activeTabId ? "bg-bg" : "text-muted hover:bg-hover",
            )}
          >
            <span className="truncate">{tab.title}</span>
            <span className="truncate text-[11px] text-muted">{tab.subtitle}</span>
            <button
              title="Close"
              className="rounded opacity-0 hover:bg-hover group-hover:opacity-100"
              onClick={(e) => {
                e.stopPropagation();
                closeTab(tab.id);
              }}
            >
              <X size={12} />
            </button>
          </div>
        ))}
      </div>
      <div className="min-h-0 flex-1">
        {!active && (
          <p className="p-6 text-center text-muted">Open a table from the sidebar, or right-click a connection → New Query</p>
        )}
        {/* Tabs stay mounted while hidden so their sessions keep their state; closing a tab unmounts it, which closes them. */}
        {tabs.map((tab) => (
          <div key={tab.id} className={cn("h-full", tab.id !== activeTabId && "hidden")}>
            <TabContent tab={tab} active={tab.id === activeTabId} />
          </div>
        ))}
      </div>
    </div>
  );
}
