import { beforeEach, describe, expect, it } from "vitest";
import type { CopyDataCommand, JobInfo, JobOutcomeEvent, RunFileCommand } from "@/lib/api/types";
import { initialCopyForm } from "./copyForm";
import type { CopyEntry, RunEntry } from "./entryPoints";
import { FakeSocket, sessionInfo } from "./jobFakes";
import type { JobTransport } from "./jobRunner";
import { openingJob, type JobLoss } from "./jobState";
import {
  clearFinishedJobs,
  deleteBlockedMessage,
  copyReadsSession,
  hasEndedJob,
  jobList,
  MAX_JOBS_KEPT,
  NOT_LISTED_AFTER_MS,
  onJobEnded,
  reconcile,
  resetJobsForTests,
  retryJob,
  runningCount,
  runningJobs,
  runningOn,
  setJobListing,
  setJobTransport,
  startJob,
  syncJobs,
  useJobs,
  type JobRecord,
} from "./jobsStore";

/* This window's jobs (spec ⑥ D-06-10): the backend's list after a reload, a job it no longer has, a retry only of what
   never reached it, and one ending each. */

const outcome = (fields: Partial<JobOutcomeEvent> = {}): JobOutcomeEvent => ({
  type: "job_outcome", request_id: "r1", job: "run_file", outcome: "completed", cancelled: false, rows: 0, bytes: 0,
  statements: 1, objects: [], errors: [], error_count: 0, warnings: [], ...fields,
});
const info = (fields: Partial<JobInfo> = {}): JobInfo => ({
  session_id: "s1", request_id: "r1", job: "run_file", title: "Run x.sql on Local PG",
  profile_id: "00000000-0000-0000-0000-000000000001", started_at: "2026-10-23T01:00:00Z", ...fields,
});
const record = (fields: Partial<JobRecord> = {}): JobRecord => ({
  requestId: "r1", sessionId: "s1", kind: "run_file", profileId: "p1", sourceProfileId: null, database: null, title: "t",
  command: null, entry: null, tables: null, copy: null, state: { ...openingJob(0), phase: "running", sent: true, sentAt: 0 }, ...fields,
});
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("reconciling with the backend's list", () => {
  it("adds what it lists that this window does not know: running ones attached, ended ones' sessions closed", () => {
    const listed = [info({ request_id: "a", session_id: "sa" }), info({ request_id: "b", session_id: "sb", outcome: outcome({ request_id: "b" }) })];
    const result = reconcile({}, listed, 100);
    expect(result.added.map((r) => [r.requestId, r.state.phase, r.command])).toEqual([["a", "running", null], ["b", "ended", null]]);
    expect([result.attach, result.close]).toEqual([[["a", "sa"]], ["sb"]]);
  });

  it("gives a known job what the list says", () => {
    const result = reconcile({ r1: record() }, [info({ outcome: outcome() })], 100);
    expect(result.events).toEqual([["r1", { type: "listed", info: info({ outcome: outcome() }), at: 100 }]]);
    expect(result.added).toEqual([]);
  });

  it("ends a running job the backend no longer has, once it was sent long enough ago", () => {
    const jobs = { r1: record({ state: { ...openingJob(0), phase: "running", sent: true, sentAt: 0 } }) };
    expect(reconcile(jobs, [], NOT_LISTED_AFTER_MS - 1).events).toEqual([]);
    expect(reconcile(jobs, [], NOT_LISTED_AFTER_MS).events).toEqual([["r1", { type: "not_listed", at: NOT_LISTED_AFTER_MS }]]);
    const opening = { r1: record({ state: openingJob(0) }) };
    expect(reconcile(opening, [], NOT_LISTED_AFTER_MS * 2).events).toEqual([]);
  });

  it("measures from when the command left, not from when its session began to open (M2)", () => {
    const slowOpen = { r1: record({ state: { ...openingJob(0), phase: "running", sent: true, sentAt: 11_000 } }) };
    expect(reconcile(slowOpen, [], 11_050).events).toEqual([]);
  });

  it("forgets the oldest ended jobs the backend no longer lists, beyond the backend's own cap (L14)", () => {
    const ended = (requestId: string, at: number, loss: JobLoss | null = null) =>
      record({ requestId, state: { ...openingJob(at), phase: "ended", sent: true, sentAt: at, loss, endedAt: at } });
    const jobs: Record<string, JobRecord> = {
      running: record({ requestId: "running", state: { ...openingJob(-3), phase: "running", sent: true, sentAt: -3 } }),
      listed: ended("listed", -2),
      lost: ended("lost", -1, "not_listed"),
    };
    for (let i = 0; i < MAX_JOBS_KEPT; i++) jobs[`j${i}`] = ended(`j${i}`, i);
    const listed = [info({ request_id: "listed", outcome: outcome({ request_id: "listed" }) })];
    expect(reconcile(jobs, listed, 300).dropped).toEqual(["j0", "j1", "j2"]);
    const { running: _r, listed: _l, lost: _o, ...atTheCap } = jobs;
    expect(reconcile(atTheCap, [], 300).dropped).toEqual([]);
  });
});

