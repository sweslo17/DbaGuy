import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { defaultFilter } from "cmdk";
import { FileCode2, FileText, Server } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import type { ObjectNode, ObjectSearchHit, Profile, ScriptNode } from "@/lib/api/types";
import { parentPath } from "@/lib/scriptPaths";
import { useWorkspace, workingProfileId, type Tab } from "@/stores/workspace";
import { ENGINE_LABELS } from "../connections/ConnectionDialog";
import { useProfiles } from "../connections/hooks";
import { kindIcon } from "../explorer/kinds";
import { objectSearchKey } from "../explorer/refreshSchema";
import { useScriptActions, useScriptTree } from "../scripts/useScripts";
import { SHORTCUTS } from "./commands";
import { PaletteGroup, PaletteInput, PaletteItem, PaletteList, PaletteNote, PaletteRoot, type Choose } from "./parts";

/** How long typing pauses before the objects are searched. */
const SEARCH_DELAY_MS = 150;
const OBJECT_LIMIT = 50;
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

/** The node the explorer opens for a found table or view: the same path, so the same tab and title. */
function hitNode(hit: ObjectSearchHit): ObjectNode {
  return { kind: hit.kind, name: hit.name, path: hit.path, has_children: true, system: false };
}

/** The icon of a tab: a query, or the object a table or object tab shows. */
function tabIcon(tab: Tab) {
  return tab.kind === "query" ? FileCode2 : kindIcon(tab.path[tab.path.length - 1]?.kind ?? tab.kind);
}

/**
 * Tables and views of the connection whose name matches `q` (`GET /metadata/search`, the backend's fuzzy match over its
 * schema snapshot), best first. Keyed by connection and text, so an older answer never replaces a newer one; while a
 * new one loads, the previous rows stay.
 */
function useObjectSearch(profileId: string | null, q: string) {
  return useQuery({
    queryKey: objectSearchKey(profileId, q),
    queryFn: async ({ signal }) => {
      const query = { profile_id: profileId!, q, limit: OBJECT_LIMIT };
      return unwrap(await (await api()).GET("/metadata/search", { params: { query }, signal }));
    },
    enabled: profileId !== null && q !== "",
    retry: false, // the error shows in its row; asking again delays it (and re-asks for a password)
    placeholderData: keepPreviousData,
  });
}

function ObjectsSection({
  profile,
  q,
  choose,
}: {
  profile: Profile | null;
  q: string;
  choose: Choose;
}) {
  const openObject = useWorkspace((s) => s.openObject);
  const search = useObjectSearch(profile?.id ?? null, q);
  const heading = profile ? `Database objects · ${profile.name}` : "Database objects";
  if (!profile) {
    return (
      <PaletteGroup heading={heading}>
        <PaletteNote>Open a connection to search objects</PaletteNote>
      </PaletteGroup>
    );
  }
  if (!q) {
    return (
      <PaletteGroup heading={heading}>
        <PaletteNote>Type to search tables and views</PaletteNote>
      </PaletteGroup>
    );
  }
  const hits = search.isError ? [] : (search.data ?? []);
  // While the answer for `q` is on its way the previous rows stay; with none, "Searching…" (only if it takes a moment).
  const waiting = search.isPending || search.isPlaceholderData;
  return (
    <PaletteGroup heading={heading}>
      {search.isError && <PaletteNote tone="danger">Could not search: {errorMessage(search.error)}</PaletteNote>}
      {!search.isError && hits.length === 0 && waiting && (
        <div className="animate-delayed-show">
          <PaletteNote>Searching…</PaletteNote>
        </div>
      )}
      {!search.isError && hits.length === 0 && !waiting && (
        <PaletteNote>No table or view matches “{q}”</PaletteNote>
      )}
      {hits.map((hit) => {
        const id = `${hit.database ?? ""}/${hit.schema_name}/${hit.name}`;
        return (
          <PaletteItem
            key={id}
            value={`object:${id}`}
            onSelect={() => choose(() => openObject(profile.id, hitNode(hit)))}
            icon={kindIcon(hit.kind)}
            label={hit.name}
            detail={hit.database ? `${hit.database} · ${hit.schema_name}` : hit.schema_name}
          />
        );
      })}
    </PaletteGroup>
  );
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
  const tree = useScriptTree({ cached: true }).data;
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
  const files = useMemo(() => scriptFiles(tree?.items ?? []), [tree]);

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
                icon={Server}
                label={profile.name}
                detail={ENGINE_LABELS[profile.engine]}
                hint="New query"
              />
            ))}
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
        <ObjectsSection profile={working} q={q} choose={choose} />
      </PaletteList>
    </PaletteRoot>
  );
}
