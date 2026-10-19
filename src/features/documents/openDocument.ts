import { create } from "zustand";
import type { ColumnMeta, Keyspace } from "@/lib/api/types";

/** What asks for the "Open document" dialog: where it opens, and what it starts with. */
export interface OpenDocumentRequest {
  profileId: string;
  /** "new": the dialog asks for the ID of a document to create (Task 7). */
  mode: "open" | "new";
  /** What is known of the collection: the dialog asks for the rest. */
  keyspace: Partial<Keyspace>;
  id?: string;
}

/** The one dialog (`OpenDocumentDialogHost`), as the tree's menu and a grid's cell menu ask for it. */
const useOpenDocument = create<{ request: OpenDocumentRequest | null }>(() => ({ request: null }));

export function askOpenDocument(request: OpenDocumentRequest): void {
  useOpenDocument.setState({ request });
}

export const useOpenDocumentRequest = (): OpenDocumentRequest | null => useOpenDocument((s) => s.request);

export function closeOpenDocument(): void {
  useOpenDocument.setState({ request: null });
}

/** The collection last opened in a scope, by connection, bucket and scope (this run of the app only). */
const lastCollections = new Map<string, string>();
const scopeKey = (profileId: string, bucket: string, scope: string) => JSON.stringify([profileId, bucket, scope]);

/** The collection last opened for this connection's bucket and scope (this run of the app only). */
export function rememberCollection(profileId: string, keyspace: Keyspace): void {
  lastCollections.set(scopeKey(profileId, keyspace.bucket, keyspace.scope), keyspace.collection);
}

export function lastCollection(profileId: string, bucket: string, scope: string): string | undefined {
  return lastCollections.get(scopeKey(profileId, bucket, scope));
}

/**
 * The ID a grid cell suggests: the row's `id` column when it is a string (`SELECT META(d).id, …`), else the clicked
 * cell's text when it is a string, else nothing.
 */
export function idPrefill(columns: readonly ColumnMeta[], row: readonly unknown[], col: number): string {
  const idColumn = columns.findIndex((column) => column.name === "id");
  const id = idColumn >= 0 ? row[idColumn] : undefined;
  if (typeof id === "string") return id;
  const clicked = row[col];
  return typeof clicked === "string" ? clicked : "";
}
