import { api, unwrap } from "@/lib/api/client";
import type { JobInfo } from "@/lib/api/types";

/** This backend run's jobs, newest first; `timeoutMs` bounds the wait (the quit guard must never hang on it). */
export async function listJobs(timeoutMs?: number): Promise<JobInfo[]> {
  const signal = timeoutMs === undefined ? undefined : AbortSignal.timeout(timeoutMs);
  return unwrap(await (await api()).GET("/data/jobs", { signal }));
}
