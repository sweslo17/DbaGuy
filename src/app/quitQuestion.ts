/*
 * What quitting asks while data-tools jobs run (spec ⑥ R-06-15, CR-5, A12): ⌘Q, the menu's Quit and closing the window
 * (which quits the app) stop every job, because the backend goes with the app. Only jobs ask (CR-5): the other close
 * guards stay a 02c carry. Pure (the list of jobs is passed in). Not `quitGuard.ts`: beside `QuitGuard.tsx` the two
 * names are one file on the Mac's case-insensitive disk (06b pre-flight H2).
 */

export type QuitHow = "quit" | "close";

export interface RunningJob {
  job: "dump" | "run_file";
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
    // A stopped run statement by statement keeps what took effect, but not a transaction the file opened and had not
    // committed: the server rolls it back (06b pre-flight M5).
    message:
      `${subject} still running: ${names.join("; ")}${more}. ${act} stops ${them}: a dump writes no file, a run in ` +
      "one transaction applies nothing, and a run statement by statement keeps what took effect (a transaction the " +
      "file opened and did not commit is rolled back).",
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
