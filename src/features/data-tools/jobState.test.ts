import { describe, expect, it } from "vitest";
import type { JobInfo, JobOutcomeEvent, JobProgressEvent, ServerEvent } from "@/lib/api/types";
import { jobReducer, listedJob, MAX_JOB_MESSAGES, neverReachedBackend, openingJob, type JobEvent, type JobState } from "./jobState";

/* A job's state (spec ⑥ R-06-15, D-06-10): the outcome ends it, with or without `finished`; a loss only when the
   backend can no longer say, and the backend's list outranks it. */

const RID = "r1";
const progress = (fields: Partial<JobProgressEvent> = {}): JobProgressEvent => ({
  type: "job_progress",
  request_id: RID,
  stage: "statements",
  rows: 0,
  bytes: 100,
  statements: 1,
  errors: 0,
  ...fields,
});
const outcome = (fields: Partial<JobOutcomeEvent> = {}): JobOutcomeEvent => ({
  type: "job_outcome",
  request_id: RID,
  job: "run_file",
  outcome: "completed",
  cancelled: false,
  rows: 0,
  bytes: 0,
  statements: 3,
  objects: [],
  errors: [],
  error_count: 0,
  warnings: [],
  ...fields,
});
const finished: ServerEvent = {
  type: "finished",
  request_id: RID,
  elapsed_ms: 5,
  in_transaction: false,
  transaction_failed: false,
  cancelled: false,
  timed_out: false,
  skipped: false,
};
const server = (event: ServerEvent, at = 10): JobEvent => ({ type: "server", event, at });
const run = (events: JobEvent[], from: JobState = openingJob(0)) => events.reduce(jobReducer, from);
const sent: JobEvent = { type: "sent", at: 1 };
const info = (fields: Partial<JobInfo> = {}): JobInfo => ({
  session_id: "s1",
  request_id: RID,
  job: "run_file",
  title: "Run x.sql on Local PG",
  profile_id: "00000000-0000-0000-0000-000000000001",
  started_at: "2026-10-23T01:02:03Z",
  ...fields,
});

describe("a job's state", () => {
  it("runs from started and progress, and ends at its outcome with or without the finished after it", () => {
    const live = run([sent, server({ type: "started", request_id: RID }), server(progress())]);
    expect([live.phase, live.progress?.bytes]).toEqual(["running", 100]);
    const done = run([server(outcome({ statements: 7 }), 20)], live);
    expect([done.phase, done.outcome?.statements, done.endedAt, done.loss]).toEqual(["ended", 7, 20, null]);
    const after = run([server(finished)], done);
    expect(after).toBe(done);
  });

  it("ends at a replayed outcome alone: a late client gets no finished (06a carry)", () => {
    const replay = run([sent, server(progress({ statements: 9 })), server(outcome({ outcome: "stopped" }))]);
    expect([replay.phase, replay.outcome?.outcome, replay.progress?.statements]).toEqual(["ended", "stopped", 9]);
  });

  it("keeps the outcome's error, and the live error before it", () => {
    const failed = outcome({ outcome: "rolled_back", error: { line: 4, statement: 2, code: "42P01", message: "no table" } });
    const state = run([sent, server({ type: "error", request_id: RID, message: "live", code: "42P01" }), server(failed)]);
    expect(state.error).toEqual({ message: "no table", code: "42P01" });
    const onlyLive = run([sent, server({ type: "error", request_id: RID, message: "live", code: null }), server(outcome())]);
    expect(onlyLive.error).toEqual({ message: "live", code: null });
  });

  it("keeps at most the backend's number of messages", () => {
    const message = server({ type: "message", request_id: RID, severity: "warning", text: "w" });
    const state = run([sent, ...Array.from({ length: MAX_JOB_MESSAGES + 5 }, () => message)]);
    expect(state.messages).toHaveLength(MAX_JOB_MESSAGES);
  });

  it("calls a request that finished without an outcome unknown, never completed", () => {
    const state = run([sent, server(finished)]);
    expect([state.phase, state.loss, state.outcome]).toEqual(["ended", "no_outcome", null]);
  });

  it("tells a session gone after the command left from one gone before it", () => {
    expect(run([sent, { type: "session_gone", at: 5 }]).loss).toBe("session_gone");
    expect(run([{ type: "session_gone", at: 5 }]).loss).toBe("not_sent");
    expect(run([sent, { type: "not_sent", at: 5 }]).phase).toBe("opening"); // sent: never "not sent"
  });

  it("starts again only what never reached the backend", () => {
    const failed = run([{ type: "open_failed", error: new Error("password"), at: 3 }]);
    expect([failed.loss, neverReachedBackend(failed)]).toEqual(["open_failed", true]);
    expect(run([{ type: "retry", at: 9 }], failed)).toEqual(openingJob(9));
    const gone = run([sent, { type: "session_gone", at: 5 }]);
    expect(neverReachedBackend(gone)).toBe(false);
    expect(run([{ type: "retry", at: 9 }], gone)).toBe(gone);
  });

  it("lets the backend's listed outcome replace a loss, never an outcome", () => {
    const gone = run([sent, { type: "session_gone", at: 5 }]);
    const listed = run([{ type: "listed", info: info({ outcome: outcome({ outcome: "unknown", warnings: ["Its session closed before it reported an outcome"] }) }), at: 6 }], gone);
    expect([listed.loss, listed.outcome?.outcome]).toEqual([null, "unknown"]);
    const done = run([sent, server(outcome({ outcome: "completed" }))]);
    expect(run([{ type: "listed", info: info({ outcome: outcome({ outcome: "unknown" }) }), at: 6 }], done)).toBe(done);
  });

  it("takes the list's progress only when it has none: a live one is newer", () => {
    const live = run([sent, server(progress({ statements: 50 }))]);
    expect(run([{ type: "listed", info: info({ progress: progress({ statements: 10 }) }), at: 6 }], live)).toBe(live);
    const fresh = run([sent]);
    expect(run([{ type: "listed", info: info({ progress: progress({ statements: 10 }) }), at: 6 }], fresh).progress?.statements).toBe(10);
  });

  it("builds a listed job as running or ended, from its start time", () => {
    const running = listedJob(info({ progress: progress() }), 99);
    expect([running.phase, running.sent, running.startedAt]).toEqual(["running", true, Date.parse("2026-10-23T01:02:03Z")]);
    const done = listedJob(info({ outcome: outcome({ outcome: "failed", job: "dump" }) }), 99);
    expect([done.phase, done.outcome?.outcome]).toEqual(["ended", "failed"]);
  });

  it("calls a job not listed only once it was sent", () => {
    expect(run([{ type: "not_listed", at: 5 }]).phase).toBe("opening");
    expect(run([sent, { type: "not_listed", at: 5 }]).loss).toBe("not_listed");
  });

  it("follows the socket while running, and records a cancel once", () => {
    const state = run([sent, { type: "socket", open: false }, { type: "cancel_sent", at: 7 }]);
    expect([state.reconnecting, state.cancelRequestedAt]).toEqual([true, 7]);
    expect(run([{ type: "socket", open: true }], state).reconnecting).toBe(false);
  });

  it("stays the same state for a socket event that changes nothing: a backoff re-renders nothing", () => {
    const reconnecting = run([sent, { type: "socket", open: false }]);
    expect(jobReducer(reconnecting, { type: "socket", open: false })).toBe(reconnecting);
    const open = run([sent, { type: "socket", open: true }]);
    expect(jobReducer(open, { type: "socket", open: true })).toBe(open);
  });

  it("keeps the first Cancel's time when a second is sent: the dialog escalates from it", () => {
    const state = run([sent, { type: "cancel_sent", at: 7 }, { type: "cancel_sent", at: 12 }]);
    expect(state.cancelRequestedAt).toBe(7);
    const done = run([server(outcome(), 20), { type: "cancel_sent", at: 25 }], state);
    expect(done.cancelRequestedAt).toBe(7); // an ended job takes no more
  });
});

