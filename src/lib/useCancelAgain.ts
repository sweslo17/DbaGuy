import { useEffect, useState } from "react";

/**
 * Cancel comes back this long after a click, or as soon as the export or job gives a new warning (SQL Server: that the
 * cancel cannot stop it safely, and what another Cancel does): within it a second Cancel would be read as a double
 * click, and on SQL Server a second Cancel is the one that forces the connection closed (03b's escalation rule).
 */
export const CANCEL_AGAIN_MS = 1_000;

/** A Cancel sent: when, and how many warnings there were then. */
export interface CancelSent {
  at: number;
  warnings: number;
}

/**
 * The Cancel a view opens with: one sent less than CANCEL_AGAIN_MS before `now` (a job's view closed and opened again
 * right after its Cancel) is still being sent, so Cancel does not come back early.
 */
export function seededCancel(sentAt: number | null, warnings: number, now: number): CancelSent | null {
  return sentAt !== null && now >= sentAt && now - sentAt < CANCEL_AGAIN_MS ? { at: sentAt, warnings } : null;
}

/**
 * Cancel's "Cancelling…" (the export and a job's view share it): `cancelling` from `sent()` until CANCEL_AGAIN_MS later
 * or until `warnings` (the count so far) grows; `reset()` ends it at once (a new run). `sentAt`: a Cancel sent before
 * the view mounted (a job's `cancelRequestedAt`).
 */
export function useCancelAgain(warnings: number, sentAt: number | null = null) {
  const [cancel, setCancel] = useState<CancelSent | null>(() => seededCancel(sentAt, warnings, Date.now()));
  useEffect(() => {
    if (!cancel) return;
    const timer = window.setTimeout(() => setCancel(null), Math.max(0, cancel.at + CANCEL_AGAIN_MS - Date.now()));
    return () => window.clearTimeout(timer);
  }, [cancel]);
  return {
    cancelling: cancel !== null && cancel.warnings === warnings,
    sent: () => setCancel({ at: Date.now(), warnings }),
    reset: () => setCancel(null),
  };
}
