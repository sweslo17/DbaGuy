import { useCallback, useEffect, useEffectEvent, useMemo, useRef, useState } from "react";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import type { PathSegment } from "@/lib/api/types";
import type { ObjectTab } from "@/stores/workspace";
import { INTERRUPTED_MESSAGE, type ExecutionState } from "../session/execution";
import {
  PAGE_SIZE,
  type SessionController,
  type SessionPhase,
  type SessionSnapshot,
} from "../session/SessionController";
import { useSession } from "../session/useSession";
import { clearFilterHistory, readFilterHistory, rememberFilter } from "./filterHistory";
import { relationOf, type Relation } from "./filterValue";

export interface SortKey {
  column: string;
  descending: boolean;
}

export interface TableDataState {
  /** Applied WHERE text ("" = none). */
  filter: string;
  sorts: SortKey[];
  /** The generated browse SELECT. */
  sql: string | null;
  requestId: string | null;
  /** browse-select (422) or execution error. */
  error: string | null;
  count: { status: "idle" | "running" | "done" | "error"; value?: number; error?: string };
}

/** A statement for a session; `run` changes on every request, so an identical one runs again (Refresh). */
interface Job {
  run: number;
  sql: string;
}

/** The applied browse: what the grid shows (or is loading). */
interface BrowseJob extends Job {
  filter: string;
  sorts: SortKey[];
}

/** A count asked for `filter`; `sql` is null while it is being built. */
interface CountJob {
  run: number;
  filter: string;
  sql: string | null;
  error: string | null;
}

async function buildSelect(
  profileId: string,
  path: PathSegment[],
  where: string,
  sorts: SortKey[],
  count: boolean,
): Promise<string> {
  const body = { profile_id: profileId, path, where: where || null, order_by: sorts, count };
  return unwrap(await (await api()).POST("/sql/browse-select", { body })).sql;
}

/** One column's sort cycle: ascending → descending → none. */
function cycle(key: SortKey | undefined, column: string): SortKey | null {
  if (!key) return { column, descending: false };
  return key.descending ? null : { column, descending: true };
}

/**
 * Header sort control: without `additive` the sort becomes this column alone; with it, the column is added (or cycled
 * in place).
 */
function nextSorts(sorts: SortKey[], column: string, additive: boolean): SortKey[] {
  const current = sorts.find((key) => key.column === column);
  const next = cycle(current, column);
  if (!additive) return next ? [next] : [];
  if (!current) return next ? [...sorts, next] : sorts;
  return sorts.flatMap((key) => (key.column !== column ? [key] : next ? [next] : []));
}

/** Ends a superseded request so that it neither holds the connection (a paused page) nor keeps its rows. */
function endRequest(controller: SessionController, requestId: string): void {
  const status = controller.store.getState().executions[requestId]?.status;
  if (status === "running") controller.cancel(requestId);
  else controller.discard(requestId);
  controller.forget(requestId);
}

/**
 * Sends `job` once the session is open — again after a retry (a new controller) — ending the request sent before it.
 * Returns the request sent last with the job it ran; it can belong to an older job until the new one is sent.
 */
function useSessionJob<J extends Job>(
  controller: SessionController | null,
  phase: SessionPhase | undefined,
  job: J | null,
): { requestId: string; job: J } | null {
  const sent = useRef<{ controller: SessionController; run: number; requestId: string } | null>(null);
  const [current, setCurrent] = useState<{ requestId: string; job: J } | null>(null);
  useEffect(() => {
    if (!controller || phase !== "open" || !job) return;
    const previous = sent.current?.controller === controller ? sent.current : null;
    if (previous?.run === job.run) return;
    if (previous) endRequest(controller, previous.requestId);
    const requestId = controller.execute(job.sql, { history: false, pageSize: PAGE_SIZE });
    sent.current = { controller, run: job.run, requestId };
    setCurrent({ requestId, job });
  }, [controller, phase, job]);
  return current;
}

function countState(
  job: CountJob | null,
  filter: string,
  snapshot: SessionSnapshot | null,
  sent: { requestId: string; job: Job } | null,
): TableDataState["count"] {
  if (!job || job.filter !== filter) return { status: "idle" };
  if (job.error) return { status: "error", error: job.error };
  if (snapshot?.phase === "failed") return { status: "error", error: snapshot.error?.message ?? "Not connected" };
  const execution = sent?.job.run === job.run ? snapshot?.executions[sent.requestId] : undefined;
  if (!execution || execution.status !== "done") return { status: "running" };
  if (execution.error) return { status: "error", error: execution.error.message };
  if (execution.cancelled) return { status: "idle" };
  if (execution.interrupted) return { status: "error", error: INTERRUPTED_MESSAGE };
  const value = Number(execution.resultSets[0]?.rows[0]?.[0]);
  return Number.isFinite(value) ? { status: "done", value } : { status: "error", error: "No count returned" };
}

/**
 * Browse state of a table data tab (spec §4.5): filter, sorts, the generated SELECT and its paged result on the tab's
 * own session, opened the first time `active` (the Data view is shown) and kept while the tab is open. Count rows runs
 * on a second session, opened on first use (ruling R-02b-2), so the paused page keeps loading more.
 */
