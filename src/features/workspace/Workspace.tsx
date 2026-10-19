import { Loader2, Plus, X } from "lucide-react";
import { memo, useEffect, useLayoutEffect, useMemo, useRef } from "react";
import { EngineIcon } from "@/components/EngineIcon";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import type { Profile } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { isDialogOpen } from "@/lib/dialogs";
import { isImeKeyEvent } from "@/lib/ime";
import { isCommandKey } from "@/lib/shortcuts";
import { revealInStrip, scrollSidewaysOnWheel } from "@/lib/stripScroll";
import { useTabStatus } from "@/stores/tabStatus";
import { useWorkspace, type Tab } from "@/stores/workspace";
import { useProfiles } from "../connections/hooks";
import { profileColor } from "../connections/profileColors";
import { ObjectDetailView } from "../explorer/ObjectDetailView";
import { QueryTab } from "../query/QueryTab";
import { relationOf } from "../table-data/filterValue";
import { TableDataTab } from "../table-data/TableDataTab";
import { isClosePending, requestCloseTab } from "./closeGuards";
import { WelcomeScreen } from "./WelcomeScreen";

/** The attribute that marks a tab's content with its tab id. */
const TAB_PANEL = "data-tab-panel";

/**
 * A tab's content: a query tab's editor, a relation's table data, or any other object's detail. Memoized: changing one
 * tab (a parameter typed, a context picked) replaces `tabs` but keeps every other tab's object, so only that one
 * renders again (R-02c-4).
 */
const TabContent = memo(function TabContent({ tab, active }: { tab: Tab; active: boolean }) {
  switch (tab.kind) {
    case "query":
      return <QueryTab tab={tab} active={active} />;
    case "table": {
      const relation = relationOf(tab.path);
      return relation ? <TableDataTab tab={tab} relation={relation} active={active} /> : <ObjectDetailView tab={tab} />;
    }
    case "object":
      return <ObjectDetailView tab={tab} />;
  }
});

/** The tab's tooltip: its title and connection, and for a query tab bound to a script, the file (and if it is gone). */
function tabTooltip(tab: Tab, profile: Profile | undefined, missing: boolean): string {
  const lines = [profile ? `${tab.title} · ${profile.name}` : tab.title];
  if (tab.kind === "query" && tab.scriptPath !== null) {
    // A tab still waiting for its file has no text of its own: ⌘S has nothing to write back.
    const resave = tab.textFromFile ? "" : "; ⌘S saves it again";
    lines.push(missing ? `${tab.scriptPath} — file missing${resave}` : tab.scriptPath);
  }
  return lines.join("\n");
}

/**
 * One tab in the strip: its connection's engine icon leads it and its colour underlines it; a query tab shows a
 * spinner while it works and a dot when its script has unsaved changes. Every way of closing it goes through the close
 * guards. It scrolls itself into view when it becomes the active tab.
 */
function TabLabel({ tab, active, profile }: { tab: Tab; active: boolean; profile: Profile | undefined }) {
  const activate = useWorkspace((s) => s.activate);
  const status = useTabStatus(tab.id);
  const tint = profileColor(profile?.color);
  const query = tab.kind === "query";
  // However it became active (opened, ⌘1–9, ⌃Tab, the palette, the restored workspace), the strip shows it.
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (active && ref.current) revealInStrip(ref.current);
  }, [active]);
  return (
    <div
      ref={ref}
      role="tab"
      aria-selected={active}
      onClick={() => activate(tab.id)}
      onAuxClick={(e) => {
        if (e.button === 1) void requestCloseTab(tab.id);
      }}
      className={cn(
        "group flex h-7 max-w-[220px] shrink-0 items-center gap-1.5 rounded-t-md px-2.5 text-[12px]",
        active ? "bg-bg" : "text-muted hover:bg-hover",
      )}
      style={tint ? { boxShadow: `inset 0 -2px 0 ${tint}` } : undefined}
      title={tabTooltip(tab, profile, status.missing)}
    >
      {profile && <EngineIcon engine={profile.engine} size={12} />}
      {query && status.busy && <Loader2 size={11} className="shrink-0 animate-spin text-muted" aria-label="Running" />}
      <span className="truncate">
        {query && status.dirty && (
          <span
            className={status.missing ? "text-warning" : undefined}
            aria-label={status.missing ? "File missing" : "Unsaved changes"}
          >
            •{" "}
          </span>
        )}
        {tab.title}
      </span>
      {profile && <span className="truncate text-[11px] text-muted">{profile.name}</span>}
      <button
        title="Close (⌘W)"
        className="shrink-0 rounded opacity-0 group-hover:opacity-100 hover:bg-hover"
        onClick={(e) => {
          e.stopPropagation();
          void requestCloseTab(tab.id);
        }}
      >
        <X size={12} />
      </button>
    </div>
  );
}

