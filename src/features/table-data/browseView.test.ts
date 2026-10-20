import { describe, expect, it } from "vitest";
import type { ServerEvent } from "@/lib/api/types";
import { applyEvent, interruptExecution, newExecution, type ExecutionState } from "../session/execution";
import { browseView, endedEarly, firstPageComplete, keptNotice, REFRESH_CANCELLED } from "./browseView";

/* What the Data view does with a browse request that ends early (review of Task 6: a cancelled Refresh kept 120 rows
 * that looked like the whole table, or an empty grid; a cut page counted as delivered rows). */

const R = "r1";
const fold = (events: object[], from: ExecutionState = newExecution(R, "execute", "SELECT")): ExecutionState =>
  events.reduce<ExecutionState>((state, event) => applyEvent(state, event as ServerEvent), from);
const started = { type: "started", request_id: R };
const resultSet = { type: "result_set", request_id: R, index: 0, columns: [], presentation: "grid" };
const rows = (n: number) => ({ type: "rows", request_id: R, index: 0, rows: Array.from({ length: n }, (_, i) => [i]) });
const end = (rowCount: number, hasMore: boolean) => ({
  type: "result_set_end",
  request_id: R,
  index: 0,
  row_count: rowCount,
  has_more: hasMore,
});
const finished = (cancelled = false) => ({
  type: "finished",
  request_id: R,
  elapsed_ms: 1,
  cancelled,
  timed_out: false,
  skipped: false,
});
const error = { type: "error", request_id: R, message: "boom", code: "query_failed" };

const arriving = fold([started, resultSet, rows(120)]);
const whole = fold([started, resultSet, rows(500), end(500, true)]);
const wholeAndOver = fold([started, resultSet, rows(500), end(500, false), finished()]);
const emptyAndOver = fold([started, resultSet, end(0, false), finished()]);
const cancelledBeforeAnySet = fold([started, finished(true)]);
const cancelledMidPage = fold([started, resultSet, rows(120), finished(true)]);
const cancelledEmptySet = fold([started, resultSet, finished(true)]);
const failedMidPage = fold([started, resultSet, rows(120), error, finished()]);
const failedBeforeAnySet = fold([started, error, finished()]);
const failingStillRunning = fold([started, resultSet, rows(120), error]); // the error came; `finished` has not
const interruptedMidPage = interruptExecution(arriving);
const cancelledWhilePaused = fold([finished(true)], whole); // the page was whole; a later page is missing
const failedAfterItsPage = fold([error, finished()], whole);
const interruptedWhilePaused = interruptExecution(whole);
const interruptedPausedEmpty = interruptExecution(fold([started, resultSet, end(0, true)]));

describe("a request that ended before its first page was complete", () => {
  it("is one that was cancelled, failed or interrupted with no page end, whatever rows arrived", () => {
    for (const execution of [
      cancelledBeforeAnySet,
      cancelledMidPage,
      cancelledEmptySet,
      failedMidPage,
      failedBeforeAnySet,
      interruptedMidPage,
    ]) {
      expect(endedEarly(execution)).toBe(true);
      expect(firstPageComplete(execution)).toBe(false);
    }
    expect(endedEarly(fold([started, resultSet, rows(3), { ...finished(), timed_out: true }]))).toBe(true);
  });

  it("is not one that is still running, finished, or had its page end before the request did", () => {
    for (const execution of [
      null,
      newExecution(R, "execute", "SELECT"),
      arriving,
      whole,
      wholeAndOver,
      emptyAndOver,
      cancelledWhilePaused,
      failedAfterItsPage,
      interruptedWhilePaused,
      interruptedPausedEmpty,
      failingStillRunning, // not over yet: the view waits for `finished`
    ]) {
      expect(endedEarly(execution)).toBe(false);
    }
  });

  it("has a whole first page when the page ended, paused, or the request finished with nothing wrong", () => {
    for (const execution of [whole, wholeAndOver, emptyAndOver, cancelledWhilePaused, failedAfterItsPage]) {
      expect(firstPageComplete(execution)).toBe(true);
    }
    expect(firstPageComplete(arriving)).toBe(false);
    expect(firstPageComplete(null)).toBe(false);
  });
});

describe("browseView", () => {
  it("shows nothing while the first page arrives, the whole page once it is in, whatever the kind of request", () => {
    for (const rerun of [false, true]) {
      expect(browseView(null, rerun)).toEqual({ set: null, keepPrevious: false, partial: false });
      expect(browseView(arriving, rerun)).toEqual({ set: null, keepPrevious: false, partial: false });
      expect(browseView(whole, rerun).set).toBe(whole.resultSets[0]);
      expect(browseView(wholeAndOver, rerun)).toMatchObject({ keepPrevious: false, partial: false });
      expect(browseView(emptyAndOver, rerun).set).toBe(emptyAndOver.resultSets[0]);
      expect(browseView(cancelledWhilePaused, rerun)).toMatchObject({ keepPrevious: false, partial: false });
    }
  });

  it("keeps the rows from before when a Refresh ends early, and shows none of the fragment", () => {
    for (const execution of [
      cancelledBeforeAnySet,
      cancelledMidPage,
      cancelledEmptySet,
      failedMidPage,
      failedBeforeAnySet,
      interruptedMidPage,
    ]) {
      expect(browseView(execution, true)).toEqual({ set: null, keepPrevious: true, partial: false });
    }
  });

  it("shows what a sort or filter that ended early got, marked partial, or nothing when it got no set", () => {
    expect(browseView(cancelledMidPage, false)).toEqual({
      set: cancelledMidPage.resultSets[0],
      keepPrevious: false,
      partial: true,
    });
    expect(browseView(failedMidPage, false)).toMatchObject({ keepPrevious: false, partial: true });
    expect(browseView(interruptedMidPage, false)).toMatchObject({ keepPrevious: false, partial: true });
    expect(browseView(cancelledBeforeAnySet, false)).toEqual({ set: null, keepPrevious: false, partial: false });
    expect(browseView(failedBeforeAnySet, false)).toEqual({ set: null, keepPrevious: false, partial: false });
  });
});

describe("keptNotice", () => {
  it("says a cancelled Refresh kept the rows from before, only while rows are on screen", () => {
    const view = browseView(cancelledMidPage, true);
    expect(keptNotice(cancelledMidPage, view, true)).toBe(REFRESH_CANCELLED);
    expect(keptNotice(cancelledMidPage, view, false)).toBeNull(); // nothing was held: the placeholder says Cancelled
    expect(keptNotice(failedMidPage, browseView(failedMidPage, true), true)).toBeNull(); // the error banner says why
    expect(keptNotice(cancelledMidPage, browseView(cancelledMidPage, false), true)).toBeNull(); // a sort or filter
    expect(keptNotice(whole, browseView(whole, true), true)).toBeNull();
  });
});
