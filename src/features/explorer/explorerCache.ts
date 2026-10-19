import type { QueryClient } from "@tanstack/react-query";
import { fieldSamplesKey } from "./fieldSamples";

/**
 * What the explorer caches of a connection: its tree's children, its detail tabs and the field samples shown on them
 * (by key prefix). The backend drops a profile's samples whenever the profile is saved, so the samples are read again
 * with the rest: a collection's detail says "Not sampled yet" once more.
 */
const explorerKeys = (profileId: string) => [["children", profileId], ["detail", profileId], fieldSamplesKey(profileId)];

/** The connection's objects changed, or may have: the tree and any open detail read them again. */
export async function invalidateExplorer(qc: QueryClient, profileId: string): Promise<void> {
  await Promise.all(explorerKeys(profileId).map((queryKey) => qc.invalidateQueries({ queryKey })));
}

/** The connection is gone: what the explorer held of it is dropped, not read again. */
export function forgetExplorer(qc: QueryClient, profileId: string): void {
  for (const queryKey of explorerKeys(profileId)) qc.removeQueries({ queryKey });
}
