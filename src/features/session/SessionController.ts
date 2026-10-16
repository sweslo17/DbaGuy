import { createStore, type StoreApi } from "zustand/vanilla";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { SessionSocket, type SocketStatus } from "@/lib/api/session-socket";
import type {
  ClientCommand,
  FormatKind,
  RowFormatOptions,
  ServerEvent,
  SessionInfo,
  SessionStateEvent,
} from "@/lib/api/types";
import { applyEvent, interruptExecution, newExecution, type ExecutionKind, type ExecutionState } from "./execution";

export type SessionPhase = "opening" | "open" | "reconnecting" | "failed" | "closed";

/** The session's transaction state from its last `session_state` event: the only source for transaction UI. */
export interface SessionStateView {
  autocommit: boolean;
  inTransaction: boolean;
  transactionFailed: boolean;
  transactionStartedAt: string | null;
  idleSeconds: number | null;
}

/** A session-wide message (request id ""), e.g. an idle-transaction warning or an idle rollback. */
export interface SessionMessage {
  id: number;
  severity: string;
  text: string;
  at: number;
}

export interface SessionSnapshot {
  phase: SessionPhase;
  info: SessionInfo | null;
  /** Opening failed (an ApiError, e.g. `password_required`) or the session is gone. */
  error: Error | null;
  state: SessionStateView | null;
  executions: Record<string, ExecutionState>;
  sessionMessages: SessionMessage[];
}

export interface ExecuteOptions {
  pageSize?: number;
  timeoutS?: number | null;
  confirmed?: boolean;
  history?: boolean;
}

export const PAGE_SIZE = 500;
const MAX_SESSION_MESSAGES = 50;
const SESSION_GONE = "The session is no longer available.";
const NOT_CONNECTED: ExecutionState["error"] = { message: "Not connected", code: "not_connected" };

export function emptySnapshot(phase: SessionPhase): SessionSnapshot {
  return { phase, info: null, error: null, state: null, executions: {}, sessionMessages: [] };
}

function disposeSession(sessionId: string): void {
  api()
    .then((client) => client.DELETE("/sessions/{session_id}", { params: { path: { session_id: sessionId } } }))
    .then(unwrap)
    .catch((error: unknown) => console.warn(`Could not close session ${sessionId}: ${errorMessage(error)}`));
}

function stateView(event: SessionStateEvent): SessionStateView {
  return {
    autocommit: event.autocommit,
    inTransaction: event.in_transaction,
    transactionFailed: event.transaction_failed,
    transactionStartedAt: event.transaction_started_at ?? null,
    idleSeconds: event.idle_seconds ?? null,
  };
}

const unfinished = (execution: ExecutionState) => execution.status !== "done";

/** Ends every unfinished execution: the events it still needed can no longer arrive. */
function interruptAll(executions: Record<string, ExecutionState>): Record<string, ExecutionState> {
  return Object.fromEntries(Object.entries(executions).map(([id, execution]) => [id, interruptExecution(execution)]));
}

/**
 * One backend session and its WebSocket, without React: folds every server event into per-request execution state
 * and session-level state in `store`. User statements are never queued for replay: a command issued while the
 * socket is not open ends at once with `not_connected`.
 */
export class SessionController {
  readonly store: StoreApi<SessionSnapshot> = createStore<SessionSnapshot>()(() => emptySnapshot("opening"));
  private socket: SessionSocket | null = null;
  private opening: Promise<void> | null = null;
  private closed = false;
  private messageCount = 0;

  constructor(
    private readonly profileId: string,
    private readonly database: string | null,
  ) {}

  /** Creates the backend session and connects its socket. Idempotent. */
  open(): Promise<void> {
    if (this.closed) return Promise.resolve();
    this.opening ??= this.connect();
    return this.opening;
  }

