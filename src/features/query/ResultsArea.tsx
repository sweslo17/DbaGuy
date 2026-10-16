import { useMemo, useState } from "react";
import { Tabs } from "@/components/ui/Tabs";
import type { Dialect } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { DataGrid } from "../grid/DataGrid";
import { useHeldResult } from "../grid/useHeldResult";
import type { ExecutionState, ResultSetState } from "../session/execution";
import type { SessionController, SessionPhase } from "../session/SessionController";
import { RunMessages, type RunMessagesProps } from "./RunMessages";
import type { RunState } from "./useQueryRunner";

const MESSAGES = "messages";
const TITLE_LENGTH = 30;

/** One result set of the run, shown in its own tab. */
interface ResultTab {
  /** By position (statement and set index): re-running the same script keeps each grid, its widths and pins. */
  key: string;
  label: string;
  statementSql: string;
  execution: ExecutionState;
  set: ResultSetState;
}

/** The statement's first 30 characters (code points, so an emoji is never cut in half), whitespace collapsed. */
function statementTitle(sql: string): string {
  const chars = Array.from(sql.replace(/\s+/g, " ").trim());
  return chars.length > TITLE_LENGTH ? `${chars.slice(0, TITLE_LENGTH).join("")}…` : chars.join("");
}

function resultTabs(run: RunState | null, executions: Record<string, ExecutionState>): ResultTab[] {
  if (!run) return [];
  return run.steps.flatMap((step, stepIndex) => {
    const execution = step.requestId ? executions[step.requestId] : undefined;
    if (!execution) return [];
    const title = statementTitle(step.statement.sql);
    return execution.resultSets.map((set, ordinal) => ({
      key: `${stepIndex}:${set.index}`,
      label: ordinal === 0 ? title : `Result ${ordinal + 1}`,
      statementSql: step.statement.sql,
      execution,
      set,
    }));
  });
}

export interface ResultsAreaProps extends Omit<RunMessagesProps, "run"> {
  run: RunState | null;
  controller: SessionController | null;
  phase: SessionPhase;
  dialect: Dialect | null;
  pageSize: number;
  /** The export's default file name. */
  title: string;
}

/**
 * The results of the tab's latest run (spec §4.1): a tab per result set, titled by its statement, plus Messages.
 * Until a new run delivers its first result, the previous grids stay, dimmed. Grids stay mounted while another tab is
 * shown, so their scroll, widths and a running export survive switching. Without a choice by the user, the first
 * result is shown, or Messages once a statement failed.
 */
export function ResultsArea({
  run,
  executions,
  controller,
  phase,
  dialect,
  pageSize,
  title,
  ...messages
}: ResultsAreaProps) {
  const tabs = useMemo(() => resultTabs(run, executions), [run, executions]);
  const held = useHeldResult(tabs.length ? tabs : null, !!run?.active);
  const shown = held.value ?? [];
  const [picked, setPicked] = useState<{ runId: string; key: string } | null>(null);

  if (!run) {
    return (
      <div className="flex h-full items-center justify-center text-[12px] text-muted">
        Results appear here (⌘↵ to run)
      </div>
    );
  }
  const failed = run.steps.some((step) => step.status === "error");
  const pickedKey =
    picked?.runId === run.id && (picked.key === MESSAGES || shown.some((tab) => tab.key === picked.key))
      ? picked.key
      : null;
  const selected = pickedKey ?? (failed ? MESSAGES : (shown[0]?.key ?? MESSAGES));
  // Only the last statement can still be paused, and only once the run is over (the next statement would end it).
  const canLoadMore = !!controller && phase === "open" && !run.active;

  return (
    <div className="flex h-full flex-col">
      <Tabs
        value={selected}
        onValueChange={(key) => setPicked({ runId: run.id, key })}
        items={[
          ...shown.map((tab) => ({ value: tab.key, label: tab.label, title: tab.statementSql.slice(0, 1000) })),
          { value: MESSAGES, label: "Messages" },
        ]}
        className="shrink-0 overflow-x-auto px-2"
      />
      <div className="relative min-h-0 flex-1">
        {shown.map((tab) => {
          const { execution, set } = tab;
          const last = execution.resultSets.at(-1)?.index === set.index;
          return (
            <div key={tab.key} className={cn("h-full", tab.key !== selected && "hidden")}>
              <DataGrid
                columns={set.columns}
                rows={set.rows}
                hasMore={set.hasMore}
                partial={set.partial}
                loadingMore={execution.status === "running" && last}
                onLoadMore={canLoadMore ? () => controller.fetchMore(execution.requestId, pageSize) : undefined}
                stale={held.stale}
                dialect={dialect}
                exportSource={controller ? { controller, sql: execution.sql, title } : null}
              />
            </div>
          );
        })}
        {selected === MESSAGES && <RunMessages run={run} executions={executions} {...messages} />}
      </div>
    </div>
  );
}