export function useTableData(tab: ObjectTab, active: boolean) {
  const { profileId, path } = tab;
  const relation: Relation | null = useMemo(() => relationOf(path), [path]);
  const database = relation?.database ?? null;
  const [shown, setShown] = useState(active);
  if (active && !shown) setShown(true);
  const [countRequested, setCountRequested] = useState(false);
  const data = useSession(profileId, database, { enabled: shown });
  const counting = useSession(profileId, database, { enabled: countRequested });
  const { controller: countController, snapshot: countSnapshot, retry: retryCount } = counting;

  const [browseJob, setBrowseJob] = useState<BrowseJob | null>(null);
  const [buildError, setBuildError] = useState<string | null>(null);
  const [countJob, setCountJob] = useState<CountJob | null>(null);
  const [history, setHistory] = useState(() => readFilterHistory(profileId, path));
  const builds = useRef(0);
  const countRuns = useRef(0);

  const sentBrowse = useSessionJob(data.controller, data.snapshot?.phase, browseJob);
  const countReady = useMemo(() => (countJob?.sql ? { run: countJob.run, sql: countJob.sql } : null), [countJob]);
  const sentCount = useSessionJob(countController, countSnapshot?.phase, countReady);
  const execution: ExecutionState | null = sentBrowse
    ? (data.snapshot?.executions[sentBrowse.requestId] ?? null)
    : null;
  const filter = browseJob?.filter ?? "";

  /** Builds the SELECT for `filter` and `sorts` and makes it the applied browse; a rejected filter changes nothing. */
  const browse = useCallback(
    (nextFilter: string, sorts: SortKey[]) => {
      const ticket = ++builds.current;
      buildSelect(profileId, path, nextFilter, sorts, false).then(
        (sql) => {
          if (ticket !== builds.current) return;
          setBuildError(null);
          setBrowseJob((job) => ({ run: (job?.run ?? 0) + 1, sql, filter: nextFilter, sorts }));
        },
        (error: unknown) => {
          if (ticket === builds.current) setBuildError(errorMessage(error));
        },
      );
    },
    [profileId, path],
  );

  const started = useRef(false);
  useEffect(() => {
    if (!shown || started.current) return;
    started.current = true;
    browse("", []);
  }, [shown, browse]);

  // Remember a filter once it produced a result: a typo the database rejected is not worth keeping.
  const succeeded = !!execution && execution.resultSets.length > 0 && !execution.error;
  const succeededFilter = succeeded && sentBrowse ? sentBrowse.job.filter : "";
  useEffect(() => {
    if (succeededFilter) setHistory(rememberFilter(profileId, path, succeededFilter));
  }, [succeededFilter, sentBrowse?.requestId, profileId, path]);

  // A count made for another filter no longer applies: stop it.
  const dropStaleCount = useEffectEvent(() => {
    if (!countJob || countJob.filter === filter) return;
    if (sentCount) countController?.cancel(sentCount.requestId);
    setCountJob(null);
  });
  useEffect(() => {
    dropStaleCount();
  }, [filter]);

  const applyFilter = useCallback(
    (text: string) => browse(text.trim(), browseJob?.sorts ?? []),
    [browse, browseJob?.sorts],
  );
  const toggleSort = useCallback(
    (column: string, additive: boolean) => browse(filter, nextSorts(browseJob?.sorts ?? [], column, additive)),
    [browse, filter, browseJob?.sorts],
  );
  const sortBy = useCallback(
    (column: string, descending: boolean) => browse(filter, [{ column, descending }]),
    [browse, filter],
  );
  /** Runs the applied browse again (or builds the first one if that failed). */
  const refresh = useCallback(() => {
    setBuildError(null);
    if (browseJob) setBrowseJob((job) => job && { ...job, run: job.run + 1 });
    else browse("", []);
  }, [browse, browseJob]);
  /** Stops a browse that has not delivered its first page yet. */
  const cancel = useCallback(() => {
    if (sentBrowse) data.controller?.cancel(sentBrowse.requestId);
  }, [data.controller, sentBrowse]);
  const loadMore = useCallback(() => {
    if (sentBrowse) data.controller?.fetchMore(sentBrowse.requestId, PAGE_SIZE);
  }, [data.controller, sentBrowse]);
  const countRows = useCallback(() => {
    if (!browseJob) return;
    if (countSnapshot?.phase === "failed") retryCount();
    setCountRequested(true);
    const run = ++countRuns.current;
    const countFilter = browseJob.filter;
    setCountJob({ run, filter: countFilter, sql: null, error: null });
    const settle = (patch: Partial<CountJob>) => setCountJob((job) => (job?.run === run ? { ...job, ...patch } : job));
    buildSelect(profileId, path, countFilter, [], true).then(
      (sql) => settle({ sql }),
      (error: unknown) => settle({ error: errorMessage(error) }),
    );
  }, [browseJob, countSnapshot?.phase, retryCount, profileId, path]);
  const cancelCount = useCallback(() => {
    if (sentCount) countController?.cancel(sentCount.requestId);
    setCountJob(null);
  }, [countController, sentCount]);
  const clearHistory = useCallback(() => {
    clearFilterHistory(profileId, path);
    setHistory([]);
  }, [profileId, path]);
  const clearFilterError = useCallback(() => setBuildError(null), []);

  const state: TableDataState = {
    filter,
    sorts: browseJob?.sorts ?? [],
    sql: browseJob?.sql ?? null,
    requestId: sentBrowse?.requestId ?? null,
    error: buildError ?? execution?.error?.message ?? (execution?.interrupted ? INTERRUPTED_MESSAGE : null),
    count: countState(countJob, filter, countSnapshot, sentCount),
  };
  return {
    state,
    execution,
    controller: data.controller,
    snapshot: data.snapshot,
    relation,
    history,
    applyFilter,
    toggleSort,
    sortBy,
    refresh,
    cancel,
    loadMore,
    countRows,
    cancelCount,
    retry: data.retry,
    clearHistory,
    clearFilterError,
  };
}
