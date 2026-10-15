import type { ColumnMeta, ServerEvent } from "@/lib/api/types";

export interface ResultSetState {
  index: number;
  columns: ColumnMeta[];
  rows: unknown[][];
  rowCount: number;
  hasMore: boolean;
}

export interface ExecutionState {
  requestId: string;
  status: "running" | "paused" | "done";
  resultSets: ResultSetState[];
  affected: { index: number; rows: number }[];
  messages: { severity: string; text: string }[];
  error: { message: string; code?: string | null; position?: { line: number; column: number } | null } | null;
  elapsedMs: number | null;
  inTransaction: boolean;
  /** The open transaction is aborted: the server only accepts ROLLBACK. */
  transactionFailed: boolean;
  cancelled: boolean;
  /** The connection dropped mid-request: later events are ignored because some were lost. */
  interrupted: boolean;
}

export const INTERRUPTED_MESSAGE = "Connection interrupted — results may be incomplete";
/** Error codes the backend sends for requests it never ran; the request is over even if `finished` goes missing. */
const TERMINAL_ERROR_CODES = new Set(["no_open_result", "invalid_command"]);

export function newExecution(requestId: string): ExecutionState {
  return {
    requestId,
    status: "running",
    resultSets: [],
    affected: [],
    messages: [],
    error: null,
    elapsedMs: null,
    inTransaction: false,
    transactionFailed: false,
    cancelled: false,
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
    resultSets: state.resultSets.map((s) => ({ ...s, hasMore: false })),
  };
}

function updateSet(state: ExecutionState, index: number, fn: (set: ResultSetState) => ResultSetState): ExecutionState {
  return { ...state, resultSets: state.resultSets.map((s) => (s.index === index ? fn(s) : s)) };
}

/** Folds one server event of this execution into its state. */
export function applyEvent(state: ExecutionState, event: ServerEvent): ExecutionState {
  if (state.interrupted) return state;
  switch (event.type) {
    case "started":
      return { ...state, status: "running" };
    case "result_set":
      return {
        ...state,
        resultSets: [
          ...state.resultSets,
          { index: event.index, columns: event.columns, rows: [], rowCount: 0, hasMore: false },
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
    case "finished":
      return {
        ...state,
        status: "done",
        elapsedMs: event.elapsed_ms,
        inTransaction: event.in_transaction,
        transactionFailed: event.transaction_failed,
        cancelled: event.cancelled,
        resultSets: state.resultSets.map((s) => ({ ...s, hasMore: false })),
      };
    default:
      return state; // an event type this client does not know must never wipe the state
  }
}
