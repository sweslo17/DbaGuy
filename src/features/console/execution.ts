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
  cancelled: boolean;
}

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
    cancelled: false,
  };
}

function updateSet(state: ExecutionState, index: number, fn: (set: ResultSetState) => ResultSetState): ExecutionState {
  return { ...state, resultSets: state.resultSets.map((s) => (s.index === index ? fn(s) : s)) };
}

/** Folds one server event of this execution into its state. */
export function applyEvent(state: ExecutionState, event: ServerEvent): ExecutionState {
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
    case "error":
      return { ...state, error: { message: event.message, code: event.code, position: event.position } };
    case "finished":
      return {
        ...state,
        status: "done",
        elapsedMs: event.elapsed_ms,
        inTransaction: event.in_transaction,
        cancelled: event.cancelled,
        resultSets: state.resultSets.map((s) => ({ ...s, hasMore: false })),
      };
  }
}
