import { afterEach, describe, expect, it, vi } from "vitest";
import type { ClientCommand, ServerEvent } from "@/lib/api/types";
import { commitState } from "../edits/commitFlow";
import { busy, SessionController } from "./SessionController";

/*
 * An apply on the real controller (spec ⑤ R-05-6, pre-flight H1): a command that never left the socket is not sent and
 * nothing happened; one that left and lost its outcome is unknown and is never sent again. The backend is a fake: the
 * session POST answers at once and the socket is one the test drives.
 */

// Hoisted with the mocks below (vi.mock runs before the imports).
const { FakeSocket } = vi.hoisted(() => {
  class FakeSocket {
    static last: FakeSocket | null = null;
    readonly sent: ClientCommand[] = [];
    open = false;
    isRejected = false;
    private listeners: ((event: ServerEvent) => void)[] = [];
    private statusListeners: ((status: "connecting" | "open" | "closed") => void)[] = [];

    constructor(readonly sessionId: string) {
      FakeSocket.last = this;
    }
    subscribe(listener: (event: ServerEvent) => void) {
      this.listeners.push(listener);
    }
    onStatus(listener: (status: "connecting" | "open" | "closed") => void) {
      this.statusListeners.push(listener);
    }
    connect(): Promise<void> {
      this.status("open");
      return Promise.resolve();
    }
    trySend(command: ClientCommand): boolean {
      if (!this.open) return false;
      this.sent.push(command);
      return true;
    }
    close() {
      this.open = false;
    }
    status(status: "open" | "closed") {
      this.open = status === "open";
      this.statusListeners.forEach((listener) => listener(status));
    }
    emit(event: object) {
      this.listeners.forEach((listener) => listener(event as ServerEvent));
    }
  }
  return { FakeSocket };
});
type FakeSocket = InstanceType<typeof FakeSocket>;

vi.mock("@/lib/api/session-socket", () => ({ SessionSocket: FakeSocket }));
vi.mock("@/lib/api/client", async (original) => ({
  ...(await original<typeof import("@/lib/api/client")>()),
  api: async () => ({
    POST: async () => ({ data: { session_id: "s1", server: {} }, response: new Response(null, { status: 201 }) }),
    DELETE: async () => ({ data: undefined, response: new Response(null, { status: 204 }) }),
  }),
}));

afterEach(() => {
  FakeSocket.last = null;
});

async function opened(): Promise<{ controller: SessionController; socket: FakeSocket }> {
  const controller = new SessionController("p1", null);
  await controller.open();
  const socket = FakeSocket.last;
  if (!socket || controller.store.getState().phase !== "open") throw new Error("the fake session did not open");
  return { controller, socket };
}

const execution = (controller: SessionController, id: string) => controller.store.getState().executions[id];
const applies = (socket: FakeSocket) => socket.sent.filter((command) => command.type === "apply_edits");

describe("an apply on the session", () => {
  it("never leaves while the session is not open: not sent, nothing happened, the session is not busy", () => {
    const controller = new SessionController("p1", null); // never opened: no socket
    const id = controller.applyEdits("plan-1");
    expect(commitState(execution(controller, id))).toEqual({ kind: "not_sent" });
    expect(busy(controller.store.getState())).toBe(false);
  });

  it("never leaves while the socket reconnects", async () => {
    const { controller, socket } = await opened();
    socket.status("closed");
    const id = controller.applyEdits("plan-1");
    expect(commitState(execution(controller, id))).toEqual({ kind: "not_sent" });
    expect(applies(socket)).toEqual([]);
  });

  it("is sent once with its plan id, and keeps the session busy until its outcome and finished", async () => {
    const { controller, socket } = await opened();
    const id = controller.applyEdits("plan-1");
    expect(applies(socket)).toEqual([{ type: "apply_edits", request_id: id, plan_id: "plan-1" }]);
    expect(execution(controller, id)).toMatchObject({ kind: "edits", status: "running" });
    expect(busy(controller.store.getState())).toBe(true);
    for (const event of [
      { type: "started", request_id: id },
      { type: "affected", request_id: id, index: 0, rows: 1 },
      { type: "edit_outcome", request_id: id, outcome: "committed", applied: ["u:1"], already_gone: [] },
      { type: "session_state", autocommit: true, in_transaction: false, transaction_failed: false },
      { type: "finished", request_id: id, elapsed_ms: 4, cancelled: false, timed_out: false, skipped: false },
    ]) {
      socket.emit(event);
    }
    expect(commitState(execution(controller, id))).toMatchObject({ kind: "ended", outcome: "committed" });
    expect(busy(controller.store.getState())).toBe(false);
  });

  it("is unknown when its socket drops and comes back without the outcome, and is never sent again", async () => {
    const { controller, socket } = await opened();
    const id = controller.applyEdits("plan-1");
    socket.emit({ type: "started", request_id: id });
    socket.status("closed");
    expect(commitState(execution(controller, id))).toEqual({ kind: "running", done: 0 }); // still waiting
    socket.status("open");
    expect(commitState(execution(controller, id))).toMatchObject({ kind: "ended", outcome: "unknown", event: null });
    // a late outcome on the new socket does not change it: some events were lost
    socket.emit({ type: "edit_outcome", request_id: id, outcome: "rolled_back", applied: [], already_gone: [] });
    expect(commitState(execution(controller, id))).toMatchObject({ outcome: "unknown" });
    expect(applies(socket)).toHaveLength(1);
  });

  it("keeps an outcome that arrived before the socket dropped", async () => {
    const { controller, socket } = await opened();
    const id = controller.applyEdits("plan-1");
    socket.emit({ type: "edit_outcome", request_id: id, outcome: "committed", applied: ["u:1"], already_gone: [] });
    socket.status("closed");
    socket.status("open");
    expect(commitState(execution(controller, id))).toMatchObject({ kind: "ended", outcome: "committed" });
  });

  it("is unknown when the session closes mid-apply", async () => {
    const { controller } = await opened();
    const id = controller.applyEdits("plan-1");
    controller.close();
    expect(commitState(execution(controller, id))).toMatchObject({ kind: "ended", outcome: "unknown" });
  });

  it("reads a refusal at the WebSocket layer as rolled back: its error does not end the apply", async () => {
    const { controller, socket } = await opened();
    const id = controller.applyEdits("plan-1");
    socket.emit({ type: "error", request_id: id, message: "Invalid command: …", code: "invalid_command" });
    expect(commitState(execution(controller, id))).toEqual({ kind: "running", done: 0 });
    socket.emit({ type: "edit_outcome", request_id: id, outcome: "rolled_back", applied: [], already_gone: [] });
    socket.emit({ type: "finished", request_id: id, elapsed_ms: 0, cancelled: false, timed_out: false, skipped: false });
    expect(commitState(execution(controller, id))).toMatchObject({ kind: "ended", outcome: "rolled_back" });
  });
});
