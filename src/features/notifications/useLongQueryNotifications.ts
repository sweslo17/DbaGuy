import { useEffect, useRef } from "react";
import { formatDuration, plural } from "@/lib/format";
import { appIsFocused, notify } from "@/lib/native";
import { useAppSettings } from "@/stores/settings";
import type { RunState } from "../query/useQueryRunner";
import { INTERRUPTED_MESSAGE, type ExecutionState } from "../session/execution";

/** "42 s", or "2 min 05 s" from a minute on: a notification has no use for hundredths. */
function seconds(ms: number): string {
  const whole = Math.round(ms / 1000);
  return whole < 60 ? `${whole} s` : formatDuration(ms);
}

const firstLine = (text: string) => text.split("\n", 1)[0].trim();

/** What a single statement returned: "1,234 rows", "500 rows loaded" (more to fetch), "3 rows affected", or nothing. */
function outcome(execution: ExecutionState | undefined): string | null {
  if (!execution) return null;
  if (execution.resultSets.length) {
    const rows = execution.resultSets.reduce((sum, set) => sum + set.rows.length, 0);
    return `${plural(rows, "row")}${execution.resultSets.some((set) => set.hasMore) ? " loaded" : ""}`;
  }
  if (execution.affected.length) {
    return `${plural(
      execution.affected.reduce((sum, a) => sum + a.rows, 0),
      "row",
    )} affected`;
  }
  return null;
}

/** Why a failed statement failed: the first line of its error. */
function failure(execution: ExecutionState | undefined): string {
  if (execution?.error) return firstLine(execution.error.message);
  if (execution?.interrupted) return INTERRUPTED_MESSAGE;
  return execution?.timedOut ? "Timed out" : "";
}

/**
 * The notification for a finished run (ruling R-02c-13): a failure names the first error; a success says what the one
 * statement returned, or how many statements ran, and on which connection. A cancelled run, or one where nothing ran,
 * gives none.
 */
export function runNotification(
  run: RunState,
  executions: Record<string, ExecutionState>,
  connection: string | null,
): { title: string; body: string } | null {
  if (run.finishedAt === null || run.cancelled || run.steps.some((step) => step.status === "cancelled")) return null;
  const took = seconds(run.finishedAt - run.startedAt);
  const execution = (requestId: string | null) => (requestId ? executions[requestId] : undefined);
  const failed = run.steps.find((step) => step.status === "error");
  if (failed) return { title: `Query failed after ${took}`, body: failure(execution(failed.requestId)) };
  const ran = run.steps.filter((step) => step.status === "done");
  if (!ran.length) return null; // the only statement was not confirmed: nothing ran
  const what = run.steps.length === 1 ? outcome(execution(ran[0].requestId)) : plural(ran.length, "statement");
  return { title: `Query finished in ${took}`, body: [what, connection].filter(Boolean).join(" · ") };
}

/**
 * Notifies when a run of this tab ends after at least `notifications.long_query_seconds` (0: never) while DbaGuy is in
 * the background (spec §4.11). Each run is considered once, when it ends.
 */
export function useLongQueryNotifications(
  run: RunState | null,
  executions: Record<string, ExecutionState>,
  connection: string | null,
): void {
  const { settings } = useAppSettings();
  const threshold = settings.notifications.long_query_seconds;
  const considered = useRef<string | null>(null);
  useEffect(() => {
    if (!run || run.active || run.finishedAt === null || considered.current === run.id) return;
    considered.current = run.id;
    if (threshold <= 0 || run.finishedAt - run.startedAt < threshold * 1000 || appIsFocused()) return;
    const message = runNotification(run, executions, connection);
    if (!message) return;
    console.info(`Notifying: ${message.title}`);
    void notify(message.title, message.body);
  }, [run, executions, connection, threshold]);
}
