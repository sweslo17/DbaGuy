import { useMutation, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { QueryContext } from "@/lib/api/types";

/** How long the view of a plan text is kept once nothing shows it (the key holds the whole text: up to 4 MiB). */
const PLAN_KEPT_MS = 30_000;

/**
 * The view of a plan's JSON text (`POST /couchbase/plan`: an EXPLAIN, an ADVISE or a profile as the grid holds it).
 * Pure on the backend, so a text is asked for once; null asks for nothing.
 */
export function usePlanView(text: string | null) {
  return useQuery({
    queryKey: ["plan", text],
    queryFn: async ({ signal }) =>
      unwrap(await (await api()).POST("/couchbase/plan", { body: { text: text! }, signal })),
    enabled: text !== null,
    staleTime: Infinity,
    gcTime: PLAN_KEPT_MS,
    retry: false,
  });
}

/** What index advice is asked for. */
export interface AdviceRequest {
  profileId: string;
  /** The statement to advise on, as it ran. */
  statement: string;
  /** The tab's effective context: without it the advice names keyspaces unqualified and cannot be run as it is. */
  queryContext: QueryContext | null;
}

/**
 * `ADVISE <statement>` on the connection's metadata connection (`POST /couchbase/advise`), not through the tab's
 * session: it never ends a paused result or joins a run. A server error (5001 on a collection holding binary
 * documents) is the mutation's error, with the server's own text.
 */
export function useAdvice() {
  return useMutation({
    mutationFn: async ({ profileId, statement, queryContext }: AdviceRequest) =>
      unwrap(
        await (await api()).POST("/couchbase/advise", {
          body: { profile_id: profileId, statement, query_context: queryContext },
        }),
      ),
  });
}
