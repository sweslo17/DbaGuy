import { useQueries } from "@tanstack/react-query";
import { useMemo } from "react";
import { api, ApiError, errorMessage, unwrap } from "@/lib/api/client";
import { pathKey, type ObjectNode, type PathSegment, type ProfileEntry } from "@/lib/api/types";
import { buildFolderTree, type FolderNode } from "../connections/folders";
import { emptyChildrenHint } from "./kinds";

export type TreeRow =
  | { type: "folder"; key: string; name: string; depth: number }
  | { type: "profile"; key: string; entry: ProfileEntry; depth: number }
  | { type: "node"; key: string; profileId: string; node: ObjectNode; depth: number }
  | { type: "loading"; key: string; depth: number }
  /** An expanded node that lists nothing, where that can also mean "not allowed to see" (`emptyChildrenHint`). */
  | { type: "hint"; key: string; depth: number; text: string; title: string }
  | {
      type: "error";
      key: string;
      depth: number;
      message: string;
      code: string;
      /** The API error's `detail` (e.g. which passwords `password_required` asks for). */
      detail: unknown;
      profileId: string;
      retry: () => void;
    };

export const folderKey = (path: string) => `folder:${path}`;
export const profileKey = (profileId: string) => `profile:${profileId}`;
export const nodeKeyPrefix = (profileId: string) => `node:${profileId}:`;
export const nodeKey = (profileId: string, path: PathSegment[]) => `${nodeKeyPrefix(profileId)}${pathKey(path)}`;
export const childrenQueryKey = (profileId: string, path: PathSegment[]) =>
  ["children", profileId, pathKey(path)] as const;

/** The query for an object's children (`path` [] is the connection's root): shared by the tree and the query tab. */
export function childrenQuery(profileId: string, path: PathSegment[]) {
  return {
    queryKey: childrenQueryKey(profileId, path),
    queryFn: async () => unwrap(await (await api()).POST("/metadata/children", { body: { profile_id: profileId, path } })),
    staleTime: 5 * 60_000,
    retry: false, // the row offers an explicit Retry; auto-retrying delays the error and re-asks for passwords
  };
}

/** The object whose children an expanded row shows. */
export interface Expansion {
  profileId: string;
  path: PathSegment[];
}

/** Flattens folders → profiles → lazily loaded objects into visible rows. */
export function useTreeRows(
  entries: ProfileEntry[],
  expanded: ReadonlySet<string>,
  expansions: ReadonlyMap<string, Expansion>,
): TreeRow[] {
  const targets = useMemo(() => [...expansions.entries()].filter(([key]) => expanded.has(key)), [expansions, expanded]);
  const results = useQueries({
    queries: targets.map(([, target]) => childrenQuery(target.profileId, target.path)),
  });

  return useMemo(() => {
    const byKey = new Map(targets.map(([key], i) => [key, results[i]]));
    const rows: TreeRow[] = [];
    const pushChildren = (profileId: string, ownerKey: string, depth: number, owner?: ObjectNode) => {
      const result = byKey.get(ownerKey);
      if (!result || result.isPending) {
        rows.push({ type: "loading", key: `${ownerKey}#loading`, depth });
        return;
      }
      if (result.isError) {
        const err = result.error;
        rows.push({
          type: "error",
          key: `${ownerKey}#error`,
          depth,
          profileId,
          message: errorMessage(err),
          code: err instanceof ApiError ? err.code : "error",
          detail: err instanceof ApiError ? err.detail : undefined,
          retry: () => void result.refetch(),
        });
        return;
      }
      const hint = result.data?.length === 0 && owner ? emptyChildrenHint(owner.kind) : undefined;
      if (hint) rows.push({ type: "hint", key: `${ownerKey}#empty`, depth, ...hint });
      for (const node of result.data ?? []) {
        const key = nodeKey(profileId, node.path);
        rows.push({ type: "node", key, profileId, node, depth });
        if (node.has_children && expanded.has(key)) pushChildren(profileId, key, depth + 1, node);
      }
    };
    const walk = (folder: FolderNode, depth: number) => {
      for (const sub of folder.folders) {
        const key = folderKey(sub.path);
        rows.push({ type: "folder", key, name: sub.name, depth });
        if (expanded.has(key)) walk(sub, depth + 1);
      }
      for (const entry of folder.profiles) {
        const key = profileKey(entry.profile.id);
        rows.push({ type: "profile", key, entry, depth });
        if (expanded.has(key)) pushChildren(entry.profile.id, key, depth + 1);
      }
    };
    walk(buildFolderTree(entries), 0);
    return rows;
  }, [entries, expanded, targets, results]);
}
