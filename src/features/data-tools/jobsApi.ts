import { api, ApiError, unwrap } from "@/lib/api/client";
import type {
  DumpPlan,
  DumpPlanRequest,
  FileExcerpt,
  FileInspection,
  JobInfo,
  SessionInfo,
  TransferPlan,
  TransferRequest,
} from "@/lib/api/types";

/*
 * The data tools' REST calls (spec ⑥ §3.2, 06a appendix). A job itself is a WebSocket command on a session of its
 * own (`jobRunner.ts`); these open and close that session, list and cancel jobs, and read what a dialog shows first.
 * Plain calls: none of them is a `useQuery` that the app's QueryClient would retry.
 */

/** A session of the job's own (D-06-10): never a tab's, never reused. */
export async function openJobSession(profileId: string, database: string | null): Promise<SessionInfo> {
  return unwrap(await (await api()).POST("/sessions", { body: { profile_id: profileId, database } }));
}

/** Its job ended: the session goes (DELETE waits for the worker to finish the job's History record). Gone already: fine. */
export async function closeJobSession(sessionId: string): Promise<void> {
  try {
    unwrap(await (await api()).DELETE("/sessions/{session_id}", { params: { path: { session_id: sessionId } } }));
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return;
    throw error;
  }
}

/** The WebSocket's cancel, for a job whose socket is not open: a job that has ended is left as it is. */
export async function cancelJobByRest(sessionId: string, requestId: string): Promise<void> {
  unwrap(await (await api()).POST("/data/jobs/cancel", { body: { session_id: sessionId, request_id: requestId } }));
}

/** This backend run's jobs, newest first; `timeoutMs` bounds the wait (the quit guard must never hang on it). */
export async function listJobs(timeoutMs?: number): Promise<JobInfo[]> {
  const signal = timeoutMs === undefined ? undefined : AbortSignal.timeout(timeoutMs);
  return unwrap(await (await api()).GET("/data/jobs", { signal }));
}

export async function clearFinishedJobsOnBackend(): Promise<void> {
  unwrap(await (await api()).POST("/data/jobs/clear"));
}

/** Show in Finder: only a file a completed dump of this backend run wrote (404 otherwise). */
export async function revealFile(path: string): Promise<void> {
  unwrap(await (await api()).POST("/data/reveal", { body: { path } }));
}

export async function inspectRunFile(profileId: string, path: string): Promise<FileInspection> {
  return unwrap(await (await api()).POST("/data/run-file/inspect", { body: { profile_id: profileId, path } }));
}

/** The file's lines around `line` (06b B2): where a run's error is. */
export async function fileExcerpt(path: string, line: number): Promise<FileExcerpt> {
  return unwrap(await (await api()).POST("/data/run-file/excerpt", { body: { path, line } }));
}

export async function fetchDumpPlan(body: DumpPlanRequest, signal?: AbortSignal): Promise<DumpPlan> {
  return unwrap(await (await api()).POST("/data/dump/plan", { body, signal }));
}

/** Copy data's preview (POST /data/transfer/plan): a plan id only while nothing is wrong. */
export async function fetchTransferPlan(body: TransferRequest, signal?: AbortSignal): Promise<TransferPlan> {
  return unwrap(await (await api()).POST("/data/transfer/plan", { body, signal }));
}
