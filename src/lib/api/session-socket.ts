import { getBackendInfo } from "../backend";
import type { ClientCommand, ServerEvent } from "./types";

export type SocketStatus = "connecting" | "open" | "closed";
type Listener = (event: ServerEvent) => void;
type StatusListener = (status: SocketStatus) => void;

const CLOSE_UNAUTHORIZED = 4401;
const CLOSE_SESSION_NOT_FOUND = 4404;
const BACKOFF_BASE_MS = 250;
const BACKOFF_MAX_MS = 5000;

/** One WebSocket per session; queues commands until open and reconnects with backoff. */
export class SessionSocket {
  private ws: WebSocket | null = null;
  private readonly listeners = new Set<Listener>();
  private readonly statusListeners = new Set<StatusListener>();
  private readonly pending: ClientCommand[] = [];
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
      for (const command of this.pending.splice(0)) ws.send(JSON.stringify(command));
    };
    ws.onmessage = (message) => {
      const event = JSON.parse(message.data as string) as ServerEvent;
      this.listeners.forEach((listener) => listener(event));
    };
    ws.onclose = (close) => {
      this.ws = null;
      this.rejected = close.code === CLOSE_UNAUTHORIZED || close.code === CLOSE_SESSION_NOT_FOUND;
      this.setStatus("closed");
      if (this.closedByUser || this.rejected) return;
      const delay = Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** this.attempts++);
      this.timer = setTimeout(() => void this.connect(), delay);
    };
  }

  send(command: ClientCommand): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(command));
    else this.pending.push(command);
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

  private setStatus(status: SocketStatus): void {
    this.statusListeners.forEach((listener) => listener(status));
  }
}
