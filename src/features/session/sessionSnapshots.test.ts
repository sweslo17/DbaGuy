import { describe, expect, it } from "vitest";
import type { ServerEvent } from "@/lib/api/types";
import { applyEvent, newExecution, type ExecutionState } from "./execution";
import type { SessionSnapshot } from "./SessionController";
import { onlyApplyProgress, shownSnapshot } from "./sessionSnapshots";

/*
 * A tab is not rendered once per statement of a grid-edit apply (05b Task 10 review): only an apply's `affected` is
 * skipped, never its outcome, its end, a message or anything else of the session.
 */

const fold = (execution: ExecutionState, ...events: object[]) =>
  events.reduce<ExecutionState>(
    (state, event) => applyEvent(state, { request_id: state.requestId, ...event } as ServerEvent),
    execution,
  );
const snapshotWith = (executions: Record<string, ExecutionState>, base: Partial<SessionSnapshot> = {}): SessionSnapshot => ({
  phase: "open",
  info: null,
  error: null,
  state: null,
  executions,
  sessionMessages: [],
  ...base,
});
/** The snapshot after `events` of the apply, everything else as in `snapshot`. */
const after = (snapshot: SessionSnapshot, ...events: object[]): SessionSnapshot => ({
  ...snapshot,
  executions: { ...snapshot.executions, apply: fold(snapshot.executions.apply, ...events) },
});

const browse = fold(newExecution("browse", "execute", "SELECT 1"), { type: "result_set", index: 0, presentation: "grid", columns: [] });
const applying = fold(newExecution("apply", "edits", ""), { type: "started" });
const start = snapshotWith({ browse, apply: applying });
const affected = (index: number) => ({ type: "affected", index, rows: 1 });

describe("an apply's progress alone", () => {
  it("is skipped: one more statement counted, nothing else changed", () => {
    expect(onlyApplyProgress(start, after(start, affected(0)))).toBe(true);
    expect(onlyApplyProgress(start, after(start, affected(0), affected(1), affected(2)))).toBe(true);
  });

  it("is not skipped once the apply says anything else", () => {
    const outcome = { type: "edit_outcome", outcome: "committed", applied: ["u:1"], already_gone: [] };
    const finished = { type: "finished", elapsed_ms: 1, in_transaction: false, cancelled: false, timed_out: false, skipped: false };
    expect(onlyApplyProgress(start, after(start, affected(0), outcome))).toBe(false);
    expect(onlyApplyProgress(start, after(start, affected(0), finished))).toBe(false);
    expect(onlyApplyProgress(start, after(start, affected(0), { type: "message", severity: "warning", text: "Press Cancel again…" }))).toBe(false);
    expect(onlyApplyProgress(start, after(start, affected(0), { type: "error", message: "m", code: "edit_conflict" }))).toBe(false);
    expect(onlyApplyProgress(start, after(start, { type: "started" }))).toBe(false); // a new object without progress
  });

  it("is not skipped when anything else of the session changed", () => {
    const progressed = after(start, affected(0));
    expect(onlyApplyProgress(start, { ...progressed, phase: "reconnecting" })).toBe(false);
    expect(onlyApplyProgress(start, { ...progressed, state: { autocommit: true, inTransaction: false, transactionFailed: false, transactionStartedAt: null, idleSeconds: null } })).toBe(false);
    expect(onlyApplyProgress(start, { ...progressed, sessionMessages: [{ id: 1, severity: "info", text: "idle", at: 0 }] })).toBe(false);
    expect(onlyApplyProgress(start, { ...progressed, error: new Error("gone") })).toBe(false);
    const rows = { ...progressed, executions: { ...progressed.executions, browse: fold(browse, { type: "rows", index: 0, rows: [[1]] }) } };
    expect(onlyApplyProgress(start, rows)).toBe(false); // the browse's rows
    const added = { ...progressed, executions: { ...progressed.executions, other: newExecution("other", "execute", "SELECT 2") } };
    expect(onlyApplyProgress(start, added)).toBe(false);
    const forgotten = { ...progressed, executions: { apply: progressed.executions.apply } };
    expect(onlyApplyProgress(start, forgotten)).toBe(false); // the browse was forgotten
    const { browse: _gone, ...rest } = progressed.executions;
    expect(onlyApplyProgress(start, { ...progressed, executions: { ...rest, other: browse } })).toBe(false); // swapped
  });

  it("is never a statement of any other request, nor a nothing-changed", () => {
    const running = fold(newExecution("apply", "execute", "UPDATE t SET a = 1"), { type: "started" });
    const query = snapshotWith({ apply: running });
    expect(onlyApplyProgress(query, after(query, affected(0)))).toBe(false);
    expect(onlyApplyProgress(start, start)).toBe(false);
    const done = snapshotWith({ apply: { ...applying, status: "done" } });
    expect(onlyApplyProgress(done, after(done, affected(0)))).toBe(false);
    // Running again after it was not (a constructed case: an apply never pauses) is a change too.
    const resumed = snapshotWith({ apply: { ...applying, status: "paused" } });
    expect(onlyApplyProgress(resumed, after({ ...resumed, executions: { apply: applying } }, affected(0)))).toBe(false);
  });
});

describe("the snapshot a tab renders", () => {
  it("keeps the one it has through an apply's progress, and takes the next change", () => {
    const one = after(start, affected(0));
    expect(shownSnapshot(null, start)).toBe(start);
    expect(shownSnapshot(start, one)).toBe(start);
    const two = after(one, affected(1));
    expect(shownSnapshot(start, two)).toBe(start); // compared with what was kept, not with the last event's
    const ended = after(two, { type: "edit_outcome", outcome: "committed", applied: [], already_gone: [] });
    expect(shownSnapshot(start, ended)).toBe(ended);
    expect(shownSnapshot(ended, ended)).toBe(ended); // asked twice for the same state: the same answer
  });
});
