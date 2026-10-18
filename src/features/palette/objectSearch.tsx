import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { pathKey, type ObjectNode, type ObjectSearchHit, type Profile } from "@/lib/api/types";
import { useWorkspace } from "@/stores/workspace";
import { useEngineInfo } from "../connections/hooks";
import { openBrowseTab } from "../explorer/browse";
import { kindIcon } from "../explorer/kinds";
import { objectSearchKey } from "./objectSearchKey";
import { PaletteGroup, PaletteItem, PaletteNote, type Choose } from "./parts";

/*
 * Quick open's database objects (spec §4.10, ruling R-02c-16): the tables and views of the connection being worked
 * with (its collections too, for Couchbase), found by the backend (`GET /metadata/search`, a fuzzy match over its
 * schema snapshot) and listed in its order. A table or view opens as the explorer would open it; a collection opens a
 * browse tab.
 */

const OBJECT_LIMIT = 50;

/**
 * The objects matching `q`, best first. Keyed by connection and text, so an older answer never replaces a newer one;
 * while a new one loads, the previous rows stay (as placeholders).
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

/** The node the explorer opens for a found table or view: the same path, so the same tab and title. */
function hitNode(hit: ObjectSearchHit): ObjectNode {
  return { kind: hit.kind, name: hit.name, path: hit.path, has_children: true, system: false };
}

/**
 * The section: a note when there is nothing to search (no connection, no text), "Searching…", no match, or why the
 * search failed (inline, not a toast). `typing`: the text has changed since `q` was searched, so the rows shown belong
 * to an older search; like placeholder rows while a newer answer loads, they are greyed and cannot be chosen yet.
 */
export function ObjectsSection({
  profile,
  q,
  typing,
  choose,
}: {
  profile: Profile | null;
  q: string;
  typing: boolean;
  choose: Choose;
}) {
  const openObject = useWorkspace((s) => s.openObject);
  // Only an engine with a query context (Couchbase) has collections to find; the others keep their wording.
  const collections = !!useEngineInfo(profile?.id ?? null)?.query_context;
  const openHit = (profileId: string, hit: ObjectSearchHit) =>
    hit.kind === "collection" ? void openBrowseTab(profileId, hit.path) : openObject(profileId, hitNode(hit));
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
        <PaletteNote>
          {collections ? "Type to search tables, views and collections" : "Type to search tables and views"}
        </PaletteNote>
      </PaletteGroup>
    );
  }
  const hits = search.isError ? [] : (search.data ?? []);
  const waiting = search.isPending || search.isPlaceholderData;
  const stale = typing || search.isPlaceholderData;
  return (
    <PaletteGroup heading={heading}>
      {search.isError && <PaletteNote tone="danger">Could not search: {errorMessage(search.error)}</PaletteNote>}
      {!search.isError && hits.length === 0 && waiting && (
        // Shown only if the answer takes a moment.
        <div className="animate-delayed-show">
          <PaletteNote>Searching…</PaletteNote>
        </div>
      )}
      {!search.isError && hits.length === 0 && !waiting && (
        <PaletteNote>
          {collections ? "No table, view or collection matches" : "No table or view matches"} “{q}”
        </PaletteNote>
      )}
      {hits.map((hit) => {
        // The explorer path names the object uniquely: database, schema, kind and name.
        const id = pathKey(hit.path);
        return (
          <PaletteItem
            key={id}
            value={`object:${id}`}
            pending={stale}
            onSelect={() => choose(() => openHit(profile.id, hit))}
            icon={kindIcon(hit.kind)}
            label={hit.name}
            detail={hit.database ? `${hit.database} · ${hit.schema_name}` : hit.schema_name}
          />
        );
      })}
    </PaletteGroup>
  );
}