const command: RunFileCommand = { type: "run_file", request_id: "r1", path: "/x.sql", mode: "statements", on_error: "stop", from_statement: 1 };
const entry: RunEntry = { profileId: "p1", database: null, searchPath: null, queryContext: null, path: "/x.sql" };
const noListing = { list: async () => [], clearFinished: async () => undefined };

describe("starting a job", () => {
  let fail: Error | null;
  const transport: JobTransport = {
    openSession: async () => {
      if (fail) throw fail;
      return sessionInfo();
    },
    closeSession: async () => undefined,
    cancel: async () => undefined,
    socket: () => new FakeSocket(), // never opened: quiet
    now: () => 7,
  };

  beforeEach(() => {
    fail = null;
    setJobTransport(transport);
    setJobListing(noListing);
    resetJobsForTests();
  });

  it("lists it at once, and tells of its end once", async () => {
    const ends: string[] = [];
    const stop = onJobEnded((job) => ends.push(job.state.loss ?? "outcome"));
    fail = new Error("A password is needed");
    startJob({ kind: "run_file", profileId: "p1", database: null, title: "Run x.sql on Local PG", command, entry });
    expect(runningJobs(useJobs.getState()).map((job) => job.requestId)).toEqual(["r1"]);
    await flush();
    expect([useJobs.getState().jobs.r1.state.loss, ends]).toEqual(["open_failed", ["open_failed"]]);
    stop();
  });

  it("keeps a copy's source connection, its form and its tables, a retry too (06c: B13, Copy again, Copy the rest)", async () => {
    const copyEntry: CopyEntry = {
      profileId: "src", database: null, source: { kind: "tables", schema: "shop", folder: [{ kind: "schema", name: "shop" }, { kind: "folder", name: "tables" }] },
    };
    const form = { ...initialCopyForm(copyEntry, ["a", "b"]), targetProfileId: "p1", targetSchema: "app" };
    const copyCommand: CopyDataCommand = { type: "copy_data", request_id: "c1", plan_id: "plan", source_session_id: null };
    fail = new Error("A password is needed");
    startJob({
      kind: "copy_data", profileId: "p1", sourceProfileId: "src", database: null, title: "Copy 2 tables", command: copyCommand,
      entry: copyEntry, tables: ["a", "b"], copy: { form, sources: ["a", "b"] },
    });
    const started = useJobs.getState().jobs.c1;
    expect([started.kind, started.sourceProfileId, started.tables, started.copy]).toEqual(["copy_data", "src", ["a", "b"], { form, sources: ["a", "b"] }]);
    // the source's connection is as busy as the target's while it runs (its deletion waits)
    expect(runningOn(useJobs.getState(), "src").map((job) => job.requestId)).toEqual(["c1"]);
    await flush();
    fail = null;
    expect(retryJob("c1")).toBe(true);
    const again = useJobs.getState().jobs.c1;
    expect([again.sourceProfileId, again.tables, again.copy]).toEqual(["src", ["a", "b"], { form, sources: ["a", "b"] }]);
    // a run's record has none of them
    startJob({ kind: "run_file", profileId: "p1", database: null, title: "t", command: { ...command, request_id: "r2" }, entry });
    const run = useJobs.getState().jobs.r2;
    expect([run.sourceProfileId, run.tables, run.copy]).toEqual([null, null, null]);
  });

  it("starts again only a job nothing of which reached the backend", async () => {
    fail = new Error("A password is needed");
    startJob({ kind: "run_file", profileId: "p1", database: null, title: "t", command, entry });
    await flush();
    fail = null;
    expect(retryJob("r1")).toBe(true);
    expect(useJobs.getState().jobs.r1.state.phase).toBe("opening");
    await flush();
    useJobs.setState(({ jobs }) => ({ jobs: { ...jobs, r1: { ...jobs.r1, state: { ...jobs.r1.state, phase: "ended", sent: true, loss: "session_gone" } } } }));
    expect(retryJob("r1")).toBe(false);
  });
});

