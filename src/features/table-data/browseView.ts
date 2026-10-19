import { firstPageDelivered, type ExecutionState, type ResultSetState } from "../session/execution";

/*
 * What the Data view does with the browse request's result (D-05-12, A12). The grid keeps the rows it shows until the
 * new request's first page is in whole. A request that ends early (cancelled, failed, interrupted) never delivers it:
 * a Refresh then leaves the rows from before on screen, and a sort or filter shows the fragment, as partial. Pure.
 */

export const REFRESH_CANCELLED = "Refresh cancelled: showing the rows from before";

/**
 * The request ended (cancelled, timed out, failed or interrupted) before its first page was complete: what it holds is
 * a fragment, and an empty set may only mean that nothing arrived. A set with a row count has its page (it ended), and
 * so does one that ended paused (`partial`); a later page can still be missing, which `partial` says itself.
 */
export function endedEarly(execution: ExecutionState | null): boolean {
  if (!execution || execution.status !== "done") return false;
  if (!(execution.cancelled || execution.timedOut || execution.interrupted || execution.error)) return false;
  const set = execution.resultSets[0];
  return !set || (set.rowCount === 0 && !set.partial);
}

/** The request's first page is in whole, so its rows may replace the ones the grid shows. */
export function firstPageComplete(execution: ExecutionState | null): boolean {
  return firstPageDelivered(execution) && !endedEarly(execution);
}

export interface BrowseView {
  /** The set to show now: the whole first page, or for a sort or filter that ended early what it got; else none. */
  set: ResultSetState | null;
  /** A Refresh ended early: keep showing the rows from before (the grid holds them), they are not stale. */
  keepPrevious: boolean;
  /** `set` is a fragment of the first page (cancelled, failed or interrupted before it was complete). */
  partial: boolean;
}

/**
 * `rerun`: the request is a Refresh of the browse the grid shows (same filter and sorts). One that ends early changes
 * nothing on screen; the notice (`execution.cancelled`, the error banner) says why. A sort or filter that ends early
 * keeps what it got, as before: the fragment marked partial, or none (the placeholder says "Cancelled").
 */
export function browseView(execution: ExecutionState | null, rerun: boolean): BrowseView {
  const set = execution?.resultSets[0] ?? null;
  if (endedEarly(execution)) {
    return rerun ? { set: null, keepPrevious: true, partial: false } : { set, keepPrevious: false, partial: !!set };
  }
  return { set: firstPageDelivered(execution) ? set : null, keepPrevious: false, partial: false };
}

/** The line under the filter bar when a Refresh was cancelled and the rows from before stay; else null. */
export function keptNotice(execution: ExecutionState | null, view: BrowseView, hasRows: boolean): string | null {
  return view.keepPrevious && hasRows && execution?.cancelled ? REFRESH_CANCELLED : null;
}
