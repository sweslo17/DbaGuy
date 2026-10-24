import { create } from "zustand";
import { errorMessage } from "@/lib/api/client";
import { SessionSocket } from "@/lib/api/session-socket";
import type { JobInfo } from "@/lib/api/types";
import type { CopyForm } from "./copyForm";
import type { CopyEntry, DumpEntry, RunEntry } from "./entryPoints";
import { JobRunner, type JobTransport } from "./jobRunner";
import {
  cancelJobByRest,
  clearFinishedJobsOnBackend,
  closeJobSession,
  listJobs,
  openJobSession,
} from "./jobsApi";
import {
  jobReducer,
  listedJob,
  neverReachedBackend,
  openingJob,
  type JobCommand,
  type JobEvent,
  type JobKind,
  type JobState,
} from "./jobState";

/*
 * This window's data-tools jobs (spec ⑥ R-06-15, D-06-10), outside React: a job outlives its dialog, and the Jobs list,
 * the dialogs, the notifications and the quit guard all read it. A job is keyed by its request id (made here, unique).
 * The backend's list (`GET /data/jobs`) is the truth after a reload or a backend restart: `syncJobs` adds what it lists
 * that this window does not know, attaches to the running ones, and ends a job it no longer has (`not_listed`). That
 * ending is a guess (a list can race the command), so the job's runner keeps listening: what its socket or a later
 * list says outranks it (pre-flight M2).
 */

export interface JobRecord {
  requestId: string;
  sessionId: string | null;
  kind: JobKind;
  /** The connection the job's session is on (a copy's: its target). */
  profileId: string;
  /** A copy's source connection (06c): its sessions close when it is deleted, so it waits for the copy too (B13). */
  sourceProfileId: string | null;
  database: string | null;
  title: string;
  /** What was sent, and the dialog entry it came from: null for a job listed after a reload. */
  command: JobCommand | null;
  entry: DumpEntry | RunEntry | CopyEntry | null;
  /** A dump's tables in the file's order (its plan), a copy's target tables in its order: "table k of n". */
  tables: readonly string[] | null;
  /** A copy started here: its form ("Copy again…") and its source tables' names in the copy's order ("Copy the rest…"). */
  copy: { form: CopyForm; sources: readonly string[] } | null;
  state: JobState;
}

export interface JobsSnapshot {
  jobs: Readonly<Record<string, JobRecord>>;
  /** Request ids, newest first. */
  order: readonly string[];
}

export const useJobs = create<JobsSnapshot>(() => ({ jobs: {}, order: [] }));

export const backendTransport: JobTransport = {
  openSession: openJobSession,
  closeSession: closeJobSession,
  cancel: cancelJobByRest,
  socket: (sessionId) => new SessionSocket(sessionId),
  now: () => Date.now(),
};

let transport: JobTransport = backendTransport;
/** Tests only: the backend the runners reach. */
export function setJobTransport(next: JobTransport): void {
  transport = next;
}

/** The backend's job list, as the store reaches it (`jobsApi`; fakes in the tests: no request ever leaves a test). */
export interface JobListing {
  list(): Promise<JobInfo[]>;
  clearFinished(): Promise<void>;
}

const backendListing: JobListing = { list: () => listJobs(), clearFinished: clearFinishedJobsOnBackend };
let listing: JobListing = backendListing;
/** Tests only: the job list the store syncs with. */
export function setJobListing(next: JobListing): void {
  listing = next;
}

/** The one runner that speaks for each job this window listens to. */
const runners = new Map<string, JobRunner>();
const endListeners = new Set<(record: JobRecord) => void>();
/** A job sent this long ago that the backend's list does not have is not there: the backend restarted (or never got it). */
export const NOT_LISTED_AFTER_MS = 10_000;
/**
 * The backend keeps this many jobs, the oldest ended ones going first (`MAX_JOBS_LISTED` in domain/data_jobs.py). Past
 * it this window forgets the oldest ended jobs the backend no longer lists (pre-flight L14): its list stays bounded
 * and offers no Show in Finder the backend would refuse.
 */
export const MAX_JOBS_KEPT = 200;

export const isRunning = (record: JobRecord): boolean => record.state.phase !== "ended";

/**
 * Told when a job ends (its notification, its toast): once, and once more only if the backend's word later replaces an
 * ending this window had to guess (a loss replaced by the outcome, or a job lost as not listed that was running).
 */
export function onJobEnded(listener: (record: JobRecord) => void): () => void {
  endListeners.add(listener);
  return () => endListeners.delete(listener);
}

/** A new ending: the job ended, or its loss gave way to the backend's outcome. */
const endingChanged = (before: JobState, after: JobState): boolean =>
  after.phase === "ended" && (before.phase !== "ended" || (before.outcome === null && after.outcome !== null));

function put(record: JobRecord, first = false): void {
  useJobs.setState(({ jobs, order }) => ({
    jobs: { ...jobs, [record.requestId]: record },
    order: first || !(record.requestId in jobs) ? [record.requestId, ...order.filter((id) => id !== record.requestId)] : order,
  }));
}

