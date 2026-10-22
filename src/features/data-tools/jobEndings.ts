import type { ToastKind } from "@/components/ui/toastItems";
import { endingIsNews, jobNotification, jobReport, type Tone } from "./jobReport";
import type { JobState } from "./jobState";
import type { JobRecord } from "./jobsStore";

/*
 * When a data-tools job ends, and what is said then (spec ⑥ R-06-15, ruling V11): a system notification per S8 and,
 * when the job's view is not open, a toast with Open. Pure, but for the teller's timer. Each ending of a job is told at
 * most once; an ending this window had to guess waits a moment first, because the backend's word often follows at once
 * (pre-flight M2: a session gone, then the list's outcome; a job not listed whose socket then speaks), and a word that
 * replaces an ending already told takes its toast's place, or dismisses it when it is not news. A job first seen
 * already ended (listed after a reload) is never told: `jobsStore` reports no ending for it. No store here: the
 * teller reads the job through what it is given.
 */

/** How long a guessed ending waits before it is told: the backend's word that replaces it comes in that time. */
export const SETTLE_MS = 2_000;

/** An ending this window had to guess (no outcome from the backend yet), which the backend's word may still replace. */
export const isGuessedEnding = (state: JobState): boolean =>
  state.phase === "ended" && state.outcome === null && (state.loss === "session_gone" || state.loss === "not_listed");

/**
 * One ending of one attempt of a job: what is told at most once. A retry is another attempt (its own `startedAt`); a job
 * guessed lost twice is one ending. Null while it runs.
 */
export function endingKey(state: JobState): string | null {
  if (state.phase !== "ended") return null;
  const outcome = state.outcome;
  const end = outcome ? `outcome:${outcome.outcome}${outcome.cancelled ? ":cancelled" : ""}` : `loss:${state.loss}`;
  return `${state.startedAt}|${end}`;
}

/**
 * Whether this window saw the job end as it ended: the backend's outcome on the job's socket, or an ending of this
 * window's own (it is its own time). Not an outcome read from the backend's list (a job listed after a reload, or one
 * whose loss the list replaced: `JobInfo` has no end time), nor `not_listed` (the time of the sync that missed it: the
 * job went at some moment before).
 */
export function endTimeKnown(state: JobState): boolean {
  if (state.phase !== "ended" || state.endedAt === null) return false;
  if (state.outcome === null) return state.loss !== "not_listed";
  return state.lastEventAt === state.endedAt;
}

/** The time beside a job in the Jobs list: how long it has run so far, or how long it ran; null when its end is not known. */
export function shownElapsed(state: JobState, now: number): number | null {
  if (state.phase !== "ended") return Math.max(0, now - state.startedAt);
  return endTimeKnown(state) && state.endedAt !== null ? Math.max(0, state.endedAt - state.startedAt) : null;
}

/** A toast's kind for a report's tone. */
export const TOAST_KINDS: Record<Tone, ToastKind> = { success: "success", warning: "warning", danger: "error", muted: "info" };

export interface EndingContext {
  /** The job whose view is open now (`shownJob()`): its ending needs no toast. */
  shown: string | null;
  /** DbaGuy is in front (`appIsFocused()`): no notification. */
  focused: boolean;
  /** S8's `notifications.long_query_seconds` (0: never). */
  thresholdS: number;
}

export interface EndingNotice {
  notification: { title: string; body: string } | null;
  toast: { kind: ToastKind; text: string } | null;
}

/**
 * What is said as a job ends (V11), for an ending that is news (`endingIsNews`: not one that ended cancelled, nor one
 * never sent; a Cancel that lost the race is news): the notification `jobNotification` gives (in the background, after
 * the threshold; not for `open_failed`), only when this window saw the job end (its time says how long it took); and a
 * toast "<outcome> — <job>" when its view is not open (`open_failed` too: its view asks for the password).
 */
export function endingNotice(record: JobRecord, context: EndingContext): EndingNotice {
  const report = jobReport(record);
  if (!report) return { notification: null, toast: null };
  const { state } = record;
  const notification = endTimeKnown(state) ? jobNotification(record, context.thresholdS, context.focused) : null;
  const quiet = context.shown === record.requestId || !endingIsNews(state);
  return {
    notification,
    toast: quiet ? null : { kind: TOAST_KINDS[report.tone], text: `${report.title} — ${record.title}` },
  };
}

/** The key of a job's ending toast: a corrected ending takes its place, or dismisses it when it is not news. */
export const endingToastKey = (requestId: string): string => `job:${requestId}`;

export interface EndingTeller {
  /** A job ended, or its ending changed (`onJobEnded`). */
  ended(record: JobRecord): void;
  /** Endings still waiting are dropped. */
  stop(): void;
}

/**
 * Tells each ending once (`tell`): a guessed one after SETTLE_MS, and only if the job still ends that way then (`current`
 * reads the store: a job forgotten, running again or ended otherwise meanwhile is not told that ending).
 */
export function endingTeller(
  current: (requestId: string) => JobRecord | undefined,
  tell: (record: JobRecord) => void,
): EndingTeller {
  const told = new Map<string, string>();
  const waiting = new Map<string, ReturnType<typeof setTimeout>>();
  const tellNow = (requestId: string, key: string) => {
    const record = current(requestId);
    if (!record || endingKey(record.state) !== key || told.get(requestId) === key) return;
    told.set(requestId, key);
    tell(record);
  };
  return {
    ended(record) {
      const key = endingKey(record.state);
      if (key === null) return;
      const { requestId } = record;
      clearTimeout(waiting.get(requestId));
      waiting.delete(requestId);
      if (!isGuessedEnding(record.state)) {
        tellNow(requestId, key);
        return;
      }
      waiting.set(
        requestId,
        setTimeout(() => {
          waiting.delete(requestId);
          tellNow(requestId, key);
        }, SETTLE_MS),
      );
    },
    stop() {
      for (const timer of waiting.values()) clearTimeout(timer);
      waiting.clear();
    },
  };
}
