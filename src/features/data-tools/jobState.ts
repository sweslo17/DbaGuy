import type {
  DumpCommand,
  JobInfo,
  JobOutcomeEvent,
  JobProgressEvent,
  RunFileCommand,
  ServerEvent,
} from "@/lib/api/types";

/*
 * One data-tools job as the frontend knows it (spec ⑥ R-06-15, D-06-10): folded from its WebSocket events and the
 * backend's job list. Pure: the runner (`jobRunner.ts`) and the store (`jobsStore.ts`) feed it events. The backend's
 * `job_outcome` is the end, with or without the `finished` after it (a client that attaches late gets none, 06a carry);
 * when the backend can no longer say (its session is gone), the frontend ends the job with a `loss` instead, which a
 * later outcome from the backend's list replaces. Nothing here ever sends anything.
 */

export type JobKind = JobOutcomeEvent["job"];
export type JobCommand = DumpCommand | RunFileCommand;

/** How a job ended without the backend's outcome: only what the frontend knows. */
export type JobLoss =
  /** Its session could not be opened (a password, a refused connection): nothing was sent. */
  | "open_failed"
  /** The command never left: cancelled while connecting, or the socket was refused before it opened. */
  | "not_sent"
  /** It was sent, then its session went away (the backend stopped or restarted) before an outcome. */
  | "session_gone"
  /**
   * It was sent a while ago, nothing came back, and the backend's list does not have it. A guess, not the backend's
   * word (a list can race the command): what the list or the socket says afterwards outranks it (pre-flight M2).
   */
  | "not_listed"
  /** The request finished without an outcome (the contract says it never does: read as unknown). */
  | "no_outcome";

export interface JobState {
  phase: "opening" | "running" | "ended";
  /** The command reached the socket: from here on it is never sent again. */
  sent: boolean;
  progress: JobProgressEvent | null;
  outcome: JobOutcomeEvent | null;
  loss: JobLoss | null;
  /** Why the session could not be opened (`open_failed`): the ApiError, for the unlock flow. */
  openError: Error | null;
  /** The live `error` event (the outcome carries it too, 06a F3). */
  error: { message: string; code: string | null } | null;
  /** The job's server messages and warnings, as they came (the backend sends at most MAX_JOB_MESSAGES). */
  messages: { severity: string; text: string }[];
  cancelRequestedAt: number | null;
  /** The socket is closed and trying again: events may be late, never lost (the backend replays its last ones). */
  reconnecting: boolean;
  /** When this window began the job (a session to open); a listed job: when the backend says it started. */
  startedAt: number;
  /** When the command left for the backend (null: not yet). `not_listed` is measured from here, not `startedAt` (M2). */
  sentAt: number | null;
  endedAt: number | null;
  /** When the last event of this job arrived (null: none yet). */
  lastEventAt: number | null;
}

export type JobEvent =
  | { type: "sent"; at: number }
  | { type: "open_failed"; error: Error; at: number }
  | { type: "not_sent"; at: number }
  | { type: "server"; event: ServerEvent; at: number }
  | { type: "socket"; open: boolean }
  | { type: "session_gone"; at: number }
  | { type: "not_listed"; at: number }
  | { type: "cancel_sent"; at: number }
  | { type: "listed"; info: JobInfo; at: number }
  | { type: "retry"; at: number };

/** The backend's own cap (`MAX_JOB_MESSAGES` in domain/data_jobs.py): a job never sends more. */
export const MAX_JOB_MESSAGES = 200;

/** A job this window is starting: its session is being opened. */
export function openingJob(at: number): JobState {
  return {
    phase: "opening",
    sent: false,
    progress: null,
    outcome: null,
    loss: null,
    openError: null,
    error: null,
    messages: [],
    cancelRequestedAt: null,
    reconnecting: false,
    startedAt: at,
    sentAt: null,
    endedAt: null,
    lastEventAt: null,
  };
}

/** A job the backend lists (a reloaded window): what the list says, sent when it started. */
export function listedJob(info: JobInfo, at: number): JobState {
  const parsed = Date.parse(info.started_at);
  const start = Number.isNaN(parsed) ? at : parsed;
  const base = { ...openingJob(start), sent: true, sentAt: start, progress: info.progress ?? null };
  if (!info.outcome) return { ...base, phase: "running" };
  return { ...base, phase: "ended", outcome: info.outcome, error: errorOf(info.outcome), endedAt: at };
}