/** Jobs the backend lists that this window did not know, newest first, on top: one update. */
function putAll(records: readonly JobRecord[]): void {
  if (records.length === 0) return;
  useJobs.setState(({ jobs, order }) => ({
    jobs: { ...jobs, ...Object.fromEntries(records.map((record) => [record.requestId, record])) },
    order: [...records.map((record) => record.requestId), ...order],
  }));
}

/** How many times this window forgot jobs: a list asked for before then may still have them (06b T5 L1). */
let forgettings = 0;

/** Forgets jobs (Clear finished, the oldest beyond MAX_JOBS_KEPT): one update; a runner still listening detaches. */
function forget(requestIds: readonly string[]): void {
  if (requestIds.length === 0) return;
  forgettings++;
  const gone = new Set(requestIds);
  for (const requestId of gone) {
    runners.get(requestId)?.detach();
    runners.delete(requestId);
  }
  useJobs.setState(({ jobs, order }) => ({
    jobs: Object.fromEntries(Object.entries(jobs).filter(([requestId]) => !gone.has(requestId))),
    order: order.filter((requestId) => !gone.has(requestId)),
  }));
}

function tellEnded(record: JobRecord): void {
  for (const listener of endListeners) {
    try {
      listener(record);
    } catch (error) {
      // A listener's bug never stops the runner that reported the end (it still has to close the job's session)
      console.warn(`Job ${record.requestId}: a listener of its end failed: ${errorMessage(error)}`);
    }
  }
}

function dispatch(requestId: string, event: JobEvent): void {
  const record = useJobs.getState().jobs[requestId];
  if (!record) return;
  const state = jobReducer(record.state, event);
  // The runner goes once the job's end is known here; a job ended as not listed keeps it (its socket may still speak),
  // until that socket is refused too (M2)
  if (state.phase === "ended" && (state.loss !== "not_listed" || event.type === "session_gone")) runners.delete(requestId);
  if (state === record.state) return;
  const next = { ...record, state };
  put(next);
  if (!endingChanged(record.state, state)) return;
  tellEnded(next);
  // The session went before this window heard the outcome: the backend may still list it (a session reaped after its
  // job ended while no socket listened), and its outcome replaces the loss at once (M2)
  if (state.loss === "session_gone") void syncJobs();
}

/** A runner for the job, the one that speaks for it: a runner it replaced is no longer heard. */
function listenTo(requestId: string, onSession?: (sessionId: string) => void): JobRunner {
  const runner: JobRunner = new JobRunner(
    requestId,
    transport,
    (event) => {
      if (runners.get(requestId) === runner) dispatch(requestId, event);
    },
    (sessionId) => {
      if (runners.get(requestId) === runner) onSession?.(sessionId);
    },
  );
  runners.set(requestId, runner);
  return runner;
}

function run(record: JobRecord): void {
  const runner = listenTo(record.requestId, (sessionId) => {
    const current = useJobs.getState().jobs[record.requestId];
    if (current) put({ ...current, sessionId });
  });
  if (record.command) runner.start(record.profileId, record.database, record.command);
}

export interface NewJob {
  kind: JobKind;
  profileId: string;
  sourceProfileId?: string | null;
  database: string | null;
  title: string;
  command: JobCommand;
  entry: DumpEntry | RunEntry | CopyEntry;
  tables?: readonly string[] | null;
  copy?: { form: CopyForm; sources: readonly string[] } | null;
}

/** Starts a job on a session of its own; returns its request id (the command's). */
export function startJob(job: NewJob): string {
  const record: JobRecord = {
    ...job,
    requestId: job.command.request_id,
    sessionId: null,
    sourceProfileId: job.sourceProfileId ?? null,
    tables: job.tables ?? null,
    copy: job.copy ?? null,
    state: openingJob(transport.now()),
  };
  put(record, true);
  run(record);
  return record.requestId;
}

/** Starts again a job nothing of which reached the backend (its session did not open, or its command never left). */
export function retryJob(requestId: string): boolean {
  const record = useJobs.getState().jobs[requestId];
  if (!record?.command || !neverReachedBackend(record.state)) return false;
  const again = { ...record, sessionId: null, state: jobReducer(record.state, { type: "retry", at: transport.now() }) };
  put(again);
  run(again);
  return true;
}

export function cancelJob(requestId: string): void {
  runners.get(requestId)?.cancel();
}

export interface Reconciled {
  /** Jobs the backend lists that this window does not know (a reload): added with what the list says. */
  added: JobRecord[];
  events: [requestId: string, event: JobEvent][];
  /** Running listed jobs to listen to. */
  attach: [requestId: string, sessionId: string][];
  /** Sessions of listed jobs that have ended: closed (never reused). */
  close: string[];
  /** Ended jobs the backend no longer lists, the oldest first, beyond MAX_JOBS_KEPT: forgotten here too (L14). */
  dropped: string[];
}

