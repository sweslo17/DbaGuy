/*
 * What quitting asks while data-tools jobs run (spec ⑥ R-06-15, CR-5, A12): ⌘Q, the menu's Quit and closing the window
 * (which quits the app) stop every job, because the backend goes with the app. Only jobs ask (CR-5): the other close
 * guards stay a 02c carry. Pure (the list of jobs and the way to ask are passed in). Not `quitGuard.ts`: beside
 * `QuitGuard.tsx` the two names are one file on the Mac's case-insensitive disk (06b pre-flight H2).
 */

import type { JobInfo } from "@/lib/api/types";

export type QuitHow = "quit" | "close";

export interface RunningJob {
  job: JobInfo["job"];
  title: string;
}

export interface QuitQuestion {
  title: string;
  message: string;
  keep: string;
  confirm: string;
}

const LISTED = 3;

/** The question to ask, or null when no job runs (then the app quits at once, as before). */
export function quitQuestion(running: readonly RunningJob[], how: QuitHow): QuitQuestion | null {
  if (!running.length) return null;
  const n = running.length;
  const names = running.slice(0, LISTED).map((job) => job.title);
  const more = n > LISTED ? `, and ${n - LISTED} more` : "";
  const subject = n === 1 ? "A job is" : `${n} jobs are`;
  const act = how === "quit" ? "Quitting" : "Closing the window quits DbaGuy, which";
  const them = n === 1 ? "it" : "them";
  return {
    title: how === "quit" ? "Quit DbaGuy?" : "Close the window?",
    // A run in one transaction is rolled back (not "applies nothing": MySQL's non-transactional tables keep their
    // changes, and the run reports unknown; 06b review). A stopped run statement by statement keeps what took effect,
    // but not a transaction the file opened and had not committed: the server rolls it back (06b pre-flight M5). A
    // copy's tables are each a transaction of their own: the one being copied is rolled back (06c).
    message:
      `${subject} still running: ${names.join("; ")}${more}. ${act} stops ${them}: a dump writes no file, a run in ` +
      "one transaction is rolled back, a run statement by statement keeps what took effect (a transaction the file " +
      "opened and did not commit is rolled back), and a copy rolls back the table it is copying (the tables before " +
      "it stay).",
    keep: "Keep running",
    confirm: how === "quit" ? "Quit anyway" : "Close anyway",
  };
}

/**
 * The jobs running now, as `list` (GET /data/jobs) says; none when it fails or cannot say within `timeoutMs` — the
 * whole wait, the backend's address included (a backend that is starting or gone has no job to keep; 06b pre-flight
 * M3: the shell's own 2 s fallback is off once the page has acked, so this bound is what keeps ⌘Q from hanging).
 */
export async function runningNow(
  list: () => Promise<readonly { job: RunningJob["job"]; title: string; outcome?: unknown }[]>,
  timeoutMs: number,
): Promise<RunningJob[]> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const late = new Promise<null>((resolve) => (timer = setTimeout(() => resolve(null), timeoutMs)));
  try {
    const jobs = await Promise.race([list(), late]);
    return (jobs ?? []).filter((info) => !info.outcome).map((info) => ({ job: info.job, title: info.title }));
  } catch (error) {
    console.warn("Quit: the running jobs could not be listed", error);
    return [];
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Decides a Quit or a window's close: `list` gives the jobs running now (`runningNow`, already bounded), and `ask`
 * puts the question to the user only when there is one (it brings the window to the front first). No job: allowed
 * at once, nothing asked. A second request while one is being decided (⌘Q twice, ⌘Q during a close's question) gets
 * the same answer; once it is settled, the next request decides again.
 */
export function quitDecider(
  list: () => Promise<readonly RunningJob[]>,
  ask: (question: QuitQuestion) => Promise<boolean>,
): (how: QuitHow) => Promise<boolean> {
  let pending: Promise<boolean> | null = null;
  return (how) => {
    pending ??= (async () => {
      const question = quitQuestion(await list(), how);
      return question ? ask(question) : true;
    })().finally(() => {
      pending = null;
    });
    return pending;
  };
}
