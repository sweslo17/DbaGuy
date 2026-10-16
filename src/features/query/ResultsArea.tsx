import { useMemo, useState } from "react";
import { Tabs } from "@/components/ui/Tabs";
import type { Dialect } from "@/lib/api/types";
import { useHeldResult } from "../grid/useHeldResult";
import type { ExecutionState, ResultSetState } from "../session/execution";
import type { SessionController, SessionPhase } from "../session/SessionController";
import { ResultGrid } from "./ResultGrid";
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
  /** Opens the tab's export dialog for a result's statement; stable across renders. */
  onExport: (sql: string) => void;
}

/**
 * The results of the tab's latest run (spec §4.1): a tab per result set, titled by its statement, plus Messages.
 * Until a new run delivers its first result, the previous grids stay, dimmed. A grid mounts the first time its tab is
 * shown and then stays mounted while another tab is shown, so its scroll and widths survive switching (the export
 * dialog belongs to the query tab). Without a choice by the user, the first result is shown, or Messages once a statement failed.
 */
export function ResultsArea({
  run,
  executions,
  controller,
  phase,
  dialect,
  pageSize,
  onExport,
  ...messages
}: ResultsAreaProps) {
  const tabs = useMemo(() => resultTabs(run, executions), [run, executions]);
  const held = useHeldResult(tabs.length ? tabs : null, !!run?.active);
  const shown = held.value ?? [];
  const [picked, setPicked] = useState<{ runId: string; key: string } | null>(null);
  /** Tabs shown at least once: only their grids are mounted (a 200-statement script mounts what is looked at). */
  const [visited, setVisited] = useState<ReadonlySet<string>>(() => new Set());

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
  if (selected !== MESSAGES && !visited.has(selected)) setVisited(new Set(visited).add(selected));
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
          if (tab.key !== selected && !visited.has(tab.key)) return null;
          const { execution, set } = tab;
          return (
            <ResultGrid
              key={tab.key}
              set={set}
              requestId={execution.requestId}
              sql={execution.sql}
              loadingMore={execution.status === "running" && execution.resultSets.at(-1)?.index === set.index}
              stale={held.stale}
              canLoadMore={canLoadMore}
              exportable={!run.active}
              onExport={onExport}
              controller={controller}
              pageSize={pageSize}
              dialect={dialect}
              hidden={tab.key !== selected}
            />
          );
        })}
        {selected === MESSAGES && <RunMessages run={run} executions={executions} {...messages} />}
      </div>
    </div>
  );
}
