import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DumpCommand, JobInfo, JobOutcomeEvent } from "@/lib/api/types";
import {
  endingKey,
  endingNotice,
  endingTeller,
  endTimeKnown,
  SETTLE_MS,
  shownElapsed,
  TOAST_KINDS,
  type EndingContext,
} from "./jobEndings";
import { jobReducer, listedJob, openingJob, type JobEvent, type JobState } from "./jobState";
import type { JobRecord } from "./jobsStore";

/* When a job ends (spec ⑥ R-06-15, V11): the notification and the toast, each ending told once, a guessed ending only
   if the backend's word does not replace it at once, and the time beside a job in the Jobs list only when it is known. */

const dump: DumpCommand = {
  type: "dump",
  request_id: "r1",
  path: "/tmp/big.sql",
  scope: { kind: "tables", schema_name: "public", names: ["big"] },
  content: "both",
  objects: { views: true, sequences: true, types: true, routines: true, triggers: true },
  drop_first: false,
};
const outcome = (fields: Partial<JobOutcomeEvent> = {}): JobOutcomeEvent => ({
  type: "job_outcome",
  request_id: "r1",
  job: "dump",
  outcome: "completed",
  cancelled: false,
  rows: 10,
  bytes: 2048,
  statements: 0,
  path: "/tmp/big.sql",
  objects: [{ kind: "table", name: "public.big", rows: 10 }],
  errors: [],
  error_count: 0,
  warnings: [],
  ...fields,
});
const info = (fields: Partial<JobInfo> = {}): JobInfo => ({
  session_id: "s1",
  request_id: "r1",
  job: "dump",
  title: "Dump public.big of dev pg to big.sql",
  profile_id: "00000000-0000-0000-0000-000000000001",
  started_at: new Date(0).toISOString(),
  ...fields,
});

/** A job started at 0 and sent at 1, then `events` (each at its own time). */
const fold = (events: JobEvent[], from: JobState = openingJob(0)): JobState =>
  [{ type: "sent", at: 1 } as JobEvent, ...events].reduce(jobReducer, from);
const ended = (at: number, fields: Partial<JobOutcomeEvent> = {}): JobEvent => ({
  type: "server",
  event: outcome(fields),
  at,
});
const record = (state: JobState, fields: Partial<JobRecord> = {}): JobRecord => ({
  requestId: "r1",
  sessionId: "s1",
  kind: "dump",
  profileId: "p1",
  sourceProfileId: null,
  database: null,
  title: "Dump public.big of dev pg to big.sql",
  command: dump,
  entry: null,
  tables: null,
  copy: null,
  state,
  ...fields,
});
const away: EndingContext = { shown: null, focused: false, thresholdS: 15 };

describe("the time beside a job in the Jobs list", () => {
  it("counts while it runs, and says how long it ran once this window saw it end", () => {
    expect(shownElapsed(fold([]), 42_000)).toBe(42_000);
    expect(shownElapsed(fold([ended(65_000)]), 99_000)).toBe(65_000);
    // an ending of this window's own is its own time
    expect(shownElapsed(fold([{ type: "session_gone", at: 30_000 }]), 99_000)).toBe(30_000);
  });

  it("says nothing for an outcome read from the backend's list: its end time is when the list came", () => {
    const listed = listedJob(info({ outcome: outcome() }), 500_000);
    expect([endTimeKnown(listed), shownElapsed(listed, 600_000)]).toEqual([false, null]);
    // a loss the list replaced with the outcome
    const replaced = fold([{ type: "session_gone", at: 30_000 }, { type: "listed", info: info({ outcome: outcome() }), at: 31_000 }]);
    expect([replaced.phase, replaced.outcome?.outcome, shownElapsed(replaced, 40_000)]).toEqual(["ended", "completed", null]);
  });

  it("says nothing for a job guessed not listed: its time is the sync's that missed it (fix M2)", () => {
    const lost = fold([{ type: "not_listed", at: 70_000 }]);
    expect([endTimeKnown(lost), shownElapsed(lost, 80_000)]).toEqual([false, null]);
    expect(endingNotice(record(lost), away)).toEqual({
      notification: null,
      toast: { kind: "error", text: "The backend has no record of this job: it restarted, or never got the command — Dump public.big of dev pg to big.sql" },
    });
  });
});

