import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useEffectEvent, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import type { PathSegment } from "@/lib/api/types";
import type { TableTabState } from "@/stores/workspace";
import { useEngineInfo } from "../connections/hooks";
import { isPasswordRequired } from "../connections/UnlockDialog";
import { editTargetKey, fetchEditTarget } from "../edits/editsApi";
import { INTERRUPTED_MESSAGE, type ExecutionState } from "../session/execution";
import {
  isExporting,
  MAX_PAGE_SIZE,
  PAGE_SIZE,
  type SessionController,
  type SessionSnapshot,
} from "../session/SessionController";
import { useSession } from "../session/useSession";
import { firstPageComplete } from "./browseView";
import { clearFilterHistory, readFilterHistory, rememberFilter } from "./filterHistory";
import { relationOf, type Relation } from "./filterValue";
import { firstSorts, TARGET_WAIT_MS, type TargetRead } from "./stableOrder";

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
  /**
   * The last filter entered was rejected (422) and is not applied: the rows still show `filter`. Cleared by applying a
   * filter again, by Escape, or by editing the text; never by a sort or a refresh.
   */
  filterError: string | null;
  count: { status: "idle" | "running" | "done" | "error"; value?: number; error?: string };
}

/** A statement for a session; `run` changes on every request, so an identical one runs again (Refresh). */
interface Job {
  run: number;
  sql: string;
  /** Rows of its first page. */
  pageSize: number;
}

/** The applied browse: what the grid shows (or is loading). */
interface BrowseJob extends Job {
  filter: string;
  sorts: SortKey[];
  /**
   * Changes when the grid should start again at the top (a new filter or sort); a re-run that keeps the position
   * (Refresh, the re-read after a commit: D-05-12) keeps it.
   */
  position: number;
  /** A Refresh of the browse the grid shows (same filter and sorts), not a new filter or sort. */
  rerun: boolean;
}

/** The first page of a re-run that keeps the position: the rows loaded now (at least a page, at most MAX_PAGE_SIZE). */
export const keptPageSize = (loaded: number) => Math.min(MAX_PAGE_SIZE, Math.max(PAGE_SIZE, loaded));

