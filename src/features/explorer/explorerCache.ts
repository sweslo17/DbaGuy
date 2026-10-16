import type { QueryClient } from "@tanstack/react-query";

/** What the explorer caches of a connection: its tree's children and its detail tabs (by key prefix). */
const explorerKeys = (profileId: string) => [
  ["children", profileId],
  ["detail", profileId],
];

/** The connection's objects changed, or may have: the tree and any open detail read them again. */
export async function invalidateExplorer(qc: QueryClient, profileId: string): Promise<void> {
  await Promise.all(explorerKeys(profileId).map((queryKey) => qc.invalidateQueries({ queryKey })));
}

/** The connection is gone: what the explorer held of it is dropped, not read again. */
export function forgetExplorer(qc: QueryClient, profileId: string): void {
  for (const queryKey of explorerKeys(profileId)) qc.removeQueries({ queryKey });
}