describe("what is said as a job ends", () => {
  it("toasts the outcome and the job with the tone's kind, and notifies a long one in the background", () => {
    const notice = endingNotice(record(fold([ended(65_000)])), away);
    expect(notice.toast).toEqual({ kind: "success", text: "Dumped to big.sql — Dump public.big of dev pg to big.sql" });
    expect(notice.notification).toEqual({
      title: "Dump finished after 1 min 05 s",
      body: "Dumped to big.sql · Dump public.big of dev pg to big.sql",
    });
  });

  it("maps each tone to a toast kind", () => {
    const kind = (events: JobEvent[]) => endingNotice(record(fold(events)), away).toast?.kind;
    expect(kind([ended(5, { warnings: ["Not included: 1 view"] })])).toBe("warning");
    expect(kind([ended(5, { outcome: "failed" })])).toBe("error");
    expect(kind([{ type: "session_gone", at: 5 }])).toBe("error");
    // muted: only cancelled and never-sent jobs are, and those are not news
    expect(TOAST_KINDS.muted).toBe("info");
  });

  it("does not toast a job whose view is open, one that ended cancelled, or one never sent", () => {
    expect(endingNotice(record(fold([ended(5)])), { ...away, shown: "r1" }).toast).toBeNull();
    const cancelled = fold([{ type: "cancel_sent", at: 3 }, ended(65_000, { outcome: "failed", cancelled: true })]);
    expect(endingNotice(record(cancelled), away)).toEqual({ notification: null, toast: null });
    // cancelled in another window: the outcome says so, and someone asked for it
    expect(endingNotice(record(fold([ended(65_000, { outcome: "failed", cancelled: true })])), away)).toEqual({ notification: null, toast: null });
    const notSent = [{ type: "not_sent", at: 5 } as JobEvent].reduce(jobReducer, openingJob(0));
    expect(endingNotice(record(notSent), away)).toEqual({ notification: null, toast: null });
  });

  it("tells a job whose Cancel lost the race: it ended otherwise first (fix M3)", () => {
    const raced = record(fold([{ type: "cancel_sent", at: 64_000 }, ended(65_000)]));
    const notice = endingNotice(raced, away);
    expect([notice.toast?.kind, notice.notification?.title]).toEqual(["success", "Dump finished after 1 min 05 s"]);
    const lost = record(fold([{ type: "cancel_sent", at: 64_000 }, { type: "session_gone", at: 65_000 }]));
    expect(endingNotice(lost, away).toast?.kind).toBe("error");
  });

  it("toasts a job that could not connect, with its view closed, and does not notify it (fix M4)", () => {
    const failed = [{ type: "open_failed", error: new Error("password"), at: 65_000 } as JobEvent].reduce(jobReducer, openingJob(0));
    expect(endingNotice(record(failed), away)).toEqual({
      notification: null,
      toast: { kind: "error", text: "Could not connect: nothing was sent — Dump public.big of dev pg to big.sql" },
    });
    expect(endingNotice(record(failed), { ...away, shown: "r1" }).toast).toBeNull();
  });

  it("notifies only in the background, after the threshold, and only for an end this window saw", () => {
    const long = record(fold([ended(65_000)]));
    expect(endingNotice(long, { ...away, focused: true }).notification).toBeNull();
    expect(endingNotice(long, { ...away, thresholdS: 0 }).notification).toBeNull();
    expect(endingNotice(record(fold([ended(14_000)])), away).notification).toBeNull();
    const replaced = fold([{ type: "session_gone", at: 65_000 }, { type: "listed", info: info({ outcome: outcome() }), at: 66_000 }]);
    const notice = endingNotice(record(replaced), away);
    expect([notice.notification, notice.toast?.kind]).toEqual([null, "success"]);
  });

  it("says nothing for a job that runs", () => {
    expect(endingNotice(record(fold([])), away)).toEqual({ notification: null, toast: null });
  });
});

