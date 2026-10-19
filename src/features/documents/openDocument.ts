import { create } from "zustand";
import { ApiError } from "@/lib/api/client";
import type { ColumnMeta, Keyspace, QueryContext } from "@/lib/api/types";
import { statementKeyspace } from "./documentApi";

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

/** What a run's context tells of the collection: its bucket and scope (nothing without a context). */
function contextKeyspace(queryContext: QueryContext | null | undefined): Partial<Keyspace> {
  if (!queryContext) return {};
  return queryContext.scope ? { bucket: queryContext.bucket, scope: queryContext.scope } : { bucket: queryContext.bucket };
}

/**
 * Where a grid's "Open document…" starts (ruling C44): the collection the executed statement names when it names
 * exactly one, as the backend reads it under the run's context; otherwise the run's context (the dialog then fills the
 * collection with the one opened last). A statement that reads other collections than the tab's context says is why the
 * statement comes first. The lookup never stops the dialog: when it fails, the context is what is used.
 */
export async function prefillKeyspace(
  sql: string,
  queryContext: QueryContext | null | undefined,
): Promise<Partial<Keyspace>> {
  try {
    const named = await statementKeyspace(sql, queryContext);
    if (named) return named;
  } catch (error) {
    console.warn("Could not read the keyspace of a statement", error instanceof ApiError ? error.code : error);
  }
  return contextKeyspace(queryContext);
}
