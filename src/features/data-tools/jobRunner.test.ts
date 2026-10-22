import { describe, expect, it } from "vitest";
import type { SocketStatus } from "@/lib/api/session-socket";
import type { ClientCommand, RunFileCommand, ServerEvent, SessionInfo } from "@/lib/api/types";
import { JobRunner, type JobSocket, type JobTransport } from "./jobRunner";
import type { JobEvent } from "./jobState";

/* A job's session and socket (spec ⑥ D-06-10): the command goes once, the session closes after the outcome and is
   never reused, a refused socket ends the job, a cancel before the command left keeps it from ever leaving. */

class FakeSocket implements JobSocket {
  isRejected = false;
  open = false;
  sent: ClientCommand[] = [];
  closed = false;
  private events = new Set<(event: ServerEvent) => void>();
  private statuses = new Set<(status: SocketStatus) => void>();
  connect = async () => undefined;
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
}

class FakeTransport implements JobTransport {
  sockets: FakeSocket[] = [];
  closedSessions: string[] = [];
  cancels: [string, string][] = [];
  fail: Error | null = null;
  private resolveOpen: (() => void) | null = null;
  holdOpen = false;
  server = {} as SessionInfo["server"];
  async openSession(): Promise<SessionInfo> {
    if (this.fail) throw this.fail;
    if (this.holdOpen) await new Promise<void>((resolve) => (this.resolveOpen = resolve));
    return { session_id: "s1", profile_id: "p1", database: null, server: this.server };
  }
  release() {
    this.resolveOpen?.();
  }
  async closeSession(sessionId: string) {
    this.closedSessions.push(sessionId);
  }
  async cancel(sessionId: string, requestId: string) {
    this.cancels.push([sessionId, requestId]);
  }
  socket(): JobSocket {
    const socket = new FakeSocket();
    this.sockets.push(socket);
    return socket;
  }
  now = () => 5;
}

const command: RunFileCommand = { type: "run_file", request_id: "r1", path: "/x.sql", mode: "statements", on_error: "stop", from_statement: 1 };
const outcome: ServerEvent = {
  type: "job_outcome", request_id: "r1", job: "run_file", outcome: "completed", cancelled: false, rows: 0, bytes: 0,
  statements: 1, objects: [], errors: [], error_count: 0, warnings: [],
};
const finished: ServerEvent = {
  type: "finished", request_id: "r1", elapsed_ms: 1, in_transaction: false, transaction_failed: false, cancelled: false,
  timed_out: false, skipped: false,
};
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function started() {
  const transport = new FakeTransport();
  const events: JobEvent[] = [];
  const runner = new JobRunner("r1", transport, (event) => events.push(event));
  return { transport, events, runner, types: () => events.map((event) => event.type) };
}