describe("each ending told once", () => {
  let store: Record<string, JobRecord>;
  let told: JobRecord[];
  const teller = () => endingTeller((requestId) => store[requestId], (r) => told.push(r));
  const set = (state: JobState) => (store.r1 = record(state));

  beforeEach(() => {
    vi.useFakeTimers();
    store = {};
    told = [];
  });
  afterEach(() => vi.useRealTimers());

  it("tells the backend's outcome at once, and the same ending only once", () => {
    const t = teller();
    t.ended(set(fold([ended(5)])));
    t.ended(store.r1);
    expect(told.map((r) => r.state.outcome?.outcome)).toEqual(["completed"]);
  });

  it("waits with a guessed ending: the backend's word that replaces it is told instead (one toast, no loss)", () => {
    const t = teller();
    const gone = fold([{ type: "session_gone", at: 5 }]);
    t.ended(set(gone));
    vi.advanceTimersByTime(SETTLE_MS - 1);
    expect(told).toEqual([]);
    t.ended(set(jobReducer(gone, { type: "listed", info: info({ outcome: outcome() }), at: 6 })));
    vi.advanceTimersByTime(SETTLE_MS);
    expect(told.map((r) => [r.state.loss, r.state.outcome?.outcome])).toEqual([[null, "completed"]]);
  });

  it("tells a guessed ending that stands, and later the backend's word that replaces it", () => {
    const t = teller();
    const lost = fold([{ type: "not_listed", at: 11_000 }]);
    t.ended(set(lost));
    vi.advanceTimersByTime(SETTLE_MS);
    // its socket spoke after all, and the job ended (the store tells it again)
    t.ended(set(jobReducer(lost, ended(60_000))));
    expect(told.map((r) => r.state.loss ?? r.state.outcome?.outcome)).toEqual(["not_listed", "completed"]);
  });

  it("does not tell a guessed ending the job no longer has: running again, or forgotten", () => {
    const t = teller();
    const lost = fold([{ type: "not_listed", at: 11_000 }]);
    t.ended(set(lost));
    set(jobReducer(lost, { type: "server", event: { type: "job_progress", request_id: "r1", stage: "data", rows: 1, bytes: 1, statements: 0, errors: 0 }, at: 11_100 }));
    vi.advanceTimersByTime(SETTLE_MS);
    expect(told).toEqual([]);
    t.ended(set(lost));
    delete store.r1; // Clear finished
    vi.advanceTimersByTime(SETTLE_MS);
    expect(told).toEqual([]);
  });

  it("tells a job guessed lost twice once, and a retried attempt's ending again", () => {
    const t = teller();
    const lost = fold([{ type: "not_listed", at: 11_000 }]);
    t.ended(set(lost));
    vi.advanceTimersByTime(SETTLE_MS);
    const revived = jobReducer(lost, { type: "listed", info: info(), at: 12_000 });
    t.ended(set(jobReducer(revived, { type: "not_listed", at: 30_000 })));
    vi.advanceTimersByTime(SETTLE_MS);
    expect(told).toHaveLength(1);

    const failed = [{ type: "open_failed", error: new Error("password"), at: 5 } as JobEvent].reduce(jobReducer, openingJob(0));
    t.ended(set(failed));
    const retried = [{ type: "retry", at: 100 } as JobEvent, { type: "open_failed", error: new Error("password"), at: 105 } as JobEvent].reduce(jobReducer, failed);
    t.ended(set(retried));
    expect(told.map((r) => [r.state.loss, r.state.startedAt])).toEqual([["not_listed", 0], ["open_failed", 0], ["open_failed", 100]]);
  });

  it("drops what waits once stopped", () => {
    const t = teller();
    t.ended(set(fold([{ type: "session_gone", at: 5 }])));
    t.stop();
    vi.advanceTimersByTime(SETTLE_MS);
    expect(told).toEqual([]);
  });

  it("keys an ending by its attempt and how it ended", () => {
    expect(endingKey(fold([]))).toBeNull();
    expect(endingKey(fold([ended(5, { outcome: "failed", cancelled: true })]))).toBe("0|outcome:failed:cancelled");
    expect(endingKey(fold([{ type: "session_gone", at: 5 }]))).toBe("0|loss:session_gone");
  });
});
