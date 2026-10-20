import { queryOptions } from "@tanstack/react-query";
import { api, ApiError, errorMessage, unwrap } from "@/lib/api/client";
import { pathKey, type EditChanges, type EditPreview, type EditTarget, type PathSegment } from "@/lib/api/types";

/*
 * The grid-edit API (spec ⑤ §3.4, the 05a contract): a table's edit target, and the preview of a commit. A preview
 * writes nothing (it keeps a one-time plan in the backend's memory), but it is a plain call, never a `useQuery`: each
 * one makes a new plan, and only the one shown in the dialog is ever applied (05a M7). Nothing here logs a value or a key.
 */

/**
 * The query-key prefix of every edit target of a profile: saving the profile reads them again (pre-flight L21), since
 * Read-only or the database it opens changes what can be edited.
 */
export const editTargetsKey = (profileId: string) => ["edits", "target", profileId] as const;

/** The query key of a table's edit target: read once per table tab, again on Refresh and when the profile is saved. */
export const editTargetKey = (profileId: string, path: PathSegment[]) =>
  [...editTargetsKey(profileId), pathKey(path)] as const;

/**
 * The edit target's query: the one every reader of a table's target uses (the table tab's first browse and its anchor,
 * 05b K13/K14; Task 9's edits), so they share one read. Read when the tab is first shown and kept while a tab uses it
 * (`staleTime: Infinity`: Refresh and a saved profile read it again), never retried, and dropped as soon as no tab uses
 * it (`gcTime: 0`): a tab opened again reads the table as it is now, never a key renamed meanwhile (the first browse
 * would sort by a column that is gone, with no header yet to clear the sort).
 */
export const editTargetQuery = (profileId: string, path: PathSegment[]) =>
  queryOptions({
    queryKey: editTargetKey(profileId, path),
    queryFn: ({ signal }) => fetchEditTarget(profileId, path, signal),
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
  });

/** How the table's rows are identified and written; `editable` false with the reason the edit bar shows. */
export async function fetchEditTarget(
  profileId: string,
  path: PathSegment[],
  signal?: AbortSignal,
): Promise<EditTarget> {
  return unwrap(await (await api()).POST("/edits/target", { body: { profile_id: profileId, path }, signal }));
}

/** The exact statements a commit of `changes` sends, and a plan id when nothing is wrong with them. */
export async function previewEdits(profileId: string, path: PathSegment[], changes: EditChanges): Promise<EditPreview> {
  return unwrap(await (await api()).POST("/edits/preview", { body: { profile_id: profileId, path, changes } }));
}

/**
 * What to show for a failed `/edits/*` request (05a M5): a 422 with `detail` has its reason in `detail[0].msg` (the
 * 64 MiB limit, a refused request shape) while its message is the generic "Invalid request"; any other failure's
 * message is its own.
 */
export function editsErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 422 && Array.isArray(error.detail)) {
    const first: unknown = error.detail[0];
    if (first && typeof first === "object" && typeof (first as { msg?: unknown }).msg === "string") {
      const msg = (first as { msg: string }).msg;
      if (msg) return msg;
    }
  }
  return errorMessage(error);
}
