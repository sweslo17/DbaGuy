import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { toast } from "@/components/ui/Toast";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import type { AppSettings, GuardDecision, QueryContext } from "@/lib/api/types";
import { refreshHistory } from "../history/useHistory";
import { CONNECTION_LOST, NOT_CONNECTED_CODE, type ExecutionState } from "../session/execution";
import { busy, type SessionController } from "../session/SessionController";
import { expandRepeats, sentText, type PlannedStatement } from "./runPlan";

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
  /** The user cancelled it (or the tab closing stopped it). */
  cancelled: boolean;
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
  /**
   * Couchbase: the tab's effective query context (null: none), read when a run starts and sent with each of its
   * statements. Undefined for engines without a context: the session's default (none) goes out as before.
   */
  queryContext?: QueryContext | null;
  listener?: RunListener;
}

/** The most times `GO n` may send a batch: a typo such as `GO 100000` would otherwise hold the session for hours. */
const MAX_GO_REPEAT = 1000;

/** Errors after which nothing more can run on this session, whatever `continue_on_error` says. */
const FATAL_CODES = new Set([NOT_CONNECTED_CODE, CONNECTION_LOST]);

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

export interface StartOptions {
  /**
   * Whether the editor still holds the text the statements were planned from; checked just before the first statement
   * is sent. Their offsets (flash, marks, jumps) are only right for that text.
   */
  unchanged?: () => boolean;
  /** Whether the statements are recorded in the History (default true); Explain's are not (ruling B27). */
  history?: boolean;
  /** Couchbase Enterprise: each statement also returns its execution profile, as a further result set. */
  profilePlan?: boolean;
}

/** The run in progress: what cancel needs to reach it, and the context and options it was started with. */
interface ActiveRun {
  controller: SessionController;
  requestId: string | null;
  cancelled: boolean;
  /** The tab's query context when the run started: a change of the picker meanwhile does not reach its statements. */
  queryContext: QueryContext | null | undefined;
  history: boolean;
  profilePlan: boolean;
}

/**
 * Runs planned statements on the tab's session (spec §4.1): one `/sql/analyze` for all of them and one confirmation
 * dialog for the flagged ones up front, then one execute per statement, each awaited until it is done or paused
 * (`GO n` sends its batch n times, each an execute of its own), with the parameter values the tab bound to it
 * (`PlannedStatement.values`). What is analysed and sent is `sentText` of the statement (Explain's prefix included).
 * An error stops the run unless `run.continue_on_error`; cancel stops the running statement and the run. A new run
 * forgets the previous run's results. The run belongs to the controller it ran on: a new session starts empty.
 */
