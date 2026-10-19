import type {
  Classification,
  ColumnMeta,
  EditOutcomeEvent,
  GuardReason,
  QueryContext,
  ServerEvent,
  StatementParams,
} from "@/lib/api/types";

/** `edits`: a grid-edit plan applied (`apply_edits`, spec ⑤ §3.5); it ends with its `editOutcome`. */
export type ExecutionKind = "execute" | "export" | "transaction" | "edits";

/**
 * What a request is sent with besides its statement (Couchbase): the query context and the typed parameters.
 * `queryContext` undefined means the session's default context (what `SessionController` fills in); null, none.
 */
export interface RequestOptions {
  queryContext?: QueryContext | null;
  params?: StatementParams | null;
}

export interface ResultSetState {
  index: number;
  /** How the set is drawn: a `plan` (Couchbase EXPLAIN, ADVISE or a profile) is one cell holding the plan's JSON text. */
  presentation: "grid" | "plan";
  columns: ColumnMeta[];
  rows: unknown[][];
  rowCount: number;
  hasMore: boolean;
  /** It ended while paused (discarded by the next statement, cancelled or interrupted): only some rows were loaded. */
  partial: boolean;
}

/** One request on a session, folded from its server events. */
export interface ExecutionState {
  requestId: string;
  kind: ExecutionKind;
  /** The statement sent; "" for transaction commands. */
  sql: string;
  /** What the request was sent with: an export of its result (run again) takes the same context and parameters. */
  request: RequestOptions;
  /** The request asked for the execution profile (Run with profile): a result set of the plan presentation is it. */
  profilePlan: boolean;
  status: "running" | "paused" | "done";
  resultSets: ResultSetState[];
  affected: { index: number; rows: number }[];
  messages: { severity: string; text: string }[];
  error: { message: string; code?: string | null; position?: { line: number; column: number } | null } | null;
  /** The guard stopped the statement: resend it with `confirmed` once the user agreed. */
  confirmation: { reasons: GuardReason[]; classification: Classification } | null;
  /** Rows written so far (export only). */
  progressRows: number | null;
  elapsedMs: number | null;
  /** `Date.now()` when the request was created. */
  startedAt: number;
  cancelled: boolean;
  timedOut: boolean;
  /** Confirmation was required: nothing ran. */
  skipped: boolean;
  /** The connection dropped mid-request: later events are ignored because some were lost. */
  interrupted: boolean;
  /** An `edits` request's outcome (exactly one per apply, 05a E1); null until it arrives, and for other kinds. */
  editOutcome: EditOutcomeEvent | null;
}

export const INTERRUPTED_MESSAGE = "Connection interrupted — results may be incomplete";
/** Error code of a statement whose database connection dropped: the session must be reopened (retried). */
export const CONNECTION_LOST = "connection_lost";
/**
 * Error code of a request that never left: the socket was not open when it was issued (`SessionController`), so the
 * backend never saw it. Only the controller sets it; the backend has no such code.
 */
export const NOT_CONNECTED_CODE = "not_connected";
/**
 * Error codes the backend sends for requests it never ran; the request is over even if `finished` goes missing. Not for
 * an `edits` request: a refused apply still sends its `edit_outcome` after the error (05a M6), and an apply that ended
 * without one would read as an unknown outcome (ruling H21).
 */
const TERMINAL_ERROR_CODES = new Set(["no_open_result", "invalid_command"]);

export function newExecution(
  requestId: string,
  kind: ExecutionKind,
  sql: string,
  request: RequestOptions = {},
  profilePlan = false,
): ExecutionState {
  return {
    requestId,
    kind,
    sql,
    request,
    profilePlan,
    status: "running",
    resultSets: [],
    affected: [],
    messages: [],
    error: null,
    confirmation: null,
    progressRows: null,
    elapsedMs: null,
    startedAt: Date.now(),
    cancelled: false,
    timedOut: false,
    skipped: false,
    interrupted: false,
    editOutcome: null,
  };
}

/** Ends a request whose `finished` can no longer arrive (socket dropped or session gone). */
export function interruptExecution(state: ExecutionState): ExecutionState {
  if (state.status === "done") return state;
  return {
    ...state,
    status: "done",
    interrupted: true,
    messages: [...state.messages, { severity: "warning", text: INTERRUPTED_MESSAGE }],
    resultSets: endSets(state.resultSets),
  };
}

/** The request is over: a set still waiting for more rows keeps only those it has. */
function endSets(sets: ResultSetState[]): ResultSetState[] {
  return sets.map((s) => (s.hasMore ? { ...s, hasMore: false, partial: true } : s));
}

/** Rebuilds only the touched result set, so appending rows stays linear in the total row count. */
function updateSet(state: ExecutionState, index: number, fn: (set: ResultSetState) => ResultSetState): ExecutionState {
  return { ...state, resultSets: state.resultSets.map((s) => (s.index === index ? fn(s) : s)) };
}

/**
 * The request's first result set has all of its first page (the page ended, it paused, or the request is over): a
 * re-run replaces the rows shown only then, so the grid never flashes empty and keeps its scroll position (A12).
 */
export function firstPageDelivered(execution: ExecutionState | null): boolean {
  const set = execution?.resultSets[0];
  return !!execution && !!set && (set.rowCount > 0 || set.hasMore || execution.status !== "running");
}

/** Folds one server event of this execution into its state. `session_state` is session-wide and never applies. */
export function applyEvent(state: ExecutionState, event: ServerEvent): ExecutionState {
  if (state.interrupted || event.type === "session_state") return state;
  switch (event.type) {
    case "started":
      return { ...state, status: "running" };
    case "result_set":
      return {
        ...state,
        resultSets: [
          ...state.resultSets,
          {
            index: event.index,
            presentation: event.presentation,
            columns: event.columns,
            rows: [],
            rowCount: 0,
            hasMore: false,
            partial: false,
          },
        ],
      };
    case "rows":
      return updateSet(state, event.index, (s) => ({ ...s, rows: s.rows.concat(event.rows) }));
    case "result_set_end": {
      const next = updateSet(state, event.index, (s) => ({ ...s, rowCount: event.row_count, hasMore: event.has_more }));
      return event.has_more ? { ...next, status: "paused" } : next;
    }
    case "affected":
      return { ...state, affected: [...state.affected, { index: event.index, rows: event.rows }] };
    case "message":
      return { ...state, messages: [...state.messages, { severity: event.severity, text: event.text }] };
    case "error": {
      const error = { message: event.message, code: event.code, position: event.position };
      const terminal = event.code != null && TERMINAL_ERROR_CODES.has(event.code) && state.kind !== "edits";
      return terminal ? { ...state, error, status: "done" } : { ...state, error };
    }
    case "confirmation_required":
      return { ...state, confirmation: { reasons: event.reasons, classification: event.classification } };
    case "progress":
      return { ...state, progressRows: event.rows };
    case "edit_outcome":
      return { ...state, editOutcome: event };
    case "finished":
      return {
        ...state,
        status: "done",
        elapsedMs: event.elapsed_ms,
        cancelled: event.cancelled,
        timedOut: event.timed_out,
        skipped: event.skipped,
        resultSets: endSets(state.resultSets),
      };
    default:
      return state; // an event type this client does not know must never wipe the state
  }
}
