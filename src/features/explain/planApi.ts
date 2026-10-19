import { useMutation, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { QueryContext } from "@/lib/api/types";

/** How long the view of a plan is kept once nothing shows it (a big plan's view is sizeable). */
const PLAN_KEPT_MS = 30_000;

/** The text of a plan set (the one cell of an EXPLAIN, an ADVISE or a profile) and which set of which request it is. */
export interface PlanText {
  requestId: string;
  setIndex: number;
  text: string;
}

/**
 * What a plan's view is cached under: the request and the set, with the text's length as a guard. The key stays small
 * (the text is up to 4 MiB and a key is hashed whenever the component renders). A request id is never reused and a
 * set's cell never changes once it is there, so two plans never share a key; a visited plan tab, which stays mounted,
 * keeps its view, and one shown again within `PLAN_KEPT_MS` of leaving finds it.
 */
export function planViewKey(plan: PlanText | null) {
  return ["plan", plan?.requestId ?? null, plan?.setIndex ?? null, plan?.text.length ?? null] as const;
}

/** The query for the view of a plan's JSON text (`POST /couchbase/plan`); null asks for nothing. */
export function planViewOptions(plan: PlanText | null) {
  const text = plan?.text ?? null;
  return {
    queryKey: planViewKey(plan),
    // The text is not in the key: this reads it from the render that started the request.
    queryFn: async ({ signal }: { signal: AbortSignal }) =>
      unwrap(await (await api()).POST("/couchbase/plan", { body: { text: text! }, signal })),
    enabled: text !== null,
    staleTime: Infinity, // the analysis is pure: the same plan always gives the same view
    gcTime: PLAN_KEPT_MS,
    retry: false,
  };
}

/** The view of a plan set's text, analysed by the backend (asked for once per request and set). */
export function usePlanView(plan: PlanText | null) {
  return useQuery(planViewOptions(plan));
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