export function useQueryRunner({ controller, profileId, settings, timeoutS, queryContext, listener }: QueryRunnerArgs) {
  const [result, setResult] = useState<{ controller: SessionController; run: RunState } | null>(null);
  const [prompt, setPrompt] = useState<ConfirmPrompt | null>(null);
  /** The statements are being checked (`/sql/analyze`) before any is sent: a long script takes a moment. */
  const [checking, setChecking] = useState(false);
  const qc = useQueryClient();
  const latest = useRef({ controller, profileId, settings, timeoutS, queryContext, listener });
  useLayoutEffect(() => {
    latest.current = { controller, profileId, settings, timeoutS, queryContext, listener };
  });
  const active = useRef<ActiveRun | null>(null);
  /** The open confirmation's answer: a tab closed while it is open answers "no", so the run never hangs. */
  const pending = useRef<((run: boolean) => void) | null>(null);
  useEffect(() => () => pending.current?.(false), []);

  const ask = useCallback(
    (when: ConfirmPrompt["when"], items: FlaggedStatement[]) =>
      new Promise<boolean>((resolve) => {
        let answered = false;
        const answer = (run: boolean) => {
          if (answered) return;
          answered = true;
          if (pending.current === answer) pending.current = null;
          setPrompt(null);
          resolve(run);
        };
        pending.current = answer;
        setPrompt({ when, items, answer });
      }),
    [],
  );

  /** The statements the user agreed to (by index), or null when nothing may run (or the run was cancelled). */
  const confirmUpFront = useCallback(
    async (job: ActiveRun, profile: string, planned: PlannedStatement[]): Promise<Set<number> | null> => {
      let decisions: GuardDecision[];
      setChecking(true);
      try {
        decisions = await analyze(profile, planned.map(sentText));
      } catch (error) {
        toast("error", `Could not check the statements before running them: ${errorMessage(error)}`);
        return null;
      } finally {
        setChecking(false);
      }
      const flagged = decisions.flatMap((decision, index) =>
        decision.action === "run" ? [] : [{ index, statement: planned[index], decision }],
      );
      if (job.cancelled) return null; // cancelled while checking: no confirmation is asked for
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
        cancelled: false,
      };
      const publish = (next: RunState) => {
        state = next;
        setResult({ controller: session, run: next });
      };
      const setStep = (index: number, patch: Partial<RunStep>) =>
        publish({ ...state, steps: state.steps.map((step, i) => (i === index ? { ...step, ...patch } : step)) });
      const send = async (index: number, agreed: boolean): Promise<ExecutionState | null> => {
        const requestId = session.execute(sentText(planned[index]), {
          pageSize: current.run.page_size,
          timeoutS: timeout,
          confirmed: agreed,
          history: job.history,
          queryContext: job.queryContext,
          params: planned[index].values ?? null,
          profilePlan: job.profilePlan,
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
      let last: ExecutionState | null = null;
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
        last = execution;
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
        cancelled: job.cancelled,
      });
      // The backend records each statement in the history as it ends, before its `finished`. A paused last result
      // ends later (read to the end, discarded or closed): the History panel refreshes again then.
      refreshHistory(qc);
      if (last?.status === "paused") void session.whenDone(last.requestId).then(() => refreshHistory(qc), () => {});
    },
    [ask, qc],
  );

  const start = useCallback(
    async (planned: PlannedStatement[], options: StartOptions = {}): Promise<void> => {
      const { controller: session, profileId: profile, queryContext: context } = latest.current;
      if (!session || !profile || planned.length === 0 || active.current) return;
      if (busy(session.store.getState())) return;
      if (planned.some((s) => s.repeat > MAX_GO_REPEAT)) {
        toast("error", `GO n runs a batch at most ${MAX_GO_REPEAT.toLocaleString("en-US")} times in DbaGuy`);
        return;
      }
      // A `GO 0` batch is never sent, so it is neither checked nor confirmed.
      const sent = planned.filter((s) => s.repeat > 0);
      if (sent.length === 0) {
        toast("info", "GO 0 runs the batch zero times");
        return;
      }
      const job: ActiveRun = {
        controller: session,
        requestId: null,
        cancelled: false,
        queryContext: context,
        history: options.history ?? true,
        profilePlan: options.profilePlan ?? false,
      };
      active.current = job;
      try {
        const confirmed = await confirmUpFront(job, profile, sent);
        if (!confirmed || job.cancelled) return;
        if (options.unchanged && !options.unchanged()) {
          // Typed while the statements were being checked: their offsets no longer match the text.
          toast("warning", "The text changed — run again");
          return;
        }
        const expanded = expandRepeats(sent, confirmed);
        await execute(job, expanded.statements, expanded.confirmed);
      } finally {
        active.current = null;
      }
    },
    [confirmUpFront, execute],
  );

  /** Stops the run: the running statement is cancelled; while the statements are being checked, none is sent. */
  const cancel = useCallback(() => {
    const job = active.current;
    if (!job) return;
    job.cancelled = true;
    if (job.requestId) job.controller.cancel(job.requestId);
  }, []);

  /**
   * Sends no further statement, so a COMMIT chosen in the close guards never commits only part of the script. The
   * running statement is left to finish on every engine (R-03a-F4): this runs before the guards ask anything, so a
   * cancel here would lose it even if the user then answers Cancel, and a Commit would silently leave it out. Closing
   * the session interrupts it anyway.
   */
  const stop = useCallback(() => {
    if (active.current) active.current.cancelled = true;
  }, []);

  const run = result && result.controller === controller ? result.run : null;
  return { run, prompt, checking, start, cancel, stop };
}
