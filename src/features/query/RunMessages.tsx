import { useVirtualizer } from "@tanstack/react-virtual";
import { Ban, CircleCheck, CircleDashed, CircleMinus, CircleX, Loader2 } from "lucide-react";
import { memo, useMemo, useRef, type ReactNode } from "react";
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
  /** Where statement `index`'s error is in the editor, if it has one. Stable, like the two below. */
  locate: (index: number, execution: ExecutionState) => ErrorLocation | null;
  onJump: (index: number, execution: ExecutionState) => void;
  onReconnect: () => void;
}

/** A statement's row before it is measured: its line and one line of details. */
const ROW_ESTIMATE = 46;

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
  const count: Record<StepStatus, number> = { pending: 0, running: 0, done: 0, error: 0, cancelled: 0, skipped: 0 };
  let current = -1;
  run.steps.forEach((step, index) => {
    count[step.status] += 1;
    if (current < 0 && (step.status === "running" || step.status === "pending")) current = index;
  });
  if (run.active) return total === 1 ? "Running…" : `Running statement ${Math.max(current, 0) + 1} of ${total}…`;
  const parts = [`${count.done + count.error + count.cancelled} of ${plural(total, "statement")} run`];
  if (count.error) parts.push(plural(count.error, "error"));
  if (count.cancelled) parts.push("cancelled");
  if (run.finishedAt !== null) parts.push(formatDuration(run.finishedAt - run.startedAt));
  return parts.join(" · ");
}

/**
 * One statement's outcome. It renders again only when its step or its execution changes: the runner and the session
 * keep every other one as it was, so an event costs one row, not the whole list.
 */
const StepRow = memo(function StepRow({
  index,
  step,
  execution,
  handlers,
}: {
  index: number;
  step: RunStep;
  execution: ExecutionState | undefined;
  handlers: Handlers;
}) {
  return (
    <>
      <div className="flex items-center gap-2">
        <span className="shrink-0">{STATUS_ICON[step.status]}</span>
        <span className="min-w-0 flex-1 truncate font-mono" title={step.statement.sql} data-selectable>
          {firstLine(step.statement.sql)}
        </span>
        <span className="shrink-0 text-muted">{outcome(step, execution)}</span>
      </div>
      <StepDetails index={index} execution={execution} {...handlers} />
    </>
  );
});

/**
 * The Messages tab: per statement its outcome, row counts, affected rows, notices and error (click to jump). Only the
 * rows in view are rendered, so a long script stays cheap while it runs.
 */
export function RunMessages({ run, executions, locate, onJump, onReconnect }: RunMessagesProps) {
  const handlers = useMemo(() => ({ locate, onJump, onReconnect }), [locate, onJump, onReconnect]);
  const scroller = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: run.steps.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW_ESTIMATE,
    overscan: 8,
  });
  return (
    <div className="flex h-full flex-col text-[12px]">
      <div className="shrink-0 border-b border-border px-3 py-1.5 text-muted">{summary(run)}</div>
      <div ref={scroller} className="min-h-0 flex-1 overflow-auto">
        <ol className="relative" style={{ height: virtualizer.getTotalSize() }}>
          {virtualizer.getVirtualItems().map((item) => {
            const step = run.steps[item.index];
            return (
              <li
                key={item.key}
                ref={virtualizer.measureElement}
                data-index={item.index}
                className="absolute top-0 left-0 flex w-full flex-col gap-1 border-b border-border px-3 py-1.5"
                style={{ transform: `translateY(${item.start}px)` }}
              >
                <StepRow
                  index={item.index}
                  step={step}
                  execution={step.requestId ? executions[step.requestId] : undefined}
                  handlers={handlers}
                />
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}
