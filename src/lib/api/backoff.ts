/*
 * How long a socket waits before it tries again: a session's WebSocket after it closed (`SessionSocket`), and a job's
 * runner whose first connect failed (`jobRunner.ts`). Pure, so the runner's tests need no backend.
 */

const BACKOFF_BASE_MS = 250;
const BACKOFF_MAX_MS = 5000;

/** The wait before attempt `attempt + 1` (0 for the first retry): doubling from 250 ms, at most 5 s. */
export function reconnectDelay(attempt: number): number {
  return Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** attempt);
}