describe("following the backend", () => {
  let clock: number;
  let listed: JobInfo[];
  let cleared: number;
  let sockets: FakeSocket[];
  let closedSessions: string[];
  let closing: number;
  let mostClosingAtOnce: number;
  let ends: string[];
  let stopListening: () => void;
  const transport: JobTransport = {
    openSession: async () => sessionInfo(),
    closeSession: async (sessionId) => {
      mostClosingAtOnce = Math.max(mostClosingAtOnce, ++closing);
      await flush();
      closing--;
      closedSessions.push(sessionId);
    },
    cancel: async () => undefined,
    socket: (sessionId) => {
      const socket = new FakeSocket(sessionId);
      sockets.push(socket);
      return socket;
    },
    now: () => clock,
  };
  const state = (requestId = "r1") => useJobs.getState().jobs[requestId]?.state;
  const started = async () => {
    startJob({ kind: "run_file", profileId: "p1", database: null, title: "t", command, entry });
    await flush();
    sockets[0].accept();
    return sockets[0];
  };

  beforeEach(() => {
    clock = 0;
    listed = [];
    cleared = 0;
    sockets = [];
    closedSessions = [];
    closing = 0;
    mostClosingAtOnce = 0;
    ends = [];
    setJobTransport(transport);
    setJobListing({
      list: async () => listed,
      clearFinished: async () => {
        cleared++;
      },
    });
    resetJobsForTests();
    stopListening?.();
    stopListening = onJobEnded((job) => ends.push(job.state.outcome?.outcome ?? job.state.loss ?? "?"));
  });

  it("after a reload, lists the backend's jobs, only listens to the running one, closes the ended ones' sessions one at a time", async () => {
    listed = [
      info({ request_id: "a", session_id: "sa" }),
      info({ request_id: "b", session_id: "sb", outcome: outcome({ request_id: "b" }) }),
      info({ request_id: "c", session_id: "sc", outcome: outcome({ request_id: "c" }) }),
    ];
    await syncJobs();
    expect(jobList(useJobs.getState()).map((job) => job.requestId)).toEqual(["a", "b", "c"]);
    expect([sockets.map((socket) => socket.sessionId), closedSessions, mostClosingAtOnce]).toEqual([["sa"], ["sb", "sc"], 1]);
    await syncJobs();
    expect([sockets.length, closedSessions]).toEqual([1, ["sb", "sc"]]);
    sockets[0].accept();
    sockets[0].emit(outcome({ request_id: "a" }));
    await flush();
    expect([sockets[0].sent, state("a")?.outcome?.outcome, closedSessions]).toEqual([[], "completed", ["sb", "sc", "sa"]]);
  });

  it("keeps listening to a job it ended as not listed: the socket's outcome revives it, and its session closes (M2)", async () => {
    const socket = await started();
    clock = NOT_LISTED_AFTER_MS;
    await syncJobs();
    expect(state()?.loss).toBe("not_listed");
    socket.emit(outcome());
    await flush();
    expect([state()?.loss, state()?.outcome?.outcome, closedSessions, ends]).toEqual([null, "completed", ["s1"], ["not_listed", "completed"]]);
  });

  it("asks the backend's list again when a job's session is gone: its outcome replaces the loss (M2)", async () => {
    const socket = await started();
    listed = [info({ outcome: outcome() })];
    socket.status("closed", true);
    expect(state()?.loss).toBe("session_gone");
    await flush();
    expect([state()?.loss, state()?.outcome?.outcome, closedSessions, ends]).toEqual([null, "completed", [], ["session_gone", "completed"]]);
  });

  it("clears the ended jobs here and on the backend; one lost as not listed stops listening and leaves its session", async () => {
    const socket = await started();
    useJobs.setState(({ jobs, order }) => ({
      jobs: { ...jobs, r2: record({ requestId: "r2", state: openingJob(0) }), r3: record({ requestId: "r3", state: { ...openingJob(0), phase: "ended" } }) },
      order: ["r3", "r2", ...order],
    }));
    clock = NOT_LISTED_AFTER_MS;
    await syncJobs();
    expect(state()?.loss).toBe("not_listed");
    await clearFinishedJobs();
    expect([cleared, useJobs.getState().order, Object.keys(useJobs.getState().jobs)]).toEqual([1, ["r2"], ["r2"]]);
    socket.emit(outcome());
    await flush();
    expect([socket.closed, closedSessions, ends]).toEqual([true, [], ["not_listed"]]);
  });

  it("calls a job whose session went before its socket was attached not sent, and lets it start again (L2)", async () => {
    startJob({ kind: "run_file", profileId: "p1", database: null, title: "t", command, entry });
    await flush();
    sockets[0].status("open");
    sockets[0].status("closed", true);
    expect([state()?.loss, sockets[0].sent, ends]).toEqual(["not_sent", [], ["not_sent"]]);
    expect(retryJob("r1")).toBe(true);
    await flush();
    sockets[1].accept();
    expect([sockets[1].sent, state()?.sent]).toEqual([[command], true]);
  });

  it("never brings back what Clear finished forgot while a sync waited for the list: it asks again, once (L1)", async () => {
    const ended = (requestId: string) => record({ requestId, sessionId: `s-${requestId}`, state: { ...openingJob(0), phase: "ended" } });
    const listedEnded = (requestId: string) => info({ request_id: requestId, session_id: `s-${requestId}`, outcome: outcome({ request_id: requestId }) });
    const answers: ((jobs: JobInfo[]) => void)[] = [];
    const ask = () => new Promise<JobInfo[]>((resolve) => answers.push(resolve));
    setJobListing({ list: () => (answers.length === 0 ? ask() : Promise.resolve([])), clearFinished: async () => undefined });
    useJobs.setState({ jobs: { r9: ended("r9") }, order: ["r9"] });
    const sync = syncJobs();
    await clearFinishedJobs();
    answers[0]([listedEnded("r9"), listedEnded("u"), info({ request_id: "v", session_id: "s-v" })]);
    await sync;
    expect([answers.length, useJobs.getState().order, sockets.length, closedSessions]).toEqual([1, [], 0, []]);

    // Forgotten again while it asked again: that list only updates the jobs this window still has
    setJobListing({ list: ask, clearFinished: async () => undefined });
    useJobs.setState({ jobs: { r8: ended("r8"), r7: record({ requestId: "r7", state: openingJob(0) }) }, order: ["r8", "r7"] });
    const again = syncJobs();
    await clearFinishedJobs();
    answers[1]([listedEnded("r8")]);
    await flush();
    useJobs.setState(({ jobs, order }) => ({ jobs: { ...jobs, r6: ended("r6") }, order: ["r6", ...order] }));
    await clearFinishedJobs();
    answers[2]([listedEnded("r8"), listedEnded("r6"), info({ request_id: "r7", session_id: "s-r7", outcome: outcome({ request_id: "r7" }) }), listedEnded("u")]);
    await again;
    expect([useJobs.getState().order, state("r7")?.outcome?.outcome, sockets.length, closedSessions]).toEqual([["r7"], "completed", 0, []]);
  });
});

