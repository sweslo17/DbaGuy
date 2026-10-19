import { toast } from "@/components/ui/Toast";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import type { PathSegment } from "@/lib/api/types";
import { useWorkspace } from "@/stores/workspace";
import { contextOfPath } from "../query/queryContext";

/**
 * Opens a collection as a browse tab (spec D-04-11, ruling B19): a new query tab titled by the collection, with the
 * backend's SELECT of its documents, in the context of the collection's scope. Nothing is run: the user decides. A
 * collection outside any bucket has no context (it is opened with none, never with a guess).
 */
export async function openBrowseTab(profileId: string, path: PathSegment[]): Promise<void> {
  try {
    const body = { profile_id: profileId, path, where: null, order_by: [], count: false };
    const { sql } = unwrap(await (await api()).POST("/sql/browse-select", { body }));
    useWorkspace.getState().openQuery(profileId, null, {
      text: sql,
      title: path.at(-1)?.name,
      queryContext: contextOfPath(path) ?? "none",
    });
    console.info("Opened a collection in a browse tab");
  } catch (error) {
    toast("error", `Could not open the collection: ${errorMessage(error)}`);
  }
}
