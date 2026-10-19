import { defaultFilter } from "cmdk";
import { FileCode2, FileText } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { EngineIcon } from "@/components/EngineIcon";
import { errorMessage } from "@/lib/api/client";
import type { ScriptNode } from "@/lib/api/types";
import { ENGINE_LABELS } from "@/lib/engines";
import { parentPath } from "@/lib/scriptPaths";
import { useWorkspace, workingProfileId, type Tab } from "@/stores/workspace";
import { useProfiles } from "../connections/hooks";
import { kindIcon } from "../explorer/kinds";
import { useScriptActions, useScriptTree } from "../scripts/useScripts";
import { SHORTCUTS } from "./commands";
import { ObjectsSection } from "./objectSearch";
import { PaletteGroup, PaletteInput, PaletteItem, PaletteList, PaletteNote, PaletteRoot, type Choose } from "./parts";

/** How long typing pauses before the objects are searched. */
const SEARCH_DELAY_MS = 150;
/** Each local section lists at most this many rows. */
const LOCAL_LIMIT = 50;

/**
 * The rows whose text matches `search` by cmdk's own fuzzy score, best first (without a search: all of them, as they
 * are). The palette filters its local sections itself, so the remote one keeps the server's order (ruling R-02c-16).
 */
function matching<T>(
  items: readonly T[],
  search: string,
  text: (item: T) => string,
  keywords: (item: T) => string[],
): T[] {
  if (!search) return items.slice(0, LOCAL_LIMIT);
  return items
    .map((item) => ({ item, score: defaultFilter(text(item), search, keywords(item)) }))
    .filter((entry) => entry.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, LOCAL_LIMIT)
    .map((entry) => entry.item);
}

/** Every file in the scripts tree, depth first. */
function scriptFiles(items: readonly ScriptNode[]): ScriptNode[] {
  return items.flatMap((node) => (node.kind === "file" ? [node] : scriptFiles(node.children ?? [])));
}

/** The icon of a tab: a query, or the object a table or object tab shows. */
function tabIcon(tab: Tab) {
  return tab.kind === "query" ? FileCode2 : kindIcon(tab.path[tab.path.length - 1]?.kind ?? tab.kind);
}

/**
 * ⌘P (spec §4.10): one list in sections. Open tabs (activates one), connections (opens a new query tab on one) and
 * script files (opens one, or the tab that has it) are filtered here as you type; database objects of the connection
 * being worked with are searched by the backend and open as the explorer would open them.
 */
export function QuickOpen({ choose }: { choose: Choose }) {
  const tabs = useWorkspace((s) => s.tabs);
  const activeTabId = useWorkspace((s) => s.activeTabId);
  const workingId = useWorkspace(workingProfileId);
  const { activate, openQuery } = useWorkspace.getState();
  const profiles = useProfiles().data;
  const tree = useScriptTree({ cached: true });
  const scripts = useScriptActions();

  const [text, setText] = useState("");
  const search = text.trim();
  const [q, setQ] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => setQ(search), SEARCH_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [search]);

  const profilesById = useMemo(() => new Map(profiles?.map((entry) => [entry.profile.id, entry.profile])), [profiles]);
  const profileName = (id: string | null) => (id === null ? "" : (profilesById.get(id)?.name ?? ""));
  const files = useMemo(() => scriptFiles(tree.data?.items ?? []), [tree.data]);

  const tabRows = matching(tabs, search, (tab) => tab.title, (tab) => [profileName(tab.profileId)]);
  const connectionRows = matching(
    profiles?.map((entry) => entry.profile) ?? [],
    search,
    (profile) => profile.name,
    (profile) => [ENGINE_LABELS[profile.engine]],
  );
  const scriptRows = matching(files, search, (file) => file.name, (file) => [file.path]);
  const working = workingId === null ? null : (profilesById.get(workingId) ?? null);

  return (
    <PaletteRoot label="Quick open" shouldFilter={false}>
      <PaletteInput
        value={text}
        onValueChange={setText}
        placeholder="Open a tab, connection, script or table…"
        hint={`${SHORTCUTS.commandPalette} Commands`}
      />
      {/* No "nothing matches": the objects section always says what it is doing. */}
      <PaletteList>
        {tabRows.length > 0 && (
          <PaletteGroup heading="Open tabs">
            {tabRows.map((tab) => (
              <PaletteItem
                key={tab.id}
                value={`tab:${tab.id}`}
                onSelect={() => choose(() => activate(tab.id))}
                icon={tabIcon(tab)}
                label={tab.title}
                detail={profileName(tab.profileId) || undefined}
                hint={tab.id === activeTabId ? "Active" : undefined}
              />
            ))}
          </PaletteGroup>
        )}
        {connectionRows.length > 0 && (
          <PaletteGroup heading="Connections">
            {connectionRows.map((profile) => (
              <PaletteItem
                key={profile.id}
                value={`connection:${profile.id}`}
                onSelect={() => choose(() => openQuery(profile.id, null))}
                icon={<EngineIcon engine={profile.engine} decorative />}
                label={profile.name}
                detail={ENGINE_LABELS[profile.engine]}
                hint="New query"
              />
            ))}
          </PaletteGroup>
        )}
        {tree.isError && (
          <PaletteGroup heading="Scripts">
            <PaletteNote tone="danger">Could not read the scripts folder: {errorMessage(tree.error)}</PaletteNote>
          </PaletteGroup>
        )}
        {scriptRows.length > 0 && (
          <PaletteGroup heading="Scripts">
            {scriptRows.map((file) => (
              <PaletteItem
                key={file.path}
                value={`script:${file.path}`}
                onSelect={() => choose(() => void scripts.open(file.path))}
                icon={FileText}
                label={file.name}
                detail={parentPath(file.path) || undefined}
              />
            ))}
          </PaletteGroup>
        )}
        <ObjectsSection profile={working} q={q} typing={search !== q} choose={choose} />
      </PaletteList>
    </PaletteRoot>
  );
}
