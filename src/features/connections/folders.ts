import type { ProfileEntry } from "@/lib/api/types";

export interface FolderNode {
  name: string;
  path: string;
  folders: FolderNode[];
  profiles: ProfileEntry[];
}

/** Group profiles by their "/"-separated folder path. The root has name "" and path "". */
export function buildFolderTree(entries: ProfileEntry[]): FolderNode {
  const root: FolderNode = { name: "", path: "", folders: [], profiles: [] };
  for (const entry of entries) {
    const parts = entry.profile.folder
      .split("/")
      .map((p) => p.trim())
      .filter(Boolean);
    let node = root;
    for (const part of parts) {
      const path = node.path ? `${node.path}/${part}` : part;
      let child = node.folders.find((f) => f.name === part);
      if (!child) {
        child = { name: part, path, folders: [], profiles: [] };
        node.folders.push(child);
      }
      node = child;
    }
    node.profiles.push(entry);
  }
  const sort = (node: FolderNode) => {
    node.folders.sort((a, b) => a.name.localeCompare(b.name));
    node.profiles.sort((a, b) => a.profile.name.localeCompare(b.profile.name));
    node.folders.forEach(sort);
  };
  sort(root);
  return root;
}
