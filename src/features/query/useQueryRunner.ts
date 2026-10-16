import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { toast } from "@/components/ui/Toast";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import type { AppSettings, GuardDecision } from "@/lib/api/types";
import { CONNECTION_LOST, type ExecutionState } from "../session/execution";
import { busy, type SessionController } from "../session/SessionController";
import type { PlannedStatement } from "./runPlan";

export type StepStatus = "pending" | "running" | "done" | "error" | "cancelled" | "skipped";

export interface RunStep {
  statement: PlannedStatement;
  /** The execute request of this statement (the latest one, if the backend asked to confirm it again). */
  requestId: string | null;
  status: StepStatus;
}

export interface RunState {
  id: string;
  steps: RunStep[];
  /** Statements are still being sent or awaited. */
  active: boolean;
  startedAt: number;
  finishedAt: number | null;
}

/** A statement the safety policy flagged: it needs the user's confirmation (`confirm`) or cannot run (`block`). */
export interface FlaggedStatement {
  index: number;
  statement: PlannedStatement;
  decision: GuardDecision;
}

/** A confirmation the runner waits for. `before`: nothing was sent yet; `during`: the backend asked mid-run. */
export interface ConfirmPrompt {
  when: "before" | "during";
  items: FlaggedStatement[];
  answer(run: boolean): void;
}

/** What the editor does as a run goes (flash, error marks); called synchronously by the runner. */
export interface RunListener {
  onStart(planned: readonly PlannedStatement[]): void;
  onStatement(index: number): void;
  onSettled(index: number, execution: ExecutionState): void;
}

export interface QueryRunnerArgs {
  controller: SessionController | null;
  profileId: string | null;
  settings: AppSettings;
  /** The per-execute timeout; null: none. */
  timeoutS: number | null;
  listener?: RunListener;
}

/** Errors after which nothing more can run on this session, whatever `continue_on_error` says. */
const FATAL_CODES = new Set(["not_connected", CONNECTION_LOST]);

async function analyze(profileId: string, statements: string[]): Promise<GuardDecision[]> {
  const client = await api();
  return unwrap(await client.POST("/sql/analyze", { body: { profile_id: profileId, statements } }));
}

function stepStatus(execution: ExecutionState | null): StepStatus {
  if (!execution) return "error"; // forgotten under the run: should not happen
  if (execution.skipped) return "skipped"; // the backend wanted a confirmation that was not given
  if (execution.cancelled && !execution.timedOut) return "cancelled";
  if (execution.error || execution.timedOut || execution.interrupted) return "error";
  return "done";
}

const isFatal = (execution: ExecutionState | null) =>
  !execution || execution.interrupted || FATAL_CODES.has(execution.error?.code ?? "");

/** The run in progress: what cancel needs to reach it. */
interface ActiveRun {
  controller: SessionController;
  requestId: string | null;
  cancelled: boolean;
}

/**
 * Runs planned statements on the tab's session (spec §4.1): one `/sql/analyze` for all of them and one confirmation
 * dialog for the flagged ones up front, then one execute per statement, each awaited until it is done or paused.
 * An error stops the run unless `run.continue_on_error`; cancel stops the running statement and the run. A new run
 * forgets the previous run's results. The run belongs to the controller it ran on: a new session starts empty.
 */
