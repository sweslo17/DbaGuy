import { keepPreviousData, useInfiniteQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { toast } from "@/components/ui/Toast";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import type { HistoryEntry, HistoryPage } from "@/lib/api/types";
import { mountedEditor } from "@/stores/editorBuffers";
import { useWorkspace } from "@/stores/workspace";
import { useProfiles } from "../connections/hooks";

/*
 * The query history (spec §4.6): the backend's `/history` API, a search as one infinite query, and what the History
 * panel does with an entry. The backend records a statement when it ends, before it reports it finished.
 */

export const historyKey = ["history"] as const;
const PAGE_SIZE = 100;

/** Refetches the history being shown (every search and filter that is mounted); the others refetch when next shown. */
export function refreshHistory(qc: QueryClient): void {
  void qc.invalidateQueries({ queryKey: historyKey });
}

async function fetchHistoryPage(
  q: string,
  profileId: string | null,
  before: number | null,
  signal: AbortSignal,
): Promise<HistoryPage> {
  const query = {
    q: q || undefined,
    profile_id: profileId ?? undefined,
    limit: PAGE_SIZE,
    before: before ?? undefined,
  };
  return unwrap(await (await api()).GET("/history", { params: { query }, signal }));
}

/**
 * The statements whose SQL contains `q` ("" for all), newest first, run on one connection (`profileId`) or on any
 * (null); a page of 100 at a time, the next one from `fetchNextPage`. A new search shows the previous one's rows until
 * its own arrive.
 */
export function useHistory(q: string, profileId: string | null) {
  return useInfiniteQuery({
    queryKey: [...historyKey, { q, profileId }],
    queryFn: ({ pageParam, signal }) => fetchHistoryPage(q, profileId, pageParam, signal),
    initialPageParam: null as number | null,
    getNextPageParam: (last) => last.next_before,
    placeholderData: keepPreviousData,
  });
}

/**
 * What the History panel does with an entry: insert its SQL into the active query tab, open it in a new tab, and clear
 * the whole history. A new tab opens on the entry's connection and database, or on none (the user picks one) when that
 * connection is known to be deleted.
 */
export function useHistoryActions() {
  const qc = useQueryClient();
  const profiles = useProfiles().data;
  return useMemo(() => {
    const openInNewTab = (entry: HistoryEntry) => {
      // Only a loaded list without the profile means it was deleted; while the list is unknown, the entry's own counts.
      const deleted = !!profiles && !profiles.some((item) => item.profile.id === entry.profile_id);
      const { openQuery } = useWorkspace.getState();
      openQuery(deleted ? null : entry.profile_id, deleted ? null : entry.database, { text: entry.sql });
      console.info(`Opened history entry ${entry.id} in a new query tab`);
      if (deleted) {
        toast("info", `The connection “${entry.profile_name}” no longer exists. Choose one for the new tab.`);
      }
    };

    /** At the cursor of the active query tab, replacing its selection; with no query tab active, in a new tab. */
    const insert = (entry: HistoryEntry) => {
      const { tabs, activeTabId } = useWorkspace.getState();
      const active = tabs.find((tab) => tab.id === activeTabId);
      const editor = active?.kind === "query" ? mountedEditor(active.id) : null;
      if (editor) editor.insert(entry.sql);
      else openInNewTab(entry);
    };

    /** Deletes every entry. Resolves to whether it did; a failure is shown as a toast. */
    const clear = async (): Promise<boolean> => {
      try {
        unwrap(await (await api()).DELETE("/history"));
        console.info("Cleared the query history");
        return true;
      } catch (error) {
        toast("error", `Could not clear the history: ${errorMessage(error)}`);
        return false;
      } finally {
        refreshHistory(qc);
      }
    };

    return { insert, openInNewTab, clear };
  }, [qc, profiles]);
}