  /** Closes the socket and the backend session; unfinished executions end as interrupted. Idempotent. */
  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.socket?.close();
    const { info, executions } = this.store.getState();
    this.store.setState({ phase: "closed", executions: interruptAll(executions) });
    if (info && !this.socket?.isRejected) disposeSession(info.session_id); // a rejected session is already gone
  }

  /** Runs one statement; returns its request id. */
  execute(sql: string, options: ExecuteOptions = {}): string {
    return this.track("execute", sql, {
      type: "execute",
      request_id: crypto.randomUUID(),
      sql,
      page_size: options.pageSize ?? PAGE_SIZE,
      confirmed: options.confirmed ?? false,
      timeout_s: options.timeoutS ?? null,
      history: options.history ?? true,
    });
  }

  /** Loads the next page of a paused result (ignored unless it is paused, so a double click asks only once). */
  fetchMore(requestId: string, count = PAGE_SIZE): void {
    if (this.execution(requestId)?.status !== "paused") return;
    if (this.send({ type: "fetch_more", request_id: requestId, count })) {
      this.updateExecution(requestId, (execution) => ({ ...execution, status: "running" }));
    }
  }

  /** Ends a paused result without fetching the rest; its `finished` follows. */
  discard(requestId: string): void {
    if (this.isUnfinished(requestId)) this.send({ type: "discard", request_id: requestId });
  }

  cancel(requestId: string): void {
    if (this.isUnfinished(requestId)) this.send({ type: "cancel", request_id: requestId });
  }

  commit(): string {
    return this.track("transaction", "", { type: "commit", request_id: crypto.randomUUID() });
  }

  rollback(): string {
    return this.track("transaction", "", { type: "rollback", request_id: crypto.randomUUID() });
  }

  setAutocommit(enabled: boolean): string {
    return this.track("transaction", "", { type: "set_autocommit", request_id: crypto.randomUUID(), enabled });
  }

  /** Runs `sql` again and writes its whole result to `path` on the backend; `progressRows` reports the rows written. */
  exportResult(sql: string, format: FormatKind, path: string, options: RowFormatOptions): string {
    return this.track("export", sql, { type: "export", request_id: crypto.randomUUID(), sql, format, path, options });
  }

  /** Drops a request's state (and its rows) from the store; later events for it are ignored. */
  forget(requestId: string): void {
    if (!this.execution(requestId)) return;
    this.store.setState((snapshot) => {
      const executions = { ...snapshot.executions };
      delete executions[requestId];
      return { executions };
    });
  }

  /** Resolves with the request's state once it is done (finished, failed or interrupted); rejects if it is unknown. */
  whenDone(requestId: string): Promise<ExecutionState> {
    return new Promise((resolve, reject) => {
      const settle = (snapshot: SessionSnapshot): boolean => {
        const execution = snapshot.executions[requestId];
        if (!execution) reject(new Error(`Unknown or forgotten request ${requestId}`));
        else if (execution.status === "done") resolve(execution);
        else return false;
        return true;
      };
      if (settle(this.store.getState())) return;
      const unsubscribe = this.store.subscribe((snapshot) => {
        if (settle(snapshot)) unsubscribe();
      });
    });
  }

  private async connect(): Promise<void> {
    try {
      const client = await api();
      const body = { profile_id: this.profileId, database: this.database };
      const info = unwrap(await client.POST("/sessions", { body }));
      if (this.closed) {
        disposeSession(info.session_id); // closed while opening (StrictMode, fast unmount): do not leak the session
        return;
      }
      this.store.setState({ info });
      const socket = new SessionSocket(info.session_id);
      this.socket = socket;
      socket.subscribe((event) => this.onEvent(event));
      socket.onStatus((status) => this.onStatus(status, socket.isRejected));
      await socket.connect();
    } catch (error) {
      if (!this.closed) this.fail(error instanceof Error ? error : new Error(String(error)));
    }
  }

  private onStatus(status: SocketStatus, rejected: boolean): void {
    if (this.closed) return;
    if (status === "open") this.onOpen();
    else if (status === "closed") {
      if (rejected) this.fail(new Error(SESSION_GONE));
      else this.store.setState({ phase: "reconnecting" });
    }
  }

  /**
   * Commands are only sent while the socket is open, so a request still unfinished when the socket opens was sent
   * before it closed: the events it still needed are gone for good and it can never complete. End it, and cancel
   * it so it stops blocking the next statement — except inside a transaction, where a cancel would abort the whole
   * transaction: there it is only marked interrupted and the user decides.
   */
  private onOpen(): void {
    const { executions, state } = this.store.getState();
    const orphans = Object.values(executions).filter(unfinished);
    this.store.setState({ phase: "open", executions: orphans.length ? interruptAll(executions) : executions });
    if (!state?.inTransaction) orphans.forEach((orphan) => this.send({ type: "cancel", request_id: orphan.requestId }));
  }

  private onEvent(event: ServerEvent): void {
    if (this.closed) return;
    if (event.type === "session_state") this.store.setState({ state: stateView(event) });
    else if (event.type === "message" && event.request_id === "") this.addSessionMessage(event.severity, event.text);
    else this.updateExecution(event.request_id, (execution) => applyEvent(execution, event));
  }

  /** Opening failed or the session is gone (and with it any transaction it had). */
  private fail(error: Error): void {
    this.store.setState((snapshot) => ({
      phase: "failed",
      error,
      state: null,
      executions: interruptAll(snapshot.executions),
    }));
  }

  /** Sends only while the socket is open: SessionSocket would otherwise queue the command and replay it late. */
  private send(command: ClientCommand): boolean {
    if (this.closed || !this.socket || this.store.getState().phase !== "open") return false;
    this.socket.send(command);
    return true;
  }

  private track(kind: ExecutionKind, sql: string, command: ClientCommand): string {
    const execution = newExecution(command.request_id, kind, sql);
    const sent = this.send(command);
    this.putExecution(sent ? execution : { ...execution, status: "done", error: NOT_CONNECTED });
    return command.request_id;
  }

  private execution(requestId: string): ExecutionState | undefined {
    return this.store.getState().executions[requestId];
  }

  private isUnfinished(requestId: string): boolean {
    const execution = this.execution(requestId);
    return !!execution && unfinished(execution);
  }

  private putExecution(execution: ExecutionState): void {
    this.store.setState((snapshot) => ({ executions: { ...snapshot.executions, [execution.requestId]: execution } }));
  }

  /** Unknown (or forgotten) request ids are ignored. */
  private updateExecution(requestId: string, fn: (execution: ExecutionState) => ExecutionState): void {
    const current = this.execution(requestId);
    if (!current) return;
    const next = fn(current);
    if (next !== current) this.putExecution(next);
  }

  private addSessionMessage(severity: string, text: string): void {
    const message: SessionMessage = { id: ++this.messageCount, severity, text, at: Date.now() };
    this.store.setState((snapshot) => ({
      sessionMessages: [...snapshot.sessionMessages, message].slice(-MAX_SESSION_MESSAGES),
    }));
  }
}
