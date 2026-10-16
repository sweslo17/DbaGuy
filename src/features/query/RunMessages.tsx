import { Ban, CircleCheck, CircleDashed, CircleMinus, CircleX, Loader2 } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";
import { formatDuration, plural } from "@/lib/format";
import { CONNECTION_LOST, type ExecutionState } from "../session/execution";
import type { RunState, RunStep, StepStatus } from "./useQueryRunner";

/** Where an error is in the editor: 1-based; `column` is null when the database gave no position (the statement). */
export interface ErrorLocation {
  line: number;
  column: number | null;
}

export interface RunMessagesProps {
  run: RunState;
  executions: Record<string, ExecutionState>;
  /** Where statement `index`'s error is in the editor, if it has one. */
  locate: (index: number, execution: ExecutionState) => ErrorLocation | null;
  onJump: (index: number, execution: ExecutionState) => void;
  onReconnect: () => void;
}

const STATUS_ICON: Record<StepStatus, ReactNode> = {
  pending: <CircleDashed size={13} className="text-muted" />,
  running: <Loader2 size={13} className="animate-spin text-accent" />,
  done: <CircleCheck size={13} className="text-success" />,
  error: <CircleX size={13} className="text-danger" />,
  cancelled: <Ban size={13} className="text-warning" />,
  skipped: <CircleMinus size={13} className="text-muted" />,
};

const MESSAGE_COLOUR: Record<string, string> = { warning: "text-warning", error: "text-danger" };

/** What ended the statement, or how long it took. */
function outcome(step: RunStep, execution: ExecutionState | undefined): string {
  if (step.status === "pending") return "Waiting";
  if (step.status === "skipped") return "Skipped";
  if (step.status === "running") return "Running…";
  if (execution?.timedOut) return "Timed out";
  if (step.status === "cancelled") return "Cancelled";
  return execution?.elapsedMs != null ? formatDuration(execution.elapsedMs) : "";
}

function firstLine(sql: string): string {
  return (
    sql
      .split("\n")
      .find((line) => line.trim() !== "")
      ?.trim() ?? ""
  );
}

type Handlers = Omit<RunMessagesProps, "run" | "executions">;

function StatementError({
  index,
  execution,
  locate,
  onJump,
  onReconnect,
}: { index: number; execution: ExecutionState } & Handlers) {
  const { error } = execution;
  if (!error) return null;
  const at = locate(index, execution);
  const place = at && (at.column === null ? `Statement at line ${at.line}` : `Line ${at.line}, column ${at.column}`);
  return (
    <div className="flex flex-col items-start gap-0.5 text-danger">
      <div className="whitespace-pre-wrap" data-selectable>
        {error.message}
      </div>
      {(place || error.code) && (
        <div className="flex items-center gap-1 text-[11px]">
          {place && (
            <button
              type="button"
              className="-mx-1 cursor-pointer rounded px-1 underline decoration-dotted underline-offset-2 hover:bg-hover"
              title="Show in the editor"
              onClick={() => onJump(index, execution)}
            >
              {place}
            </button>
          )}
          {error.code && (
            <span className="opacity-80" data-selectable>
              {place ? "· " : ""}
              {error.code}
            </span>
          )}
        </div>
      )}
      {error.code === CONNECTION_LOST && (
        <Button size="sm" onClick={onReconnect}>
          Reconnect
        </Button>
      )}
    </div>
  );
}

function StepDetails({
  index,
  execution,
  ...handlers
}: { index: number; execution: ExecutionState | undefined } & Handlers) {
  if (!execution) return null;
  return (
    <div className="flex flex-col gap-0.5 pl-[22px]">
      {execution.resultSets.map((set) => (
        <div key={`set-${set.index}`} className="text-muted">
          {plural(set.rows.length, "row")}
          {set.hasMore ? " loaded" : set.partial ? " (partial)" : ""}
        </div>
      ))}
      {execution.affected.map((a) => (
        <div key={`affected-${a.index}`}>{plural(a.rows, "row")} affected</div>
      ))}
      {execution.skipped && execution.confirmation && (
        <div className="text-warning">Not run: it needs confirmation.</div>
      )}
      {execution.messages.map((m, i) => (
        <div
          key={`message-${i}`}
          className={cn("whitespace-pre-wrap", MESSAGE_COLOUR[m.severity] ?? "text-muted")}
          data-selectable
        >
          {m.text}
        </div>
      ))}
      {execution.error && <StatementError index={index} execution={execution} {...handlers} />}
    </div>
  );
}

function summary(run: RunState): string {
  const total = run.steps.length;
  const count = (status: StepStatus) => run.steps.filter((step) => step.status === status).length;
  if (run.active) {
    const current = run.steps.findIndex((step) => step.status === "running" || step.status === "pending");
    return total === 1 ? "Running…" : `Running statement ${Math.max(current, 0) + 1} of ${total}…`;
  }
  const parts = [`${count("done") + count("error") + count("cancelled")} of ${plural(total, "statement")} run`];
  if (count("error")) parts.push(plural(count("error"), "error"));
  if (count("cancelled")) parts.push("cancelled");
  if (run.finishedAt !== null) parts.push(formatDuration(run.finishedAt - run.startedAt));
  return parts.join(" · ");
}

/** The Messages tab: per statement its outcome, row counts, affected rows, notices and error (click to jump). */
export function RunMessages({ run, executions, ...handlers }: RunMessagesProps) {
  return (
    <div className="h-full overflow-auto text-[12px]">
      <div className="border-b border-border px-3 py-1.5 text-muted">{summary(run)}</div>
      <ol>
        {run.steps.map((step, index) => {
          const execution = step.requestId ? executions[step.requestId] : undefined;
          return (
            <li key={index} className="flex flex-col gap-1 border-b border-border px-3 py-1.5">
              <div className="flex items-center gap-2">
                <span className="shrink-0">{STATUS_ICON[step.status]}</span>
                <span className="min-w-0 flex-1 truncate font-mono" title={step.statement.sql} data-selectable>
                  {firstLine(step.statement.sql)}
                </span>
                <span className="shrink-0 text-muted">{outcome(step, execution)}</span>
              </div>
              <StepDetails index={index} execution={execution} {...handlers} />
            </li>
          );
        })}
      </ol>
    </div>
  );
}
