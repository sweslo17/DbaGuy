import { api, ApiError, unwrap } from "@/lib/api/client";
import type { DocumentView, Keyspace, PathSegment, QueryContext, SaveAnswer, SavePreview } from "@/lib/api/types";
import type { DocumentTabState } from "@/stores/workspace";
import { keyspaceOfPath } from "../explorer/fieldSamples";
import { formatJson, parseJson } from "../grid/json";

/*
 * The document API (`/couchbase/documents/*`, spec §3.9): a document is read as the server stores it, exact text and
 * META; saved, inserted and removed with the CAS it was read with. Nothing here logs a document's text or ID.
 *
 * Writes are plain calls, never a `useQuery` (the app's QueryClient retries once, which would send a write twice), and
 * carry no `AbortSignal` and no timeout of their own: a write that was sent is never taken back (ruling C45).
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

/**
 * The collection a statement reads when it names exactly one (fully qualified, or resolved against the run's context as
 * SQL++ resolves it); null for none or several. The backend reads the statement (`POST /couchbase/statement-keyspace`):
 * the frontend never parses SQL++.
 */
export async function statementKeyspace(
  sql: string,
  queryContext: QueryContext | null | undefined,
  signal?: AbortSignal,
): Promise<Keyspace | null> {
  const body = { sql, query_context: queryContext ?? null };
  return unwrap(await (await api()).POST("/couchbase/statement-keyspace", { body, signal }));
}

/** What a save or an insert answers when the server would store other tokens than the user typed (nothing written yet). */
export const isPreview = (answer: SaveAnswer): answer is SavePreview => answer.status === "needs_confirmation";

/**
 * Writes `text` over the document if it still has `cas`, keeping its expiration. An unconfirmed save whose text the
 * server would store differently comes back as a `SavePreview` and writes nothing; the same call with `confirmed` writes.
 */
export async function saveDocument(ref: DocumentRef, cas: string, text: string, confirmed: boolean): Promise<SaveAnswer> {
  const body = { profile_id: ref.profileId, keyspace: ref.keyspace, id: ref.id, cas, text, confirmed };
  return unwrap(await (await api()).POST("/couchbase/documents/save", { body }));
}

/** Creates the document, previewed like a save; a 409 `document_exists` when the ID is taken. */
export async function insertDocument(ref: DocumentRef, text: string, confirmed: boolean): Promise<SaveAnswer> {
  const body = { profile_id: ref.profileId, keyspace: ref.keyspace, id: ref.id, text, confirmed };
  return unwrap(await (await api()).POST("/couchbase/documents/insert", { body }));
}

/** Deletes the document if it still has `cas`. */
export async function removeDocument(ref: DocumentRef, cas: string): Promise<void> {
  const body = { profile_id: ref.profileId, keyspace: ref.keyspace, id: ref.id, cas };
  unwrap(await (await api()).POST("/couchbase/documents/remove", { body }));
}

/**
 * `text` pretty-printed (numbers and key order as written), or as it is when it cannot be parsed (nested deeper than
 * `parseJson`'s 500 levels, ruling C34): the server's text is shown either way.
 */
export function prettyJson(text: string): string {
  const parsed = parseJson(text);
  return parsed ? formatJson(parsed.root) : text;
}

/** Why a write did not go through, as the tab says it: one line each (`problemMessage`), some with a button. */
export type SaveProblem =
  | { kind: "invalid_json"; offset: number; reason: string; line: number; column: number }
  | { kind: "numbers"; paths: string[] }
  | { kind: "exists" }
  | { kind: "outcome_unknown"; message: string }
  | { kind: "error"; message: string };

/** The three writes of a document tab: the first two are a "save" to the user, the third a "delete". */
export type WriteKind = "save" | "insert" | "remove";

/** What a write is called, and what may have become of the document, in a message. */
const WORDING: Record<WriteKind, { noun: string; done: string }> = {
  save: { noun: "save", done: "written" },
  insert: { noun: "save", done: "written" },
  remove: { noun: "delete", done: "deleted" },
};

/**
 * A write whose outcome is not known (rulings C45, F3): it may have been done. `cause` says why ("Lost contact with the
 * backend during the save"). It stays on screen while the text is edited and offers Reload; the write is never sent again.
 */
