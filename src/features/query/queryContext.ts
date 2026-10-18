import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { PathSegment, QueryContext } from "@/lib/api/types";
import { useWorkspace } from "@/stores/workspace";
import type { ExecutionState } from "../session/execution";
import type { RunState } from "./useQueryRunner";

/*
 * Couchbase's per-tab query context (spec §3.11.1): the bucket (and scope) that unqualified names resolve in. The
 * frontend never quotes a name: the text of a context comes from the backend (`ContextOption.text`).
 */

/** A tab's choice: undefined follows the profile's default context, "none" is no context (ruling B14). */
export type TabContext = QueryContext | "none" | undefined;

/** The context a tab's statements run in: its choice, else the session's default (the profile's database). */
export function effectiveContext(choice: TabContext, sessionDefault: QueryContext | null): QueryContext | null {
  if (choice === "none") return null;
  return choice ?? sessionDefault;
}

/** Whether `a` and `b` are the same context: the same bucket and scope (no scope is the same as a null one). */
export function sameContext(a: QueryContext | null | undefined, b: QueryContext | null | undefined): boolean {
  if (!a || !b) return !a && !b;
  return a.bucket === b.bucket && (a.scope ?? null) === (b.scope ?? null);
}

/** The context a tree node stands for: its bucket and its scope, if any; null for a node outside every bucket. */
export function contextOfPath(path: PathSegment[]): QueryContext | null {
  const bucket = path.find((segment) => segment.kind === "bucket");
  if (!bucket) return null;
  const scope = path.find((segment) => segment.kind === "scope");
  return scope ? { bucket: bucket.name, scope: scope.name } : { bucket: bucket.name };
}

/**
 * The contexts the picker offers (`GET /couchbase/contexts`). `current` is the tab's effective context: the backend
 * lists it too when its schema snapshot lacks it. While the options of another context load, the list shown is the
 * previous one (of the same connection).
 */
export function useContextOptions(profileId: string | null, current: QueryContext | null, enabled: boolean) {
  return useQuery({
    queryKey: ["couchbase-contexts", profileId, current?.bucket ?? null, current?.scope ?? null],
    queryFn: async ({ signal }) => {
      const query = {
        profile_id: profileId!,
        ...(current ? { bucket: current.bucket } : {}),
        ...(current?.scope != null ? { scope: current.scope } : {}),
      };
      return unwrap(await (await api()).GET("/couchbase/contexts", { params: { query }, signal }));
    },
    enabled: enabled && profileId !== null,
    retry: false, // the picker's menu shows the error; asking again delays it (and re-asks for a password)
    placeholderData: (previous, previousQuery) => (previousQuery?.queryKey[1] === profileId ? previous : undefined),
  });
}

/**
 * A click on a tree node (spec §3.11.1): when the active tab is a query tab on the node's connection, its context
 * becomes the node's bucket and scope. Nothing otherwise, and nothing for a node outside any bucket.
 */
export function applyTreeContext(profileId: string, path: PathSegment[]): void {
  const context = contextOfPath(path);
  if (!context) return;
  const { tabs, activeTabId, updateTab } = useWorkspace.getState();
  const tab = tabs.find((t) => t.id === activeTabId);
  if (tab?.kind !== "query" || tab.profileId !== profileId) return;
  if (typeof tab.queryContext === "object" && sameContext(tab.queryContext, context)) return;
  updateTab(tab.id, "query", { queryContext: context });
}

/** What `CompleteRequest.seen_fields` takes: at most this many names, each at most this long. */
const SEEN_FIELDS_MAX = 500;
const SEEN_FIELD_MAX_LENGTH = 300;
/** Result columns that are not document fields: `SELECT RAW`'s value, the grid's overflow column, a plan's text. */
const NOT_FIELDS: ReadonlySet<string> = new Set(["value", "(other fields)", "plan"]);

/**
 * Whether a column name is offered as a field seen in results. A name with a dot is left out (ruling B22): `SELECT *`
 * names a collision column `d.id`, and a suggestion would insert `` `d.id` ``, a key of its own. A name the backend
 * refuses (empty, over 300 characters, a lone surrogate) would make the whole completion request fail.
 */
function isSeenField(name: string): boolean {
  return (
    name !== "" &&
    name.length <= SEEN_FIELD_MAX_LENGTH &&
    !name.includes(".") &&
    !NOT_FIELDS.has(name) &&
    !/\p{Surrogate}/u.test(name)
  );
}

/**
 * The column names of the tab's latest run (every result set of every statement), unique, in order and at most 500:
 * what SQL++ completion offers as fields "seen in results".
 */
export function seenFieldsOf(run: RunState | null, executions: Record<string, ExecutionState>): string[] {
  const names = new Set<string>();
  for (const step of run?.steps ?? []) {
    const execution = step.requestId ? executions[step.requestId] : undefined;
    for (const set of execution?.resultSets ?? []) {
      for (const { name } of set.columns) {
        if (names.size >= SEEN_FIELDS_MAX) return [...names];
        if (isSeenField(name)) names.add(name);
      }
    }
  }
  return [...names];
}
