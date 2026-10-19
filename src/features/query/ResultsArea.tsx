import { useCallback, useMemo, useState } from "react";
import type { Dialect } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { PlanResult } from "../explain/PlanResult";
import { planSetLabel } from "../explain/planModel";
import { sameElements, useHeldResult } from "../grid/useHeldResult";
import type { ExecutionState, ResultSetState } from "../session/execution";
import type { RequestOptions, SessionController, SessionPhase } from "../session/SessionController";
import { ResultGrid } from "./ResultGrid";
import { ResultTabStrip, type StripTab } from "./ResultTabStrip";
import { RunMessages, type RunMessagesProps } from "./RunMessages";
import type { RunState, RunStep } from "./useQueryRunner";

const MESSAGES = "messages";
const MESSAGES_TAB: StripTab = { value: MESSAGES, label: "Messages" };
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

const NO_TABS: ResultTab[] = [];

/**
 * An execution's result tabs, built once for each state of it: an event for another statement keeps them as they are,
 * so the list compares element by element (`sameElements`). An execution belongs to one step of one run.
 */
const tabsOfExecution = new WeakMap<ExecutionState, ResultTab[]>();

function executionTabs(step: RunStep, stepIndex: number, execution: ExecutionState): ResultTab[] {
  if (!execution.resultSets.length) return NO_TABS;
  let tabs = tabsOfExecution.get(execution);
  if (!tabs) {
    const title = statementTitle(step.statement.sql);
    const labelOf = (set: ResultSetState, ordinal: number) =>
      set.presentation === "plan" ? planSetLabel(execution, ordinal) : ordinal === 0 ? title : `Result ${ordinal + 1}`;
    tabs = execution.resultSets.map((set, ordinal) => ({
      key: `${stepIndex}:${set.index}`,
      label: labelOf(set, ordinal),
      statementSql: step.statement.sql,
      execution,
      set,
    }));
    tabsOfExecution.set(execution, tabs);
  }
  return tabs;
}

function resultTabs(run: RunState | null, executions: Record<string, ExecutionState>): ResultTab[] {
  if (!run) return NO_TABS;
  return run.steps.flatMap((step, stepIndex) => {
    const execution = step.requestId ? executions[step.requestId] : undefined;
    return execution ? executionTabs(step, stepIndex, execution) : NO_TABS;
  });
}

/** Couchbase: what a result of the `plan` presentation is drawn with (`EngineInfo.plan_format`). */
export interface PlanSupport {
  profileId: string;
  /** The server's edition (the session reports it): Community Edition gives no index advice. */
  edition: "enterprise" | "community" | null;
}

export interface ResultsAreaProps extends Omit<RunMessagesProps, "run"> {
  run: RunState | null;
  controller: SessionController | null;
  phase: SessionPhase;
  dialect: Dialect | null;
  pageSize: number;
  /** The connection's engine draws plans (Couchbase): a `plan` set shows the plan view, switchable to the grid. */
  plans: PlanSupport | null;
  /** Opens the tab's export dialog for a result's statement and what it was sent with; stable across renders. */
  onExport: (sql: string, request: RequestOptions) => void;
}

/**
 * The results of the tab's latest run (spec §4.1): a tab per result set, titled by its statement, plus Messages.
 * Until a new run delivers its first result, the previous grids stay, dimmed. A grid mounts the first time its tab is
 * shown and then stays mounted while another tab is shown, so its scroll and widths survive switching (the export
 * dialog belongs to the query tab). Without a choice by the user, the first result is shown, or Messages once a statement failed.
 * A set of the `plan` presentation (Couchbase's EXPLAIN, ADVISE and execution profile) is drawn as a plan, with the
 * grid a switch away; it is labelled Plan, or Profile when it is the execution profile a run asked for.
 */
export function ResultsArea({
  run,
  executions,
  controller,
  phase,
  dialect,
  pageSize,
  plans,
  onExport,
  ...messages
}: ResultsAreaProps) {
  const tabs = useMemo(() => resultTabs(run, executions), [run, executions]);
  // Kept as it is while its elements are (an event for a statement without a result changes none).
  const held = useHeldResult(tabs.length ? tabs : null, !!run?.active, sameElements);
  const shown = held.value ?? NO_TABS;
  const strip = useMemo(
    () => shown.map((tab) => ({ value: tab.key, label: tab.label, title: tab.statementSql.slice(0, 1000) })),
    [shown],
  );
  const [picked, setPicked] = useState<{ runId: string; key: string } | null>(null);
  const runId = run?.id ?? null;
  const pick = useCallback(
    (key: string) => {
      if (runId !== null) setPicked({ runId, key });
    },
    [runId],
  );
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
      <ResultTabStrip tabs={strip} pinned={MESSAGES_TAB} value={selected} onValueChange={pick} />
      <div className="relative min-h-0 flex-1">
        {shown.map((tab) => {
          if (tab.key !== selected && !visited.has(tab.key)) return null;
          const { execution, set } = tab;
          const drawsPlan = plans !== null && set.presentation === "plan";
          const grid = (
            <ResultGrid
              key={tab.key}
              set={set}
              requestId={execution.requestId}
              sql={execution.sql}
              request={execution.request}
              loadingMore={execution.status === "running" && execution.resultSets.at(-1)?.index === set.index}
              stale={held.stale}
              canLoadMore={canLoadMore}
              exportable={!run.active}
              onExport={onExport}
              controller={controller}
              pageSize={pageSize}
              dialect={dialect}
              hidden={!drawsPlan && tab.key !== selected}
            />
          );
          if (!drawsPlan) return grid;
          return (
            <div key={tab.key} className={cn("h-full", tab.key !== selected && "hidden")}>
              <PlanResult
                set={set}
                execution={execution}
                profileId={plans.profileId}
                edition={plans.edition}
                grid={grid}
                stale={held.stale}
              />
            </div>
          );
        })}
        {selected === MESSAGES && <RunMessages run={run} executions={executions} {...messages} />}
      </div>
    </div>
  );
}
