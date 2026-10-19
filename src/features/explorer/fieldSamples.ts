import { useIsMutating, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { FieldPath, FieldSample, Keyspace, PathSegment } from "@/lib/api/types";

/*
 * Couchbase field samples (R-04-11): the field paths of a collection's documents, read only when the user asks (the
 * tree's "Sample fields…", or the collection's detail page) and kept by the backend per profile and keyspace. Completion
 * reads that cache, so what the detail page shows is what completion offers. Nothing here ever holds, logs or shows a
 * document's value: a sample is field paths, their kinds and counts.
 */

const SAMPLE_QUERY = "field-sample";

/** What a connection's samples are cached under: the prefix to invalidate when its profile changes. */
export const fieldSamplesKey = (profileId: string) => [SAMPLE_QUERY, profileId] as const;

/** The cached sample of one collection (`GET /couchbase/fields`). */
export const fieldSampleKey = (profileId: string, keyspace: Keyspace) =>
  [...fieldSamplesKey(profileId), keyspace.bucket, keyspace.scope, keyspace.collection] as const;

/** The collection a tree path stands for (its `bucket`, `scope` and `collection` segments); null without all three. */
export function keyspaceOfPath(path: PathSegment[]): Keyspace | null {
  const nameOf = (kind: string) => path.find((segment) => segment.kind === kind)?.name;
  const bucket = nameOf("bucket");
  const scope = nameOf("scope");
  const collection = nameOf("collection");
  return bucket !== undefined && scope !== undefined && collection !== undefined ? { bucket, scope, collection } : null;
}

const sameKeyspace = (a: Keyspace, b: Keyspace) =>
  a.bucket === b.bucket && a.scope === b.scope && a.collection === b.collection;

/** "dbaguy › app › users": the collection as the user reads it (names, never SQL). */
export const keyspaceLabel = (keyspace: Keyspace): string =>
  `${keyspace.bucket} › ${keyspace.scope} › ${keyspace.collection}`;

/** The keyspace's last sample, or null when it has none: nothing samples by itself. */
export function useFieldSample(profileId: string, keyspace: Keyspace) {
  return useQuery({
    queryKey: fieldSampleKey(profileId, keyspace),
    queryFn: async ({ signal }): Promise<FieldSample | null> => {
      const query = { profile_id: profileId, ...keyspace };
      return unwrap(await (await api()).GET("/couchbase/fields", { params: { query }, signal })) ?? null;
    },
    retry: false, // it only reads the backend's cache: asking again would not change the answer
  });
}

export interface SampleRequest {
  profileId: string;
  keyspace: Keyspace;
  /** Documents to read; null: the setting's `couchbase.sample_size`. */
  size: number | null;
}

const SAMPLE_MUTATION = ["sample-fields"] as const;

/**
 * Samples a collection (`POST /couchbase/fields/sample`) and puts the result under `fieldSampleKey`. A server error
 * (13014 with the missing role, for a user who may not scan the keyspace) is the mutation's error with the server's
 * own text; it is not retried. The cache is filled here, not in a `mutate` callback: a sample whose dialog was closed
 * with Cancel still finishes in the background and lands in the cache.
 */
export function useSampleFields() {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: SAMPLE_MUTATION,
    mutationFn: async ({ profileId, keyspace, size }: SampleRequest) =>
      unwrap(
        await (await api()).POST("/couchbase/fields/sample", {
          body: { profile_id: profileId, keyspace, size },
        }),
      ),
    onSuccess: (sample, { profileId, keyspace }) => {
      qc.setQueryData<FieldSample | null>(fieldSampleKey(profileId, keyspace), sample);
      console.info(`Sampled ${sample.documents} documents: ${sample.fields.length} field paths in ${sample.elapsed_ms} ms`);
    },
  });
}

/** Whether a sample of this collection is running, whoever started it (the detail page, or a dialog since closed). */
export function useIsSampling(profileId: string, keyspace: Keyspace): boolean {
  const running = useIsMutating({
    mutationKey: SAMPLE_MUTATION,
    predicate: (mutation) => {
      const request = mutation.state.variables as SampleRequest | undefined;
      return request?.profileId === profileId && sameKeyspace(request.keyspace, keyspace);
    },
  });
  return running > 0;
}

/** A field path as the table writes it: keys joined by `.`, `[]` for an array's elements, `(document)` for the root. */
export function formatFieldPath(path: FieldPath["path"]): string {
  return path.length === 0 ? "(document)" : path.map((key) => key ?? "[]").join(".");
}

/** The JSON kinds a path was seen with, most documents first: `string 8 · null 2`. */
export function formatKinds(types: FieldPath["types"]): string {
  return Object.entries(types)
    .sort(([, a], [, b]) => b - a)
    .map(([kind, count]) => `${kind} ${count.toLocaleString()}`)
    .join(" · ");
}

/**
 * What a sample left out, each with the reason. The limits are the backend's (`sampling.py`: SAMPLE_MAX_BYTES,
 * FIELD_PATHS_MAX, FIELD_KEY_MAX_CHARS); the sample says that a limit was reached, not what the limit is.
 */
export function sampleNotes(sample: FieldSample): { label: string; explanation: string }[] {
  const notes: { label: string; explanation: string }[] = [];
  if (sample.cut_at_bytes) {
    notes.push({
      label: "stopped at 32 MB",
      // The document that crossed the limit may have been the collection's last: "may", never "does".
      explanation: `Reading stops once the documents read pass 32 MB, so this sample may hold fewer documents than the ${sample.requested.toLocaleString()} asked for.`,
    });
  }
  if (sample.paths_capped) {
    notes.push({
      label: "only the first 2,000 paths",
      explanation: "Paths after the first 2,000, and everything under a key longer than 1 KiB, are left out.",
    });
  }
  return notes;
}