/** What the backend's list changes here (pure). */
export function reconcile(jobs: Readonly<Record<string, JobRecord>>, listed: readonly JobInfo[], now: number): Reconciled {
  const result: Reconciled = { added: [], events: [], attach: [], close: [], dropped: [] };
  const seen = new Set<string>();
  for (const info of listed) {
    seen.add(info.request_id);
    const known = jobs[info.request_id];
    if (known) {
      result.events.push([info.request_id, { type: "listed", info, at: now }]);
      continue;
    }
    result.added.push({
      requestId: info.request_id,
      sessionId: info.session_id,
      kind: info.job,
      profileId: info.profile_id,
      sourceProfileId: info.source_profile_id ?? null,
      database: null,
      title: info.title,
      command: null,
      entry: null,
      tables: null,
      copy: null,
      state: listedJob(info, now),
    });
    if (info.outcome) result.close.push(info.session_id);
    else result.attach.push([info.request_id, info.session_id]);
  }
  const unlisted = Object.values(jobs).filter((record) => !seen.has(record.requestId));
  for (const record of unlisted) {
    const { sentAt } = record.state;
    if (!isRunning(record) || sentAt === null) continue;
    // From when the command left, not when its session began to open (M2): a slow open never makes a fresh job "lost"
    if (now - sentAt >= NOT_LISTED_AFTER_MS) result.events.push([record.requestId, { type: "not_listed", at: now }]);
  }
  const excess = Object.keys(jobs).length + result.added.length - MAX_JOBS_KEPT;
  if (excess > 0) {
    // A job ended as not listed is kept: what its socket says may still revive it
    result.dropped = unlisted
      .filter((record) => !isRunning(record) && record.state.loss !== "not_listed")
      .sort((a, b) => a.state.startedAt - b.state.startedAt)
      .slice(0, excess)
      .map((record) => record.requestId);
  }
  return result;
}

/** Closes ended jobs' sessions one at a time, in the background of a sync: a reload's burst never crowds other requests. */
async function closeSessions(sessionIds: readonly string[]): Promise<void> {
  for (const sessionId of sessionIds) {
    try {
      await transport.closeSession(sessionId);
    } catch (error) {
      console.warn(`Could not close session ${sessionId}: ${errorMessage(error)}`);
    }
  }
}

/** A sync asks at most this often when jobs were forgotten while it waited (the first list, and once more). */
const SYNC_ASKS = 2;

/**
 * Brings the list in line with the backend's (on start, whenever the Jobs list opens, and after a session is gone). A
 * list asked for before this window forgot jobs (Clear finished, the oldest beyond MAX_JOBS_KEPT) may still have them:
 * it is asked again, and a list that is stale again only updates the jobs this window still has (L1).
 */
export async function syncJobs(): Promise<void> {
  for (let asked = 1; ; asked++) {
    const before = forgettings;
    let listed: JobInfo[];
    try {
      listed = await listing.list();
    } catch (error) {
      console.warn(`Could not list the jobs: ${errorMessage(error)}`);
      return;
    }
    const stale = forgettings !== before;
    if (stale && asked < SYNC_ASKS) continue;
    await applyListed(listed, !stale);
    return;
  }
}

/** The backend's list, applied; `whole` false (a list older than what this window forgot): only its known jobs' events. */
async function applyListed(listed: readonly JobInfo[], whole: boolean): Promise<void> {
  const { added, events, attach, close, dropped } = reconcile(useJobs.getState().jobs, listed, transport.now());
  if (whole) putAll(added);
  for (const [requestId, event] of events) dispatch(requestId, event);
  if (!whole) return;
  forget(dropped);
  for (const [requestId, sessionId] of attach) {
    if (!runners.has(requestId)) listenTo(requestId).attach(sessionId);
  }
  await closeSessions(close);
}

/** Clear finished: forgotten here and by the backend (POST /data/jobs/clear). */
export async function clearFinishedJobs(): Promise<void> {
  await listing.clearFinished();
  const { jobs, order } = useJobs.getState();
  forget(order.filter((requestId) => !isRunning(jobs[requestId])));
}

/** Tests only: a fresh window (every runner detached, no job, no runner). */
export function resetJobsForTests(): void {
  for (const runner of runners.values()) runner.detach();
  runners.clear();
  useJobs.setState({ jobs: {}, order: [] });
}

/** The jobs, newest first. */
export const jobList = ({ jobs, order }: JobsSnapshot): JobRecord[] => order.map((id) => jobs[id]);
export const runningJobs = (snapshot: JobsSnapshot): JobRecord[] => jobList(snapshot).filter(isRunning);
/**
 * The running jobs a connection's deletion would stop (B13): those on its sessions, and the copies reading from it (06c:
 * `DELETE /profiles/{id}` closes the source's session too).
 */
export const runningOn = (snapshot: JobsSnapshot, profileId: string): JobRecord[] =>
  runningJobs(snapshot).filter((job) => job.profileId === profileId || job.sourceProfileId === profileId);
/** How many jobs run (the Jobs button's count): a number, so a selector of it renders only when it changes (Q1). */
export const runningCount = (snapshot: JobsSnapshot): number => runningJobs(snapshot).length;
/** Whether a job has ended (Clear finished has something to clear): a boolean, for the same reason. */
export const hasEndedJob = (snapshot: JobsSnapshot): boolean => jobList(snapshot).some((record) => !isRunning(record));