describe("the Jobs button's counts", () => {
  it("counts the running jobs (opening ones too) and knows whether one has ended", () => {
    const snapshot = (records: JobRecord[]) => ({
      jobs: Object.fromEntries(records.map((r) => [r.requestId, r])),
      order: records.map((r) => r.requestId),
    });
    const running = [record({ requestId: "a", state: openingJob(0) }), record({ requestId: "b" })];
    expect([runningCount(snapshot(running)), hasEndedJob(snapshot(running))]).toEqual([2, false]);
    const ended = record({ requestId: "c", state: { ...openingJob(0), phase: "ended", sent: true, sentAt: 0, loss: "not_listed", endedAt: 1 } });
    const some = snapshot([...running, ended]);
    expect([runningCount(some), hasEndedJob(some)]).toEqual([2, true]);
  });
});

describe("why a connection is not deleted while jobs run (06c B13)", () => {
  const onIt = record({ requestId: "a", profileId: "p1" });
  const reading = (id: string) => record({ requestId: id, kind: "copy_data", profileId: "other", sourceProfileId: "p1" });
  it("says a copy that only reads from it is reading, not running on it (Task 8 review 5)", () => {
    expect(deleteBlockedMessage([onIt], "p1")).toBe("Profile not deleted — a job is still running on it");
    expect(deleteBlockedMessage([onIt, record({ requestId: "b", profileId: "p1" })], "p1")).toBe("Profile not deleted — 2 jobs are still running on it");
    expect(deleteBlockedMessage([reading("c")], "p1")).toBe("Profile not deleted — a copy is still reading from it");
    expect(deleteBlockedMessage([reading("c"), reading("d")], "p1")).toBe("Profile not deleted — 2 copies are still reading from it");
    expect(deleteBlockedMessage([onIt, reading("c")], "p1")).toBe("Profile not deleted — 2 jobs are still running on it or reading from it");
  });
});

