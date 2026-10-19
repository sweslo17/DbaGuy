import { api, ApiError, unwrap } from "@/lib/api/client";
import type { DocumentView, Keyspace, PathSegment } from "@/lib/api/types";
import type { DocumentTabState } from "@/stores/workspace";
import { keyspaceOfPath } from "../explorer/fieldSamples";

/*
 * The document API (`/couchbase/documents/*`, spec §3.9): a document is read as the server stores it, exact text and
 * META. Nothing here logs a document's text or ID.
 */

/** A document: the connection, the collection and the ID. */
export interface DocumentRef {
  profileId: string;
  keyspace: Keyspace;
  id: string;
}

/** The tab path of a document: its collection's segments, then the document, named by its ID. */
export function documentPath(keyspace: Keyspace, id: string): PathSegment[] {
  return [
    { kind: "bucket", name: keyspace.bucket },
    { kind: "scope", name: keyspace.scope },
    { kind: "collection", name: keyspace.collection },
    { kind: "document", name: id },
  ];
}

/** The document a tab shows. A tab's path always names one (`openDocument`, and a restored file is checked). */
export function documentRef(tab: DocumentTabState): DocumentRef {
  const keyspace = keyspaceOfPath(tab.path);
  const id = tab.path.at(-1);
  if (!keyspace || id?.kind !== "document") throw new Error("The tab does not name a document");
  return { profileId: tab.profileId, keyspace, id: id.name };
}

/** The document as stored; a 404 `document_missing` when there is none (`isDocumentMissing`). */
export async function getDocument(ref: DocumentRef, signal?: AbortSignal): Promise<DocumentView> {
  const body = { profile_id: ref.profileId, keyspace: ref.keyspace, id: ref.id };
  return unwrap(await (await api()).POST("/couchbase/documents/get", { body, signal }));
}

/** Whether `error` says the document does not exist (not that reading it failed). */
export const isDocumentMissing = (error: unknown): boolean =>
  error instanceof ApiError && error.code === "document_missing";
