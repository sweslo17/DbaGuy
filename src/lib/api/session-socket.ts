import { getBackendInfo } from "../backend";
import { reconnectDelay } from "./backoff";
import type { ClientCommand, ServerEvent } from "./types";

export type SocketStatus = "connecting" | "open" | "closed";
type Listener = (event: ServerEvent) => void;
type StatusListener = (status: SocketStatus) => void;

const CLOSE_UNAUTHORIZED = 4401;
const CLOSE_SESSION_NOT_FOUND = 4404;

/** One WebSocket per session; reconnects with backoff. Commands are never queued: they are sent only while open. */
export class SessionSocket {
  private ws: WebSocket | null = null;
  private readonly listeners = new Set<Listener>();
  private readonly statusListeners = new Set<StatusListener>();
  private closedByUser = false;
  private rejected = false;
  private attempts = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly sessionId: string) {}

  /** True once the backend refused the socket for good (bad token or unknown session): it will not reconnect. */
  get isRejected(): boolean {
    return this.rejected;
  }

  async connect(): Promise<void> {
    const { baseUrl, token } = await getBackendInfo();
    if (this.closedByUser) return;
    const url = `${baseUrl.replace(/^http/, "ws")}/sessions/${this.sessionId}/ws?token=${encodeURIComponent(token)}`;
    this.setStatus("connecting");
    const ws = new WebSocket(url);
    this.ws = ws;
    ws.onopen = () => {
      this.attempts = 0;
      this.setStatus("open");
    };
    ws.onmessage = (message) => {
      const event = JSON.parse(message.data as string) as ServerEvent;
      this.listeners.forEach((listener) => listener(event));
    };
    ws.onclose = (close) => {
      this.ws = null;
      this.rejected = close.code === CLOSE_UNAUTHORIZED || close.code === CLOSE_SESSION_NOT_FOUND;
      this.setStatus("closed");
      this.scheduleReconnect();
    };
  }

  /** Sends only while the WebSocket is open (never queued, so nothing is replayed after a reconnect). */
  trySend(command: ClientCommand): boolean {
    if (this.ws?.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify(command));
    return true;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  onStatus(listener: StatusListener): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  close(): void {
    this.closedByUser = true;
    if (this.timer) clearTimeout(this.timer);
    this.ws?.close();
  }

  /**
   * The next attempt, after a growing delay; until the user closes the socket or the backend rejects it. An attempt
   * that fails before it has a WebSocket (e.g. the backend info cannot be read while the shell restarts it) schedules
   * the next one itself: no `onclose` would.
   */
  private scheduleReconnect(): void {
    if (this.closedByUser || this.rejected) return;
    const delay = reconnectDelay(this.attempts++);
    this.timer = setTimeout(() => {
      this.connect().catch((error: unknown) => {
        console.warn(`Session ${this.sessionId}: reconnect attempt failed`, error);
        this.scheduleReconnect();
      });
    }, delay);
  }

  private setStatus(status: SocketStatus): void {
    this.statusListeners.forEach((listener) => listener(status));
  }
}
