import { describe, expect, it } from "vitest";
import type { ServerEvent } from "@/lib/api/types";
import { applyEvent, firstPageDelivered, interruptExecution, newExecution, type ExecutionState } from "./execution";

/* Folding server events (spec ⑤ §3.5, 05a M6): an apply's outcome, a refused apply, a re-run's first page (A12). */

const fold = (state: ExecutionState, events: ServerEvent[]) => events.reduce(applyEvent, state);
const R = "r1";

describe("an apply request", () => {
  it("keeps its edit_outcome", () => {
    const state = fold(newExecution(R, "edits", ""), [
      { type: "started", request_id: R },
      { type: "edit_outcome", request_id: R, outcome: "committed", applied: ["u:1"], already_gone: [] },
      { type: "finished", request_id: R, elapsed_ms: 3, cancelled: false, timed_out: false, skipped: false },
    ] as ServerEvent[]);
    expect(state.editOutcome?.outcome).toBe("committed");
    expect(state.status).toBe("done");
  });

  it("is not over at a terminal error: a refusal at the WebSocket layer still sends its outcome", () => {
    const refused = fold(newExecution(R, "edits", ""), [
      { type: "error", request_id: R, message: "Invalid command", code: "invalid_command" },
    ] as ServerEvent[]);
    expect(refused.status).toBe("running");
    const query = fold(newExecution(R, "execute", "SELECT 1"), [
      { type: "error", request_id: R, message: "Invalid command", code: "invalid_command" },
    ] as ServerEvent[]);
    expect(query.status).toBe("done"); // other requests as before
  });
});

describe("a re-run's first page", () => {
  const started = fold(newExecution(R, "execute", "SELECT"), [
    { type: "started", request_id: R },
    { type: "result_set", request_id: R, index: 0, columns: [], presentation: "grid" },
    { type: "rows", request_id: R, index: 0, rows: [[1]] },
  ] as ServerEvent[]);

  it("is not delivered while its rows still arrive", () => {
    expect(firstPageDelivered(null)).toBe(false);
    expect(firstPageDelivered(newExecution(R, "execute", "SELECT"))).toBe(false);
    expect(firstPageDelivered(started)).toBe(false);
  });

  it("is delivered once the page ends, pauses, or the request is over", () => {
    const paused = applyEvent(started, {
      type: "result_set_end",
      request_id: R,
      index: 0,
      row_count: 1,
      has_more: true,
    } as ServerEvent);
    expect(firstPageDelivered(paused)).toBe(true);
    const pausedEmpty = fold(newExecution(R, "execute", "SELECT"), [
      { type: "result_set", request_id: R, index: 0, columns: [], presentation: "grid" },
      { type: "result_set_end", request_id: R, index: 0, row_count: 0, has_more: true },
    ] as ServerEvent[]);
    expect(firstPageDelivered(pausedEmpty)).toBe(true); // it paused: the page is all there is for now
    // a Load more on it (running again) does not take the delivered page back
    expect(firstPageDelivered({ ...paused, status: "running" })).toBe(true);
    expect(firstPageDelivered({ ...pausedEmpty, status: "running" })).toBe(true);
    const empty = fold(newExecution(R, "execute", "SELECT"), [
      { type: "result_set", request_id: R, index: 0, columns: [], presentation: "grid" },
      { type: "result_set_end", request_id: R, index: 0, row_count: 0, has_more: false },
    ] as ServerEvent[]);
    expect(firstPageDelivered(empty)).toBe(false);
    expect(firstPageDelivered(interruptExecution(empty))).toBe(true);
  });
});