describe("a job's runner", () => {
  it("sends the command once, the first time the socket opens, never on a reconnect", async () => {
    const { transport, runner, types } = started();
    runner.start("p1", null, command);
    await flush();
    const socket = transport.sockets[0];
    socket.status("open");
    socket.status("closed");
    socket.status("open");
    expect(socket.sent).toEqual([command]);
    expect(types()).toEqual(["socket", "sent", "socket", "socket"]);
  });

  it("closes the socket and the session once the outcome is in, and ignores what follows", async () => {
    const { transport, runner, events } = started();
    runner.start("p1", null, command);
    await flush();
    const socket = transport.sockets[0];
    socket.status("open");
    socket.emit({ type: "started", request_id: "r1" });
    socket.emit({ type: "message", request_id: "", severity: "warning", text: "idle transaction" });
    socket.emit({ type: "started", request_id: "other" });
    socket.emit(outcome);
    socket.emit(finished);
    expect(events.filter((event) => event.type === "server").map((event) => event.type === "server" && event.event.type)).toEqual(["started", "job_outcome"]);
    expect([socket.closed, transport.closedSessions]).toEqual([true, ["s1"]]);
  });

  it("ends a job whose socket the backend refuses: its session is gone, nothing is closed", async () => {
    const { transport, runner, types } = started();
    runner.start("p1", null, command);
    await flush();
    const socket = transport.sockets[0];
    socket.status("open");
    socket.status("closed", true);
    expect(types()).toEqual(["socket", "sent", "session_gone"]);
    expect(transport.closedSessions).toEqual([]);
  });

  it("reports a session that could not open, and sends nothing", async () => {
    const { transport, runner, events } = started();
    transport.fail = new Error("A password is needed");
    runner.start("p1", null, command);
    await flush();
    expect(events).toEqual([{ type: "open_failed", error: transport.fail, at: 5 }]);
    expect(transport.sockets).toEqual([]);
  });

  it("never sends a command cancelled while its session opened, and closes the session", async () => {
    const { transport, runner, types } = started();
    transport.holdOpen = true;
    runner.start("p1", null, command);
    runner.cancel();
    transport.release();
    await flush();
    await flush();
    expect(types()).toEqual(["cancel_sent", "not_sent"]);
    expect([transport.sockets.length, transport.closedSessions]).toEqual([0, ["s1"]]);
  });

  it("cancels on the open socket, else through REST", async () => {
    const { transport, runner } = started();
    runner.start("p1", null, command);
    await flush();
    const socket = transport.sockets[0];
    socket.status("open");
    runner.cancel();
    expect(socket.sent).toEqual([command, { type: "cancel", request_id: "r1" }]);
    socket.status("closed");
    runner.cancel();
    expect(transport.cancels).toEqual([["s1", "r1"]]);
  });

  it("only listens to a job listed after a reload", async () => {
    const { transport, runner, types } = started();
    runner.attach("s9");
    const socket = transport.sockets[0];
    socket.status("open");
    socket.emit({ type: "job_progress", request_id: "r1", stage: "statements", rows: 0, bytes: 1, statements: 1, errors: 0 });
    socket.emit(outcome);
    expect(socket.sent).toEqual([]);
    expect(types()).toEqual(["socket", "server", "server"]);
    expect(transport.closedSessions).toEqual(["s9"]);
  });

  it("closes the session of a request that finished without an outcome (the contract says it never does)", async () => {
    const { transport, runner, types } = started();
    runner.start("p1", null, command);
    await flush();
    const socket = transport.sockets[0];
    socket.status("open");
    socket.emit(finished);
    socket.emit(outcome);
    expect(types()).toEqual(["socket", "sent", "server"]);
    expect([socket.closed, transport.closedSessions]).toEqual([true, ["s1"]]);
  });

  it("sends a run with no context chosen in the profile's default, as the session says; none and a choice as they are", async () => {
    const sentWith = async (server: Partial<SessionInfo["server"]>, queryContext?: RunFileCommand["query_context"]) => {
      const { transport, runner } = started();
      transport.server = { engine: "couchbase", version: "7.6.12", ...server };
      runner.start("p1", null, queryContext === undefined ? command : { ...command, query_context: queryContext });
      await flush();
      transport.sockets[0].status("open");
      return transport.sockets[0].sent[0];
    };
    const fallback = { bucket: "dbaguy", scope: "app" };
    const chosen = { bucket: "travel-sample", scope: null };
    expect(await sentWith({ default_query_context: fallback })).toEqual({ ...command, query_context: fallback });
    expect(await sentWith({ default_query_context: fallback }, null)).toEqual({ ...command, query_context: null });
    expect(await sentWith({ default_query_context: fallback }, chosen)).toEqual({ ...command, query_context: chosen });
    expect(await sentWith({ engine: "postgres", default_query_context: null })).toEqual(command);
    expect(await sentWith({})).toEqual(command);
  });

  it("stops listening when detached, and leaves the session to the backend's list", async () => {
    const { transport, runner, types } = started();
    runner.start("p1", null, command);
    await flush();
    const socket = transport.sockets[0];
    socket.status("open");
    runner.detach();
    socket.emit(outcome);
    runner.cancel();
    expect(types()).toEqual(["socket", "sent"]);
    expect([socket.closed, socket.sent.length, transport.closedSessions, transport.cancels]).toEqual([true, 1, [], []]);
  });
});