export function useQueryRunner({ controller, profileId, settings, timeoutS, listener }: QueryRunnerArgs) {
  const [result, setResult] = useState<{ controller: SessionController; run: RunState } | null>(null);
  const [prompt, setPrompt] = useState<ConfirmPrompt | null>(null);
  const latest = useRef({ controller, profileId, settings, timeoutS, listener });
  useLayoutEffect(() => {
    latest.current = { controller, profileId, settings, timeoutS, listener };
  });
  const active = useRef<ActiveRun | null>(null);

  const ask = useCallback(
    (when: ConfirmPrompt["when"], items: FlaggedStatement[]) =>
      new Promise<boolean>((resolve) => {
        let answered = false;
        const answer = (run: boolean) => {
          if (answered) return;
          answered = true;
          setPrompt(null);
          resolve(run);
        };
        setPrompt({ when, items, answer });
      }),
    [],
  );

  /** The statements the user agreed to (by index), or null when nothing may run. */
  const confirmUpFront = useCallback(
    async (profile: string, planned: PlannedStatement[]): Promise<Set<number> | null> => {
      let decisions: GuardDecision[];
      try {
        decisions = await analyze(
          profile,
          planned.map((s) => s.sql),
        );
      } catch (error) {
        toast("error", `Could not check the statements before running them: ${errorMessage(error)}`);
        return null;
      }
      const flagged = decisions.flatMap((decision, index) =>
        decision.action === "run" ? [] : [{ index, statement: planned[index], decision }],
      );
      if (flagged.length === 0) return new Set();
      const agreed = await ask("before", flagged);
      if (!agreed || flagged.some((f) => f.decision.action === "block")) return null;
      return new Set(flagged.map((f) => f.index));
    },
    [ask],
  );

  const execute = useCallback(
    async (job: ActiveRun, planned: PlannedStatement[], confirmed: Set<number>) => {
      const { controller: session } = job;
      const { settings: current, timeoutS: timeout, listener: events } = latest.current;
      let state: RunState = {
        id: crypto.randomUUID(),
        steps: planned.map((statement) => ({ statement, requestId: null, status: "pending" })),
        active: true,
        startedAt: Date.now(),
        finishedAt: null,
      };
      const publish = (next: RunState) => {
        state = next;
        setResult({ controller: session, run: next });
      };
      const setStep = (index: number, patch: Partial<RunStep>) =>
        publish({ ...state, steps: state.steps.map((step, i) => (i === index ? { ...step, ...patch } : step)) });
      const send = async (index: number, agreed: boolean): Promise<ExecutionState | null> => {
        const requestId = session.execute(planned[index].sql, {
          pageSize: current.run.page_size,
          timeoutS: timeout,
          confirmed: agreed,
          history: true,
        });
        job.requestId = requestId;
        setStep(index, { requestId, status: "running" });
        try {
          return await session.whenSettled(requestId);
        } catch {
          return null;
        }
      };

      session.forgetAllExcept([]); // only this run's results are kept from now on
      publish(state);
      events?.onStart(planned);
      for (let index = 0; index < planned.length && !job.cancelled; index += 1) {
        events?.onStatement(index);
        let execution = await send(index, confirmed.has(index));
        if (execution?.skipped && execution.confirmation && !job.cancelled) {
          // The policy changed since the analysis (e.g. the profile was edited): ask for this statement alone.
          const { reasons, classification } = execution.confirmation;
          const item = {
            index,
            statement: planned[index],
            decision: { action: "confirm" as const, reasons, classification },
          };
          if (await ask("during", [item])) {
            session.forget(execution.requestId);
            execution = await send(index, true);
          }
        }
        const status = stepStatus(execution);
        setStep(index, { status });
        if (execution) events?.onSettled(index, execution);
        if (status === "cancelled" || status === "skipped") break;
        if (status === "error" && (!current.run.continue_on_error || isFatal(execution))) break;
      }
      publish({
        ...state,
        steps: state.steps.map((step) => (step.status === "pending" ? { ...step, status: "skipped" } : step)),
        active: false,
        finishedAt: Date.now(),
      });
    },
    [ask],
  );

  const start = useCallback(
    async (planned: PlannedStatement[]): Promise<void> => {
      const { controller: session, profileId: profile } = latest.current;
      if (!session || !profile || planned.length === 0 || active.current) return;
      if (busy(session.store.getState())) return;
      const job: ActiveRun = { controller: session, requestId: null, cancelled: false };
      active.current = job;
      try {
        const confirmed = await confirmUpFront(profile, planned);
        if (confirmed && !job.cancelled) await execute(job, planned, confirmed);
      } finally {
        active.current = null;
      }
    },
    [confirmUpFront, execute],
  );

  const cancel = useCallback(() => {
    const job = active.current;
    if (!job) return;
    job.cancelled = true;
    if (job.requestId) job.controller.cancel(job.requestId);
  }, []);

  const run = result && result.controller === controller ? result.run : null;
  return { run, prompt, start, cancel };
}