describe("when the command was sent (pre-flight M2)", () => {
  it("is the moment it left, not when its session began to open", () => {
    const opening = openingJob(0);
    expect([opening.startedAt, opening.sentAt]).toEqual([0, null]);
    const left = run([{ type: "sent", at: 11_000 }], opening);
    expect([left.startedAt, left.sentAt]).toEqual([0, 11_000]);
    expect(run([{ type: "sent", at: 12_000 }], left).sentAt).toBe(11_000); // once
  });

  it("is a listed job's start: it was sent then", () => {
    const listed = listedJob(info({ progress: progress() }), 99);
    expect(listed.sentAt).toBe(Date.parse("2026-10-23T01:02:03Z"));
  });

  it("lets a job ended as not listed run again when the list has it running", () => {
    const lost = run([sent, { type: "not_listed", at: 5 }]);
    const back = run([{ type: "listed", info: info({ progress: progress({ statements: 10 }) }), at: 6 }], lost);
    expect([back.phase, back.loss, back.endedAt, back.progress?.statements]).toEqual(["running", null, null, 10]);
    const bare = run([{ type: "listed", info: info(), at: 6 }], lost);
    expect([bare.phase, bare.loss, bare.endedAt]).toEqual(["running", null, null]);
  });

  it("keeps a live progress over the list's when it runs again", () => {
    const lost = run([sent, server(progress({ statements: 50 })), { type: "not_listed", at: 5 }]);
    const back = run([{ type: "listed", info: info({ progress: progress({ statements: 10 }) }), at: 6 }], lost);
    expect([back.phase, back.progress?.statements]).toEqual(["running", 50]);
  });

  it("still lets the list's outcome win over a not_listed", () => {
    const lost = run([sent, { type: "not_listed", at: 5 }]);
    const listed = run([{ type: "listed", info: info({ outcome: outcome({ outcome: "failed", job: "dump" }) }), at: 6 }], lost);
    expect([listed.phase, listed.loss, listed.outcome?.outcome]).toEqual(["ended", null, "failed"]);
  });

  it("takes what the socket says over a not_listed: the backend's word outranks the guess", () => {
    const lost = run([sent, { type: "not_listed", at: 5 }]);
    const live = run([server(progress({ statements: 4 }), 8)], lost);
    expect([live.phase, live.loss, live.endedAt, live.progress?.statements]).toEqual(["running", null, null, 4]);
    const done = run([server(outcome({ statements: 9 }), 20)], lost);
    expect([done.phase, done.loss, done.outcome?.statements, done.endedAt]).toEqual(["ended", null, 9, 20]);
    expect(run([server({ type: "session_state", autocommit: true, in_transaction: false, transaction_failed: false }, 8)], lost)).toBe(lost);
  });

  it("does not undo any other loss from the socket", () => {
    const gone = run([sent, { type: "session_gone", at: 5 }]);
    expect(run([server(outcome(), 8)], gone)).toBe(gone);
  });
});
