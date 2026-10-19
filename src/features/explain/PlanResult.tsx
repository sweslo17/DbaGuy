import { memo, useCallback, useMemo, useState, type ReactNode } from "react";
import { errorMessage } from "@/lib/api/client";
import type { PlanView as PlanViewData, QueryContext } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import type { ExecutionState, ResultSetState } from "../session/execution";
import { useAdvice, usePlanView } from "./planApi";
import { adviceStatement, planSource, UNKNOWN_VIEW } from "./planModel";
import { PlanNotice, PlanView, type AdviceControl } from "./PlanView";

const COMMUNITY_ADVICE = "Community Edition does not give index advice";
const TOO_LARGE = "The plan is too large to draw; switch to Grid or export it as JSON";

type Mode = "plan" | "grid";
const MODES: { value: Mode; label: string }[] = [
  { value: "plan", label: "Plan" },
  { value: "grid", label: "Grid" },
];

/** `Plan | Grid`: the same result as a drawing or as the grid's one cell. */
function ViewSwitch({ value, onChange }: { value: Mode; onChange: (mode: Mode) => void }) {
  return (
    <div role="tablist" aria-label="Result view" className="inline-flex rounded-md border border-border p-px">
      {MODES.map((mode) => (
        <button
          key={mode.value}
          type="button"
          role="tab"
          aria-selected={value === mode.value}
          onClick={() => onChange(mode.value)}
          className={cn(
            "h-5 rounded px-2 text-[12px] outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
            value === mode.value ? "bg-selected text-fg" : "text-muted hover:text-fg",
          )}
        >
          {mode.label}
        </button>
      ))}
    </div>
  );
}

/**
 * A plan view with its index advice: the advice is asked for here and kept by the plan it was asked for (the caller
 * keys this by request and set, so another plan starts without one). Memoised on what it is given, none of which
 * changes while the session sends events for other statements.
 */
const PlanPane = memo(function PlanPane({
  view,
  statement,
  queryContext,
  profileId,
  edition,
}: {
  view: PlanViewData;
  /** The statement advice is asked for. */
  statement: string;
  queryContext: QueryContext | null;
  profileId: string;
  edition: "enterprise" | "community" | null;
}) {
  const advise = useAdvice();
  const { mutate } = advise;
  const request = useCallback(
    () => mutate({ profileId, statement, queryContext }),
    [mutate, profileId, statement, queryContext],
  );
  const community = edition === "community";
  const error = advise.error ? errorMessage(advise.error) : null;
  const control = useMemo<AdviceControl>(
    () => ({
      available: !community,
      reason: community ? COMMUNITY_ADVICE : undefined,
      pending: advise.isPending,
      result: advise.data ?? null,
      error,
      request,
    }),
    [community, advise.isPending, advise.data, error, request],
  );
  return <PlanView view={view} advice={control} />;
});

export interface PlanResultProps {
  /** A result set of the `plan` presentation: one row, one cell holding the plan's JSON text. */
  set: ResultSetState;
  execution: ExecutionState;
  profileId: string;
  /** The server's edition (Couchbase): Community Edition gives no index advice. Null while unknown. */
  edition: "enterprise" | "community" | null;
  /** The set as the grid shows it. */
  grid: ReactNode;
  /** The rows belong to the previous run (shown dimmed until the new run delivers a result). */
  stale?: boolean;
}

/**
 * A `plan` result set of Couchbase (EXPLAIN, ADVISE, the execution profile), switchable between the plan view and the
 * grid (spec §3.10, §3.13). The cell's text goes to the backend for analysis; a plan too large to be sent whole, or one
 * the backend cannot read, is not drawn and the grid still has it. Advice is asked for the statement the plan is of,
 * in the context the request ran in.
 */
export function PlanResult({ set, execution, profileId, edition, grid, stale = false }: PlanResultProps) {
  const source = useMemo(() => planSource(set.rows), [set.rows]);
  const analysed = usePlanView(source.kind === "text" ? source.text : null);
  const [mode, setMode] = useState<Mode>("plan");
  /** The grid mounts the first time it is shown and then stays, so its scroll and widths survive the switch. */
  const [gridShown, setGridShown] = useState(false);
  const choose = (next: Mode) => {
    setMode(next);
    if (next === "grid") setGridShown(true);
  };

  let plan: ReactNode;
  if (source.kind === "truncated") plan = <PlanNotice>{TOO_LARGE}</PlanNotice>;
  else if (source.kind === "unreadable") plan = <PlanView view={UNKNOWN_VIEW} />;
  else if (source.kind === "none") {
    plan =
      execution.status === "done" ? (
        <PlanNotice>The server returned no plan</PlanNotice>
      ) : (
        <div className="h-full animate-delayed-show">
          <PlanNotice>Loading…</PlanNotice>
        </div>
      );
  } else if (analysed.isError) {
    plan = <PlanNotice tone="danger">{`Could not read the plan: ${errorMessage(analysed.error)}`}</PlanNotice>;
  } else if (!analysed.data) {
    plan = (
      <div className="h-full animate-delayed-show">
        <PlanNotice>Reading the plan…</PlanNotice>
      </div>
    );
  } else {
    plan = (
      <PlanPane
        key={`${execution.requestId}:${set.index}`}
        view={analysed.data}
        statement={adviceStatement(analysed.data, execution.sql)}
        queryContext={execution.request.queryContext ?? null}
        profileId={profileId}
        edition={edition}
      />
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-8 shrink-0 items-center border-b border-border px-2">
        <ViewSwitch value={mode} onChange={choose} />
      </div>
      <div className="relative min-h-0 flex-1">
        <div className={cn("h-full", mode !== "plan" && "hidden", stale && "opacity-50")}>{plan}</div>
        {(gridShown || mode === "grid") && <div className={cn("h-full", mode !== "grid" && "hidden")}>{grid}</div>}
      </div>
    </div>
  );
}
