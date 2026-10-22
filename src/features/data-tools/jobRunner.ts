import type { SocketStatus } from "@/lib/api/session-socket";
import type { ClientCommand, ServerEvent, SessionInfo } from "@/lib/api/types";
import type { JobCommand, JobEvent } from "./jobState";

/*
 * One job's session and socket (spec ⑥ D-06-10, R-06-15), outside React: a job outlives its dialog. A new job opens a
 * session of its own and sends its command once, the first time the socket opens, never again (a reconnect gets the
 * backend's replay of its last events instead). A job listed after a reload only listens. Once the job's request is
 * over (its outcome, live or replayed), the socket and the session close (a job's session is never reused: a dump
 * leaves its settings on it, 06a carry). A socket the backend refuses (the session is gone: the backend stopped or
 * restarted) ends the job here with a loss. Everything it learns goes to `report` as a `JobEvent`.
 */

/** What the runner needs of a session's WebSocket (`SessionSocket`). */
export interface JobSocket {
  readonly isRejected: boolean;
  connect(): Promise<void>;
  trySend(command: ClientCommand): boolean;
  subscribe(listener: (event: ServerEvent) => void): () => void;
  onStatus(listener: (status: SocketStatus) => void): () => void;
  close(): void;
}

/** The backend, as the runner reaches it (`backendTransport` in jobsStore.ts; fakes in the tests). */
export interface JobTransport {
  openSession(profileId: string, database: string | null): Promise<SessionInfo>;
  closeSession(sessionId: string): Promise<void>;
  cancel(sessionId: string, requestId: string): Promise<void>;
  socket(sessionId: string): JobSocket;
  now(): number;
}

const errorMessage = (error: unknown) => (error instanceof Error ? error.message : String(error));

const hasRequestId = (event: ServerEvent): event is Extract<ServerEvent, { request_id: string }> => "request_id" in event;

/** The job's request is over: its outcome (the end), or a `finished` without one (read as unknown, `jobState`). */
const endsRequest = (event: ServerEvent): boolean => event.type === "job_outcome" || event.type === "finished";

/**
 * The command as it goes, once its session is open: a run's `query_context` left undefined means the profile's default,
 * which only the session knows (`server.default_query_context`, as `SessionController` resolves a tab's); the backend
 * reads null or omitted as no context. Only an engine with query contexts has a default (Couchbase): without one the
 * command goes as it is (omitted: none, which is then the default). null and an explicit context pass unchanged.
 */
export function withSessionDefaults(command: JobCommand, info: SessionInfo): JobCommand {
  const fallback = info.server.default_query_context;
  if (command.type !== "run_file" || command.query_context !== undefined || !fallback) return command;
  return { ...command, query_context: fallback };
}

export class JobRunner {
  private socket: JobSocket | null = null;
  private sessionId: string | null = null;
  private sent = false;
  /** Listens no more: the job's request is over here, its session is gone, or the runner was detached. */
  private done = false;
  /** The socket and the session were dealt with (closed once, never twice). */
  private finished = false;
  /** A Cancel before the command left: it is never sent. */
  private withheld = false;
  private readonly unsubscribe: (() => void)[] = [];

  constructor(
    readonly requestId: string,
    private readonly transport: JobTransport,
    private readonly report: (event: JobEvent) => void,
    /** The job's session, once known (the store keeps it for a cancel through REST and for the list). */
    private readonly onSession: (sessionId: string) => void = () => undefined,
  ) {}

  /** A new job: its own session, then `command` once the socket first opens. */
  start(profileId: string, database: string | null, command: JobCommand): void {
    void this.open(profileId, database, command);
  }

  /** A job the backend lists (a reloaded window): its socket only, which replays the job's last events. */
  attach(sessionId: string): void {
    this.sent = true;
    this.sessionId = sessionId;
    this.listen(sessionId, null);
  }

  /** Cancel: on the socket while it is open, else through REST; before the command left, it is never sent. */
  cancel(): void {
    if (this.done) return;
    this.report({ type: "cancel_sent", at: this.transport.now() });
    if (!this.sent) {
      this.withheld = true;
      return;
    }
    if (this.socket?.trySend({ type: "cancel", request_id: this.requestId })) return;
    if (this.sessionId) {
      this.transport
        .cancel(this.sessionId, this.requestId)
        .catch((error: unknown) => console.warn(`Job ${this.requestId}: cancel failed: ${errorMessage(error)}`));
    }
  }

  /**
   * The window forgot the job (Clear finished, while it was ended only as not listed): the socket closes and nothing
   * more is reported. The session is left alone: whatever still runs there, the backend's list tells, and a window
   * that finds it running attaches to it anew (one socket per session, never two).
   */
  detach(): void {
    this.stop();
  }

  private async open(profileId: string, database: string | null, command: JobCommand): Promise<void> {
    let info: SessionInfo;
    try {
      info = await this.transport.openSession(profileId, database);
    } catch (error) {
      this.done = true;
      this.report({ type: "open_failed", error: error instanceof Error ? error : new Error(String(error)), at: this.transport.now() });
      return;
    }
    this.sessionId = info.session_id;
    this.onSession(info.session_id);
    if (this.withheld) {
      this.notSent();
      return;
    }
    this.listen(info.session_id, withSessionDefaults(command, info));
  }

  private listen(sessionId: string, command: JobCommand | null): void {
    const socket = this.transport.socket(sessionId);
    this.socket = socket;
    this.unsubscribe.push(
      socket.subscribe((event) => {
        if (this.done || !hasRequestId(event) || event.request_id !== this.requestId) return;
        const ends = endsRequest(event);
        if (ends) this.done = true;
        try {
          this.report({ type: "server", event, at: this.transport.now() });
        } finally {
          if (ends) this.finish();
        }
      }),
      socket.onStatus((status) => {
        if (this.done) return;
        if (status === "open") {
          this.report({ type: "socket", open: true });
          if (command && !this.sent) this.send(socket, command);
        } else if (status === "closed") {
          if (!socket.isRejected) {
            this.report({ type: "socket", open: false });
            return;
          }
          // The backend refused the socket for good: the session is gone, and the job's outcome with it
          this.stop();
          this.report({ type: "session_gone", at: this.transport.now() });
        }
      }),
    );
    socket.connect().catch((error: unknown) => {
      console.warn(`Job ${this.requestId}: the socket could not connect: ${errorMessage(error)}`);
      if (!this.sent && !this.done) this.notSent();
    });
  }

  private send(socket: JobSocket, command: JobCommand): void {
    if (this.withheld || !socket.trySend(command)) {
      this.notSent();
      return;
    }
    this.sent = true;
    this.report({ type: "sent", at: this.transport.now() });
  }

  /** Nothing left: the session is closed (it never ran anything of this job). */
  private notSent(): void {
    this.done = true;
    this.report({ type: "not_sent", at: this.transport.now() });
    this.finish();
  }

  /** Listens no more: its listeners go and its socket closes (a refused one stops reconnecting with it). */
  private stop(): void {
    this.done = true;
    this.unsubscribe.splice(0).forEach((unsubscribe) => unsubscribe());
    this.socket?.close();
  }

  /** The job ended: its socket and its session go, once. */
  private finish(): void {
    if (this.finished) return;
    this.finished = true;
    this.stop();
    const sessionId = this.sessionId;
    if (sessionId) {
      this.transport
        .closeSession(sessionId)
        .catch((error: unknown) => console.warn(`Job ${this.requestId}: its session could not be closed: ${errorMessage(error)}`));
    }
  }
}