describe("a copy's source connection (06c B13)", () => {
  it("comes with a listed copy, and its deletion waits for the copy as the target's does", () => {
    const source = "00000000-0000-0000-0000-000000000002";
    const { added } = reconcile({}, [info({ job: "copy_data", source_profile_id: source })], 0);
    expect(added[0].sourceProfileId).toBe(source);
    const jobs = { r1: record({ kind: "copy_data", sourceProfileId: "src" }) };
    expect(runningOn({ jobs, order: ["r1"] }, "src").map((job) => job.requestId)).toEqual(["r1"]);
    expect(runningOn({ jobs, order: ["r1"] }, "p1")).toHaveLength(1);
    expect(runningOn({ jobs, order: ["r1"] }, "other")).toHaveLength(0);
  });
});

describe("a query tab's session a copy reads from (06c L-f)", () => {
  const copy = (fields: Partial<CopyDataCommand>): JobRecord["command"] => ({ type: "copy_data", request_id: "c1", plan_id: "p", source_session_id: null, ...fields });

  it("is read while the copy runs, by that session only, and not once it has ended", () => {
    const running = { c1: record({ requestId: "c1", kind: "copy_data", command: copy({ source_session_id: "tab-1" }) }) };
    const reading = { jobs: running, order: ["c1"] };
    expect([copyReadsSession(reading, "tab-1"), copyReadsSession(reading, "tab-2")]).toEqual([true, false]);
    const ended = { c1: { ...running.c1, state: { ...running.c1.state, phase: "ended" as const } } };
    expect(copyReadsSession({ jobs: ended, order: ["c1"] }, "tab-1")).toBe(false);
  });

  it("is not read by a copy of tables, a run, or a job listed after a reload (no command)", () => {
    const jobs = {
      a: record({ requestId: "a", kind: "copy_data", command: copy({ source_session_id: null }) }),
      b: record({ requestId: "b", kind: "copy_data", command: null }),
      c: record({ requestId: "c", kind: "run_file", command }),
    };
    expect(copyReadsSession({ jobs, order: ["a", "b", "c"] }, "tab-1")).toBe(false);
  });
});
