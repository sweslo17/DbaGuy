import type {
  Classification,
  ColumnMeta,
  GuardReason,
  QueryContext,
  ServerEvent,
  StatementParams,
} from "@/lib/api/types";

export type ExecutionKind = "execute" | "export" | "transaction";

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
}

export const INTERRUPTED_MESSAGE = "Connection interrupted — results may be incomplete";
/** Error code of a statement whose database connection dropped: the session must be reopened (retried). */
export const CONNECTION_LOST = "connection_lost";
/** Error codes the backend sends for requests it never ran; the request is over even if `finished` goes missing. */
const TERMINAL_ERROR_CODES = new Set(["no_open_result", "invalid_command"]);

export function newExecution(
  requestId: string,
  kind: ExecutionKind,
  sql: string,
  request: RequestOptions = {},
): ExecutionState {
  return {
    requestId,
    kind,
    sql,
    request,
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
      const terminal = event.code != null && TERMINAL_ERROR_CODES.has(event.code);
      return terminal ? { ...state, error, status: "done" } : { ...state, error };
    }
    case "confirmation_required":
      return { ...state, confirmation: { reasons: event.reasons, classification: event.classification } };
    case "progress":
      return { ...state, progressRows: event.rows };
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