/** A count asked for `filter`; `sql` is null while it is being built. */
interface CountJob {
  run: number;
  filter: string;
  /** The browse run shown when the count was asked: rows fetched again after it make a finished count stale. */
  browseRun: number;
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
 * While an export runs on the session the job is held, and sent when the export ends: sent now, it would only wait
 * behind the export. Returns the request sent last with the job it ran; it can belong to an older job until the new
 * one is sent.
 */
function useSessionJob<J extends Job>(
  controller: SessionController | null,
  snapshot: SessionSnapshot | null,
  job: J | null,
): { requestId: string; job: J } | null {
  const sent = useRef<{ controller: SessionController; run: number; requestId: string } | null>(null);
  const [current, setCurrent] = useState<{ requestId: string; job: J } | null>(null);
  const phase = snapshot?.phase;
  const exporting = isExporting(snapshot);
  useEffect(() => {
    if (!controller || phase !== "open" || exporting || !job) return;
    const previous = sent.current?.controller === controller ? sent.current : null;
    if (previous?.run === job.run) return;
    if (previous) endRequest(controller, previous.requestId);
    const requestId = controller.execute(job.sql, { history: false, pageSize: job.pageSize });
    sent.current = { controller, run: job.run, requestId };
    setCurrent({ requestId, job });
  }, [controller, phase, exporting, job]);
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
 * The table's edit target, read once per tab from the first time it is shown, in parallel with the session's connect
 * (Task 9's edits read the same query): its key anchors the grid on a re-run (K14), its stable order sorts a PostgreSQL
 * tab's first browse (K13). A read that failed for want of the password is read again once the session is open (the
 * unlock dialog is the session's, and gives the password to both), once per session.
 */
function useEditTarget(
  profileId: string,
  path: PathSegment[],
  enabled: boolean,
  session: { controller: SessionController | null; open: boolean },
) {
  const query = useQuery({
    queryKey: editTargetKey(profileId, path),
    queryFn: ({ signal }) => fetchEditTarget(profileId, path, signal),
    enabled,
    staleTime: Infinity,
    retry: false,
  });
  const { refetch } = query;
  const passwordRequired = query.isError && isPasswordRequired(query.error);
  const readAgainFor = useRef<SessionController | null>(null);
  useEffect(() => {
    if (!session.open || !passwordRequired || readAgainFor.current === session.controller) return;
    readAgainFor.current = session.controller;
    void refetch();
  }, [session.open, session.controller, passwordRequired, refetch]);
  const { status, data } = query;
  const read = useMemo<TargetRead>(
    () =>
      status === "success"
        ? { status, target: data }
        : status === "error"
          ? { status, passwordRequired }
          : { status: "pending" },
    [status, data, passwordRequired],
  );
  return { target: data ?? null, read };
}

/**
 * Browse state of a table data tab (spec §4.5): filter, sorts, the generated SELECT and its paged result on the tab's
 * own session, opened the first time `active` (the Data view is shown) and kept while the tab is open. Count rows runs
 * on a second session, opened on first use (ruling R-02b-2), so the paused page keeps loading more.
 */
export function useTableData(tab: TableTabState, active: boolean) {
  const { profileId, path } = tab;
  const relation: Relation | null = useMemo(() => relationOf(path), [path]);
  const database = relation?.database ?? null;
  const [shown, setShown] = useState(active);
  if (active && !shown) setShown(true);
  const [countRequested, setCountRequested] = useState(false);
  const data = useSession(profileId, database, { enabled: shown });
  const counting = useSession(profileId, database, { enabled: countRequested });
  const { controller: countController, snapshot: countSnapshot, retry: retryCount } = counting;
  const engine = useEngineInfo(profileId);
  const rowEditing = engine?.row_editing === true;
  const sessionOpen = data.snapshot?.phase === "open";
  const edit = useEditTarget(profileId, path, shown && rowEditing, { controller: data.controller, open: sessionOpen });

  const [browseJob, setBrowseJob] = useState<BrowseJob | null>(null);
  /** The last sort or refresh could not be built. */
  const [buildError, setBuildError] = useState<string | null>(null);
  const [filterError, setFilterError] = useState<string | null>(null);
  const [countJob, setCountJob] = useState<CountJob | null>(null);
  const [history, setHistory] = useState(() => readFilterHistory(profileId, path));
  const builds = useRef(0);
  const countRuns = useRef(0);
  // The rows the grid shows, for the page size of a Refresh that keeps the position (D-05-12). Read when the re-run is
  // asked for, never as a dependency (rows arrive page by page).
  const loaded = useRef(0);

  const sentBrowse = useSessionJob(data.controller, data.snapshot, browseJob);
  const countReady = useMemo(
    () => (countJob?.sql ? { run: countJob.run, sql: countJob.sql, pageSize: PAGE_SIZE } : null),
    [countJob],
  );
  const sentCount = useSessionJob(countController, countSnapshot, countReady);
  const execution: ExecutionState | null = sentBrowse
    ? (data.snapshot?.executions[sentBrowse.requestId] ?? null)
    : null;
  const filter = browseJob?.filter ?? "";
  const phase = data.snapshot?.phase;
  // The applied browse has not delivered its first page yet (not sent yet, or running): the grid keeps the rows it
  // shows until then.
  const pending =
    !!browseJob &&
    phase !== "failed" &&
    phase !== "closed" &&
    (sentBrowse?.job.run !== browseJob.run || (!!execution && execution.status !== "done"));

  /**
   * Builds the SELECT for `filter` and `sorts` and makes it the applied browse; a rejected build changes nothing. Only
   * a build for a newly entered filter (`newFilter`) sets or clears the filter error: a sort keeps it.
   */
  const browse = useCallback(
    (nextFilter: string, sorts: SortKey[], newFilter = false) => {
      const ticket = ++builds.current;
      buildSelect(profileId, path, nextFilter, sorts, false).then(
        (sql) => {
          if (ticket !== builds.current) return;
          setBuildError(null);
          if (newFilter) setFilterError(null);
          loaded.current = 0; // a new filter or sort starts over: nothing of the old rows is "loaded" for its Refresh
          setBrowseJob((job) => ({
            run: (job?.run ?? 0) + 1,
            sql,
            filter: nextFilter,
            sorts,
            pageSize: PAGE_SIZE,
            position: (job?.position ?? 0) + 1,
            rerun: false,
          }));
        },
        (error: unknown) => {
          if (ticket !== builds.current) return;
          if (newFilter) setFilterError(errorMessage(error));
          else setBuildError(errorMessage(error));
        },
      );
    },
    [profileId, path],
  );

  // The first browse: where the engine's unsorted order moves an edited row (PostgreSQL), sorted by the table's stable
  // order once its target is read (K13). It never waits long: TARGET_WAIT_MS after the session opened, it browses
  // unsorted. Applied once: a Refresh, a re-read or a filter keeps the sorts the user left.
  const [waitedOut, setWaitedOut] = useState(false);
  const ordersByKey = engine ? engine.orders_browse_by_key : null;
  const defaultSorts = useMemo(
    () => firstSorts(ordersByKey, edit.read, waitedOut),
    [ordersByKey, edit.read, waitedOut],
  );
  const waiting = shown && !browseJob && defaultSorts === null;
  useEffect(() => {
    if (!waiting || !sessionOpen) return;
    const timer = setTimeout(() => setWaitedOut(true), TARGET_WAIT_MS);
    return () => clearTimeout(timer);
  }, [waiting, sessionOpen]);
  const started = useRef(false);
  useEffect(() => {
    if (!shown || started.current || !defaultSorts) return;
    started.current = true;
    browse("", defaultSorts);
  }, [shown, browse, defaultSorts]);

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

  const count = countState(countJob, filter, countSnapshot, sentCount);
  const countRunning = count.status === "running";
  // A count that finished before the rows were fetched again (a refresh, a sort) may be out of date: drop it once
  // the new rows arrive. One still running is as new as they are.
  const succeededRun = succeeded && sentBrowse ? sentBrowse.job.run : 0;
  const dropOutdatedCount = useEffectEvent(() => {
    if (countJob && !countRunning && succeededRun > countJob.browseRun) setCountJob(null);
  });
  useEffect(() => {
    dropOutdatedCount();
  }, [succeededRun]);

  const applyFilter = useCallback(
    (text: string) => browse(text.trim(), browseJob?.sorts ?? [], true),
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
  // D-05-12 (CR-1: only an engine whose table data can be edited): a re-run reloads as many rows as are loaded and the
  // grid keeps its position.
  const keepsPosition = rowEditing;
  useLayoutEffect(() => {
    // The rows of the applied browse's request once its first page is in whole: one still loading it, or cut short,
    // holds fewer, and a second Refresh meanwhile must reload what the grid shows (the held rows), not what has arrived
    // so far. The request sent before a new filter or sort took over is not the applied browse's (`browse` reset it).
    const set = execution?.resultSets[0];
    if (set && sentBrowse?.job.run === browseJob?.run && firstPageComplete(execution)) loaded.current = set.rows.length;
  });
  /**
   * Runs the applied browse again (or builds the first one if that failed); a finished count is out of date. Where the
   * engine can edit rows, it loads as many rows as are loaded now (up to MAX_PAGE_SIZE) and keeps the grid's position.
   */
  const refresh = useCallback(() => {
    setBuildError(null);
    if (!countRunning) setCountJob(null);
    if (!browseJob) {
      // The first browse again (its build failed), or now while it still waits for the target: with what is known.
      started.current = true;
      browse("", defaultSorts ?? []);
      return;
    }
    setBrowseJob((job) =>
      job && keepsPosition
        ? { ...job, run: job.run + 1, pageSize: keptPageSize(loaded.current), rerun: true }
        : job && { ...job, run: job.run + 1, pageSize: PAGE_SIZE, position: job.position + 1, rerun: true },
    );
  }, [browse, browseJob, countRunning, keepsPosition, defaultSorts]);
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
    setCountJob({ run, filter: countFilter, browseRun: browseJob.run, sql: null, error: null });
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
  /** Escape (the input shows the applied filter again) or an edit: the rejected text is no longer what is shown. */
  const clearFilterError = useCallback(() => setFilterError(null), []);

  // The request whose first page is in whole: a commit whose outcome was unknown waits for another one (fresh rows,
  // R-05-6), a Refresh or the same browse sent again on a new session alike. A first page cut short (cancelled, failed,
  // interrupted) is not fresh rows.
  const deliveredRequest = sentBrowse && firstPageComplete(execution) ? sentBrowse.requestId : null;

  const state: TableDataState = {
    filter,
    sorts: browseJob?.sorts ?? [],
    sql: browseJob?.sql ?? null,
    requestId: sentBrowse?.requestId ?? null,
    error:
      filterError ??
      buildError ??
      execution?.error?.message ??
      (execution?.interrupted ? INTERRUPTED_MESSAGE : null),
    filterError,
    count,
  };
  return {
    state,
    execution,
    pending,
    /** Changes only when the grid should scroll back to the top (D-05-12). */
    position: browseJob?.position ?? 0,
    /** The browse request whose first page is in whole (null while one loads, and when one ended before it did). */
    deliveredRequest,
    /** The request on screen is a Refresh of what the grid shows: one that ends early leaves the rows as they were. */
    rerun: sentBrowse?.job.rerun ?? false,
    /** The table's edit target once read (null before, and when it could not be): its key anchors a re-run (K14). */
    target: edit.target,
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
