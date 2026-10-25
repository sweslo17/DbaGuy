import type { SocketStatus } from "@/lib/api/session-socket";
import type { ClientCommand, ServerEvent, SessionInfo } from "@/lib/api/types";
import type { JobSocket } from "./jobRunner";

/*
 * Tests only (`jobRunner.test.ts`, `jobsStore.test.ts`; the app never imports it): a job's socket the test drives. It
 * opens, closes, is refused, and speaks when told, as the backend's WebSocket does (D-06-10): a socket attached to a
 * live session gets the session's state first; a refused one is accepted, then closed with 4404, and gets none.
 */

/** What the backend sends first on every socket it attaches to a live session (`manager.attach` → `replay_state`). */
export const SESSION_STATE: ServerEvent = { type: "session_state", autocommit: true, in_transaction: false, transaction_failed: false };

export const sessionInfo = (server: Partial<SessionInfo["server"]> = {}): SessionInfo => ({
  session_id: "s1",
  profile_id: "p1",
  database: null,
  server: server as SessionInfo["server"],
});

export class FakeSocket implements JobSocket {
  isRejected = false;
  open = false;
  closed = false;
  sent: ClientCommand[] = [];
  connects = 0;
  private events = new Set<(event: ServerEvent) => void>();
  private statuses = new Set<(status: SocketStatus) => void>();

  /** `failingConnects`: that many first connects fail before there is a WebSocket (the backend's address unknown). */
  constructor(
    readonly sessionId = "",
    private failingConnects = 0,
  ) {}

  async connect(): Promise<void> {
    this.connects++;
    if (this.failingConnects > 0) {
      this.failingConnects--;
      throw new Error("The backend's address is not known yet");
    }
  }

  trySend(command: ClientCommand): boolean {
    if (!this.open) return false;
    this.sent.push(command);
    return true;
  }

  subscribe(listener: (event: ServerEvent) => void) {
    this.events.add(listener);
    return () => this.events.delete(listener);
  }

  onStatus(listener: (status: SocketStatus) => void) {
    this.statuses.add(listener);
    return () => this.statuses.delete(listener);
  }

  close() {
    this.closed = true;
  }

  status(status: SocketStatus, rejected = false) {
    this.open = status === "open";
    this.isRejected = rejected;
    this.statuses.forEach((listener) => listener(status));
  }

  emit(event: ServerEvent) {
    this.events.forEach((listener) => listener(event));
  }

  /** The backend attached the socket to its live session: it opens, then gets the session's state. */
  accept() {
    this.status("open");
    this.emit(SESSION_STATE);
  }
}
