import type { QueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { objectSearchKey } from "../palette/objectSearchKey";
import { invalidateExplorer } from "./explorerCache";

/**
 * Refresh schema (spec §4.10): the backend forgets the connection's schema snapshot, so completion and object search
 * rebuild it; the explorer reads the connection's objects again (its tree, any open detail and the field samples shown
 * on a collection's detail: the backend keeps its samples, so they are only read again, not dropped), and earlier
 * object searches are dropped rather than shown until they are asked again.
 */
export async function refreshSchema(qc: QueryClient, profileId: string): Promise<void> {
  unwrap(await (await api()).POST("/metadata/schema/refresh", { body: { profile_id: profileId } }));
  console.info(`Schema of ${profileId} refreshed`);
  qc.removeQueries({ queryKey: objectSearchKey(profileId) });
  await invalidateExplorer(qc, profileId);
}