export function Workspace() {
  const tabs = useWorkspace((s) => s.tabs);
  const activeTabId = useWorkspace((s) => s.activeTabId);
  const openQuery = useWorkspace((s) => s.openQuery);
  const profiles = useProfiles();
  const profilesById = useMemo(
    () => new Map(profiles.data?.map((entry) => [entry.profile.id, entry.profile])),
    [profiles.data],
  );
  const active = tabs.find((t) => t.id === activeTabId) ?? null;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isImeKeyEvent(e) || !isCommandKey(e, "w")) return;
      e.preventDefault();
      // Never the tab behind a dialog, the palette included (ruling R-02c-19), and never a second close queued behind
      // one being asked about (its dialog may be another tab's).
      if (!activeTabId || isDialogOpen() || isClosePending()) return;
      void requestCloseTab(activeTabId);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [activeTabId]);

  // The tab just hidden may still hold the focus (e.g. a palette command switched tabs): keys must never reach a hidden
  // editor. A query tab focuses its own editor once it is shown.
  useLayoutEffect(() => {
    const focused = document.activeElement;
    if (!(focused instanceof HTMLElement)) return;
    const panel = focused.closest(`[${TAB_PANEL}]`);
    if (panel && panel.getAttribute(TAB_PANEL) !== activeTabId) focused.blur();
  }, [activeTabId]);

  // A new query tab starts on the connection selected in the explorer.
  const newQuery = () => openQuery(useWorkspace.getState().selectedProfileId, null);

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-8 shrink-0 items-end border-b border-border bg-panel">
        {/* No scrollbar of its own, whatever the system setting: one would sit inside the strip and cut off the tabs. The
            strip scrolls by wheel, swipe, and by showing the active tab; the New query button stays outside it, in view. */}
        <div
          role="tablist"
          className="scrollbar-none flex h-full min-w-0 flex-1 items-end gap-px overflow-x-auto overflow-y-hidden px-1"
          onWheel={scrollSidewaysOnWheel}
        >
          {tabs.map((tab) => (
            <TabLabel
              key={tab.id}
              tab={tab}
              active={tab.id === activeTabId}
              profile={tab.profileId === null ? undefined : profilesById.get(tab.profileId)}
            />
          ))}
        </div>
        <button
          title="New query on the connection selected in the sidebar"
          aria-label="New query"
          className="mb-1 mr-1 flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted hover:bg-hover hover:text-fg"
          onClick={newQuery}
        >
          <Plus size={14} />
        </button>
      </div>
      <div className="min-h-0 flex-1">
        {!active && <WelcomeScreen />}
        {/* Tabs stay mounted while hidden so their sessions and editors keep their state; closing a tab unmounts it.
            A tab whose content throws shows the error in its place; the other tabs carry on. */}
        {tabs.map((tab) => (
          <div key={tab.id} data-tab-panel={tab.id} className={cn("h-full", tab.id !== activeTabId && "hidden")}>
            <ErrorBoundary name={`Tab ${tab.title}`} title="This tab hit an error" recover="retry">
              <TabContent tab={tab} active={tab.id === activeTabId} />
            </ErrorBoundary>
          </div>
        ))}
      </div>
    </div>
  );
}