export function unknownOutcome(kind: WriteKind, cause: string): Extract<SaveProblem, { kind: "outcome_unknown" }> {
  return {
    kind: "outcome_unknown",
    message: `${cause}: it may or may not have been ${WORDING[kind].done}. Reload to see what is stored.`,
  };
}

/** What a failed save, insert or delete means for the tab. */
export type WriteFailure =
  /** `document_conflict`: the document was changed after it was opened. */
  | { kind: "changed" }
  /** `document_missing`: the document does not exist (any more). */
  | { kind: "deleted" }
  /**
   * `password_required` (rulings C31, C48): nothing was sent. `error` is the one the unlock dialog reads (same code and
   * detail), its message saying the write was not done.
   */
  | { kind: "password"; error: ApiError }
  | { kind: "problem"; problem: SaveProblem };

const detailOf = (error: ApiError): Record<string, unknown> =>
  typeof error.detail === "object" && error.detail !== null ? (error.detail as Record<string, unknown>) : {};

/** The problem for an `ApiError` that is none of the codes with a meaning of their own: the server's message. */
const messageProblem = (error: Error): WriteFailure => ({ kind: "problem", problem: { kind: "error", message: error.message } });

/**
 * Reads the failure of a save, insert or delete (`kind`) by its `ApiError.code` (backend `domain/errors.py`).
 *
 * A failure without an `ApiError` (a fetch `TypeError` from a backend that restarted or died mid-write, any other throw)
 * is `outcome_unknown` (ruling C45): the request may have reached the server, so it is never sent again. So is a
 * `write_outcome_unknown` answer (with the backend's own message) and an `internal_error`, which may have happened after
 * the write. Every other answer carries the server's message as it is (`binary_document`, `read_only`, a `query_failed`
 * that says nothing was sent, …).
 *
 * `position` turns a UTF-16 offset into a line and column of the text that was sent (the editor's); it is only asked for
 * `invalid_json`.
 */
export function classifyWriteError(
  error: unknown,
  kind: WriteKind,
  position: (offset: number) => { line: number; column: number },
): WriteFailure {
  if (!(error instanceof ApiError)) {
    return { kind: "problem", problem: unknownOutcome(kind, `Lost contact with the backend during the ${WORDING[kind].noun}`) };
  }
  const detail = detailOf(error);
  switch (error.code) {
    case "document_conflict":
      return { kind: "changed" };
    case "document_missing":
      return { kind: "deleted" };
    case "password_required": {
      // The profile's unlock dialog asks for it (`RecoverableError`; `isPasswordRequired`): the same code and detail.
      const done = kind === "remove" ? "Not deleted" : "Not saved";
      const message = `${done}: the connection needs its password. ${error.message}`;
      return { kind: "password", error: new ApiError(message, error.code, error.status, error.detail) };
    }
    case "document_exists":
      return { kind: "problem", problem: { kind: "exists" } };
    case "write_outcome_unknown":
      return { kind: "problem", problem: { kind: "outcome_unknown", message: error.message } };
    case "internal_error":
      return {
        kind: "problem",
        problem: unknownOutcome(kind, `The backend reported an internal error during the ${WORDING[kind].noun}`),
      };
    case "invalid_json": {
      const { offset, reason } = detail;
      if (typeof offset !== "number" || !Number.isFinite(offset) || typeof reason !== "string") return messageProblem(error);
      return { kind: "problem", problem: { kind: "invalid_json", offset, reason, ...position(offset) } };
    }
    case "number_out_of_range": {
      const { paths } = detail;
      if (!Array.isArray(paths) || !paths.every((path) => typeof path === "string")) return messageProblem(error);
      return { kind: "problem", problem: { kind: "numbers", paths } };
    }
    default:
      return messageProblem(error);
  }
}

/** The line under the bar for a problem. */
export function problemMessage(problem: SaveProblem): string {
  switch (problem.kind) {
    case "invalid_json":
      return `Not valid JSON at line ${problem.line}, column ${problem.column}: ${problem.reason}`;
    case "numbers":
      return `Couchbase would change these numbers: ${problem.paths.join(", ")} — write them as strings to save`;
    case "exists":
      return "A document with this ID already exists";
    case "outcome_unknown":
    case "error":
      return problem.message;
  }
}