const errorOf = (outcome: JobOutcomeEvent) =>
  outcome.error ? { message: outcome.error.message, code: outcome.error.code ?? null } : null;

const ended = (state: JobState, at: number, loss: JobLoss, patch: Partial<JobState> = {}): JobState => ({
  ...state,
  phase: "ended",
  loss,
  endedAt: at,
  reconnecting: false,
  ...patch,
});

/** A job ended as `not_listed` that turned out to be running (the list has it, or the socket spoke): it runs again. */
const running = (state: JobState, progress: JobProgressEvent | null): JobState => ({
  ...state,
  phase: "running",
  loss: null,
  endedAt: null,
  progress: state.progress ?? progress,
});

/** What nothing reached the backend for: its job may be started again (`retry`). */
export const neverReachedBackend = (state: JobState): boolean =>
  state.phase === "ended" && (state.loss === "open_failed" || state.loss === "not_sent");

function onServerEvent(state: JobState, event: ServerEvent, at: number): JobState {
  // The first ending stands (a `finished` after the outcome, say); except a guessed one: the socket's word outranks it.
  const live = state.loss === "not_listed" ? running(state, null) : state;
  if (live.phase === "ended") return state;
  const seen = { ...live, lastEventAt: at };
  switch (event.type) {
    case "started":
      return { ...seen, phase: "running" };
    case "job_progress":
      return { ...seen, phase: "running", progress: event };
    case "job_outcome":
      return {
        ...seen,
        phase: "ended",
        outcome: event,
        error: errorOf(event) ?? live.error,
        endedAt: at,
        reconnecting: false,
      };
    case "error":
      return { ...seen, error: { message: event.message, code: event.code ?? null } };
    case "message":
      if (live.messages.length >= MAX_JOB_MESSAGES) return seen;
      return { ...seen, messages: [...live.messages, { severity: event.severity, text: event.text }] };
    case "finished":
      return ended(seen, at, "no_outcome");
    default:
      return state; // session_state, and nothing else a job sends
  }
}

/**
 * The backend's list: an outcome replaces a loss (it is the truth); a job lost as `not_listed` that the list has
 * running runs again; a running job takes the list's progress if it has none (a live one is newer).
 */
function onListed(state: JobState, info: JobInfo, at: number): JobState {
  if (state.outcome) return state;
  if (info.outcome) {
    return { ...state, phase: "ended", outcome: info.outcome, loss: null, error: errorOf(info.outcome), endedAt: at };
  }
  if (state.loss === "not_listed") return running(state, info.progress ?? null);
  if (state.phase === "ended") return state;
  return state.progress || !info.progress ? state : { ...state, phase: "running", progress: info.progress };
}

export function jobReducer(state: JobState, event: JobEvent): JobState {
  switch (event.type) {
    case "sent":
      return state.phase === "opening" ? { ...state, sent: true, sentAt: state.sentAt ?? event.at } : state;
    case "open_failed":
      return state.phase === "ended" ? state : ended(state, event.at, "open_failed", { openError: event.error });
    case "not_sent":
      return state.phase === "ended" || state.sent ? state : ended(state, event.at, "not_sent");
    case "server":
      return onServerEvent(state, event.event, event.at);
    case "socket":
      return state.phase === "ended" ? state : { ...state, reconnecting: !event.open };
    case "session_gone":
      return state.phase === "ended" ? state : ended(state, event.at, state.sent ? "session_gone" : "not_sent");
    case "not_listed":
      return state.phase === "ended" || !state.sent ? state : ended(state, event.at, "not_listed");
    case "cancel_sent":
      return state.phase === "ended" ? state : { ...state, cancelRequestedAt: event.at };
    case "listed":
      return onListed(state, event.info, event.at);
    case "retry":
      return neverReachedBackend(state) ? openingJob(event.at) : state;
  }
}

/** The stage the last progress was at ("commit": the run's COMMIT may have been sent). */
export const lastStage = (state: JobState): JobProgressEvent["stage"] | null => state.progress?.stage ?? null;
