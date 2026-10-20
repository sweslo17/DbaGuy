import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api/client";
import type { EditOutcomeEvent, EditPreview, ServerEvent } from "@/lib/api/types";
import {
  applyEvent,
  INTERRUPTED_MESSAGE,
  interruptExecution,
  newExecution,
  type ExecutionState,
} from "../session/execution";
import {
  abandonedCommit,
  changeLabel,
  commitReport,
  commitState,
  commitToast,
  commitWarning,
  listedMessages,
  LOST_TEXT,
  CLOSE_UNKNOWN_LABEL,
  mayStopWaiting,
  NOT_SENT_OUTCOME,
  OUTCOME_WAIT_MS,
  PARTIAL_NOTE,
  problemsOf,
  rerunAfter,
  ROLLBACK_UNCONFIRMED,
  rollbackToast,
  ROLLED_BACK_TEXT,
  SQL_MODE_NOTE,
  SQLITE_BUSY_NOTE,
  stagedOutcome,
  STOPPED_WAITING_TEXT,
  TRANSACTION_OPEN_NOTE,
  UNKNOWN_NOTE,
  type EndedCommit,
} from "./commitFlow";
import { editsErrorMessage } from "./editsApi";
import { emptyStaged, rowKeyFromKey, stage } from "./staging";

/*
 * An apply's outcome as the frontend reads it (spec ⑤ R-05-6, the 05a appendix's event shapes): exactly one
 * edit_outcome; an apply that ends without one is unknown and never resent; a command that never left is not sent.
 */

const R = "apply";
const apply = (events: object[]): ExecutionState =>
  events.reduce<ExecutionState>(
    (state, event) => applyEvent(state, { request_id: R, ...event } as ServerEvent),
    newExecution(R, "edits", ""),
  );
const finished = (cancelled = false, inTransaction = false) => ({
  type: "finished",
  elapsed_ms: 5,
  in_transaction: inTransaction,
  cancelled,
  timed_out: false,
  skipped: false,
});
const outcome = (value: string, extra: object = {}) => ({
  type: "edit_outcome",
  outcome: value,
  applied: [],
  already_gone: [],
  ...extra,
});
const endedOf = (execution: ExecutionState) => commitState(execution) as EndedCommit;

describe("reading an apply's events", () => {
  it("counts the statements while it runs", () => {
    const running = apply([
      { type: "started" },
      { type: "affected", index: 0, rows: 1 },
      { type: "affected", index: 1, rows: 1 },
    ]);
    expect(commitState(running)).toEqual({ kind: "running", done: 2 });
  });

  it("reads a commit", () => {
    const done = apply([
      { type: "started" },
      { type: "affected", index: 0, rows: 1 },
      outcome("committed", { applied: ["u:1"], already_gone: ["d:2"] }),
      { type: "session_state", autocommit: true, in_transaction: false, transaction_failed: false },
      finished(),
    ]);
    expect(commitState(done)).toMatchObject({ kind: "ended", outcome: "committed" });
    expect(stagedOutcome(endedOf(done))).toEqual({
      outcome: "committed",
      applied: ["u:1"],
      failedChange: null,
      message: null,
    });
  });

  it("reads a rollback with the failing statement and the server's text", () => {
    const failed = apply([
      { type: "started" },
      { type: "error", message: "Row id = 7 was changed or deleted since it was loaded", code: "edit_conflict" },
      outcome("rolled_back", { failed_change: "u:1", statement: 0 }),
      finished(),
    ]);
    const commit = endedOf(failed);
    expect(stagedOutcome(commit)).toEqual({
      outcome: "rolled_back",
      applied: [],
      failedChange: "u:1",
      message: "Row id = 7 was changed or deleted since it was loaded",
    });
    expect(commitReport(commit, { total: 3, sqlModeRefusals: 0 })).toMatchObject({
      tone: "danger",
      title: "Nothing was applied",
      failedStatement: 0,
      actions: ["show_in_grid"],
    });
  });

  it("reads a refusal at the WebSocket layer as rolled back, not unknown", () => {
    const refused = apply([
      { type: "error", message: "Busy", code: "invalid_command" },
      outcome("rolled_back"),
      finished(),
    ]);
    expect(commitState(refused)).toMatchObject({ kind: "ended", outcome: "rolled_back" });
  });

  it("reads an apply cancelled while queued as rolled back and cancelled", () => {
    const queued = apply([
      { type: "started" },
      { type: "message", severity: "warning", text: "Query cancelled before it started" },
      outcome("rolled_back"),
      finished(true),
    ]);
    expect(commitReport(endedOf(queued), { total: 1, sqlModeRefusals: 0 }).title).toBe(
      "Cancelled: nothing was applied",
    );
  });

  it("says a session that closed before the apply started closed: the user cancelled nothing", () => {
    const closed = endedOf(
      apply([
        { type: "started" },
        { type: "message", severity: "warning", text: "Session closed before the query started" },
        outcome("rolled_back"),
        { type: "session_state", autocommit: true, in_transaction: false, transaction_failed: false },
        finished(true),
      ]),
    );
    expect(commitReport(closed, { total: 1, sqlModeRefusals: 0 })).toMatchObject({
      tone: "danger",
      title: "The session closed before the commit started: nothing was applied",
      detail: null,
      actions: [],
    });
    expect(stagedOutcome(closed)).toEqual(NOT_SENT_OUTCOME); // rolled back; no row turns red
  });

  it("calls an apply that ended without its outcome unknown", () => {
    const lost = interruptExecution(apply([{ type: "started" }, { type: "affected", index: 0, rows: 1 }]));
    const commit = endedOf(lost);
    expect(commit).toMatchObject({ kind: "ended", outcome: "unknown", event: null });
    expect(commitReport(commit, { total: 2, sqlModeRefusals: 0 })).toMatchObject({
      title: "Outcome unknown",
      detail: LOST_TEXT,
      actions: ["refresh"],
    });
  });

  it("keeps an outcome that arrived before the socket went", () => {
    const late = interruptExecution(apply([{ type: "started" }, outcome("committed", { applied: ["u:1"] })]));
    expect(commitState(late)).toMatchObject({ outcome: "committed" });
  });

  it("calls a command that never left not sent", () => {
    const never = {
      ...newExecution(R, "edits", ""),
      status: "done" as const,
      error: { message: "Not connected", code: "not_connected" },
    };
    expect(commitState(never)).toEqual({ kind: "not_sent" });
  });
});

describe("what the result says", () => {
  const rolledBack = (code: string) =>
    endedOf(apply([{ type: "error", message: "refused", code }, outcome("rolled_back"), finished()]));

  it("offers Roll back and Reconnect for an open transaction", () => {
    expect(commitReport(rolledBack("transaction_open"), { total: 1, sqlModeRefusals: 0 })).toMatchObject({
      notes: [TRANSACTION_OPEN_NOTE],
      actions: ["rollback", "reconnect"],
    });
  });

  it("offers a new preview for an expired plan, and for a first sql_mode refusal; Reconnect once it repeats", () => {
    expect(commitReport(rolledBack("plan_expired"), { total: 1, sqlModeRefusals: 0 }).actions).toEqual([
      "preview_again",
    ]);
    expect(commitReport(rolledBack("sql_mode_changed"), { total: 1, sqlModeRefusals: 1 }).actions).toEqual([
      "preview_again",
    ]);
    expect(commitReport(rolledBack("sql_mode_changed"), { total: 1, sqlModeRefusals: 2 })).toMatchObject({
      notes: [SQL_MODE_NOTE],
      actions: ["reconnect"],
    });
  });

  it("explains a busy SQLite file", () => {
    expect(commitReport(rolledBack("SQLITE_BUSY"), { total: 1, sqlModeRefusals: 0 }).notes).toEqual([SQLITE_BUSY_NOTE]);
  });

  it("counts a partial outcome", () => {
    const partial = endedOf(
      apply([outcome("partial", { applied: ["d:1"], failed_change: "u:2", statement: 1 }), finished()]),
    );
    expect(commitReport(partial, { total: 3, sqlModeRefusals: 0 }).title).toBe(
      "Partly applied: 1 of 3 changes took effect",
    );
  });

  it("toasts a commit with the rows already gone and where new rows went", () => {
    const event: EditOutcomeEvent = {
      type: "edit_outcome",
      request_id: R,
      outcome: "committed",
      applied: ["u:1", "d:2", "i:3"],
      already_gone: ["d:2"],
    };
    expect(commitToast(event, "public.orders", 0)).toBe(
      "Committed 3 changes to public.orders · 1 row was already gone",
    );
    expect(commitToast(event, "orders", 1)).toMatch(/New rows appear where the table's order puts them/);
  });

  it("runs the browse again after a commit or a partial one, after a rollback only when it was paused", () => {
    expect([rerunAfter("committed", false), rerunAfter("partial", false), rerunAfter("unknown", true)]).toEqual([
      true,
      true,
      false,
    ]);
    expect([rerunAfter("rolled_back", true), rerunAfter("rolled_back", false)]).toEqual([true, false]);
  });
});

describe("a preview's problems and the changes they name", () => {
  it("groups the problems by change, a key collision on the change as a whole", () => {
    const preview = {
      problems: [
        { change_id: "u:1", column: "n", code: "invalid_number", message: "Not a number" },
        { change_id: "u:1", column: null, code: "key_collision", message: "This change moves a row onto the key…" },
      ],
    } as EditPreview;
    expect(problemsOf(preview)).toEqual({
      "u:1": [
        { column: "n", message: "Not a number" },
        { column: null, message: "This change moves a row onto the key…" },
      ],
    });
  });

  it("names a change by its key, or as a new row", () => {
    const staged = stage(
      stage(
        emptyStaged(["id"]),
        {
          type: "deleteRows",
          rows: [
            { kind: "row", rowKey: rowKeyFromKey({ id: "9007199254740993" }, ["id"]), key: { id: "9007199254740993" } },
          ],
        },
        () => "d:1",
      ),
      { type: "addRow", id: "i:2" },
    );
    expect(changeLabel(staged, "d:1")).toBe("Row id = 9007199254740993");
    expect(changeLabel(staged, "i:2")).toBe("New row");
  });
});

describe("the text of a failed /edits request", () => {
  it("is a 422's first detail when there is one, else its message", () => {
    const detail = [
      {
        type: "commit_too_large",
        loc: ["body", "changes"],
        msg: "The changes are too large: at most 64 MiB of values per commit",
      },
    ];
    expect(editsErrorMessage(new ApiError("Invalid request", "validation_failed", 422, detail))).toBe(detail[0].msg);
    expect(editsErrorMessage(new ApiError("Not a grid cell: …", "validation_failed", 422))).toBe("Not a grid cell: …");
    expect(editsErrorMessage(new ApiError("This connection is read-only", "read_only", 403))).toBe(
      "This connection is read-only",
    );
  });
});

/*
 * Every apply code of the 05a contract, read the way the dialog shows it (the appendix's `error.code` table and event
 * shapes). A refusal or a failure before COMMIT is "Nothing was applied"; only a missing or unknown outcome is unknown.
 */
describe("every code an apply can end with", () => {
  const report = (events: object[], context = { total: 2, sqlModeRefusals: 0 }) =>
    commitReport(endedOf(apply(events)), context);
  const refusedWith = (code: string | null, message = "refused", extra: object = {}) =>
    report([{ type: "started" }, { type: "error", message, code }, outcome("rolled_back", extra), finished()]);

  it.each([
    ["read_only"],
    ["invalid_text"],
    ["transaction_failed"],
    ["row_count_unknown"],
    ["session_busy"],
    ["invalid_command"],
    ["23505"], // PostgreSQL SQLSTATE
    ["1406"], // MySQL errno
    ["547"], // SQL Server number
    ["SQLITE_CONSTRAINT_UNIQUE"],
    [null], // "Internal error: …"
  ])("%s: nothing was applied, with the server's text and nothing else to do", (code) => {
    expect(refusedWith(code, "the server's text")).toEqual({
      tone: "danger",
      title: "Nothing was applied",
      detail: "the server's text",
      notes: [],
      actions: [],
      failedChange: null,
      failedStatement: null,
      failedPosition: null,
    });
  });

  it("names the change and the place in its statement when one failed (edit_conflict, a server error)", () => {
    const position = { line: 1, column: 8 };
    const failed = report([
      { type: "started" },
      { type: "affected", index: 0, rows: 1 },
      {
        type: "error",
        message: "Row id = 7 matched 2 rows: its key is not unique. Nothing was applied",
        code: "edit_conflict",
        position,
      },
      outcome("rolled_back", { failed_change: "u:2", statement: 1 }),
      finished(),
    ]);
    expect(failed).toMatchObject({
      failedChange: "u:2",
      failedStatement: 1,
      failedPosition: position,
      actions: ["show_in_grid"],
    });
    // a position without a failed statement (a COMMIT error) has no statement to sit in
    const commitError = report([
      { type: "error", message: "x", code: "23503", position },
      outcome("rolled_back"),
      finished(),
    ]);
    expect(commitError.failedPosition).toBeNull();
  });

  it("offers a new preview for a mismatched plan as for an expired one", () => {
    expect(refusedWith("plan_mismatch").actions).toEqual(["preview_again"]);
  });

  it("offers Reconnect, once, when the database connection was lost before COMMIT", () => {
    expect(refusedWith("connection_lost", "Lost connection to MySQL server during query")).toMatchObject({
      title: "Nothing was applied",
      actions: ["reconnect"],
    });
    expect(refusedWith("transaction_open").actions).toEqual(["rollback", "reconnect"]);
  });

  const ROLLBACK_FAILED_TEXT = "The transaction could not be rolled back: close and reopen this tab";

  it("offers Reconnect when the rollback failed and left the transaction open", () => {
    const stuck = (warning: object[], inTransaction: boolean) =>
      report([
        { type: "started" },
        { type: "error", message: "deadlock", code: "1213" },
        ...warning,
        outcome("rolled_back"),
        finished(false, inTransaction),
      ]);
    const warning = [{ type: "message", severity: "warning", text: ROLLBACK_FAILED_TEXT }];
    expect(stuck(warning, true).actions).toEqual(["reconnect"]); // as the backend sends it
    expect(stuck([], true).actions).toEqual(["reconnect"]); // finished.in_transaction alone says it
    expect(stuck(warning, false).actions).toEqual(["reconnect"]); // the text alone, as a fallback
    expect(stuck([], false).actions).toEqual([]);
    const unknown = report([outcome("unknown"), finished(false, true)]);
    expect(unknown.actions).toEqual(["refresh", "reconnect"]);
    // a repeated sql_mode refusal whose rollback after BEGIN failed: two reasons to reconnect, one button
    const twice = report(
      [
        { type: "error", message: "The session's sql_mode changed since the preview: …", code: "sql_mode_changed" },
        { type: "message", severity: "warning", text: ROLLBACK_FAILED_TEXT },
        outcome("rolled_back"),
        finished(),
      ],
      { total: 1, sqlModeRefusals: 2 },
    );
    expect(twice.actions).toEqual(["reconnect"]);
  });

  it("explains every busy or locked SQLite code, and only those", () => {
    for (const code of ["SQLITE_BUSY", "SQLITE_BUSY_SNAPSHOT", "SQLITE_LOCKED", "SQLITE_LOCKED_SHAREDCACHE"]) {
      expect(refusedWith(code).notes).toEqual([SQLITE_BUSY_NOTE]);
    }
    expect(refusedWith("SQLITE_CONSTRAINT").notes).toEqual([]);
  });

  it("calls a cancel a cancel, even before its finished arrives, without saying it twice", () => {
    const cancelled = report([
      { type: "started" },
      { type: "error", message: "Cancelled: nothing was applied", code: "cancelled" },
      outcome("rolled_back"),
      finished(true),
    ]);
    expect(cancelled).toMatchObject({ title: "Cancelled: nothing was applied", detail: null });
    const codeOnly = report([
      { type: "error", message: "Cancelled: nothing was applied", code: "cancelled" },
      outcome("rolled_back"),
      finished(false),
    ]);
    expect(codeOnly.title).toBe("Cancelled: nothing was applied");
  });

  it("reads an unknown outcome the backend sent with its own text, and offers Reconnect for a lost connection", () => {
    const left = report([
      { type: "started" },
      { type: "error", message: "COMMIT left a transaction open, which was rolled back: …", code: "outcome_unknown" },
      outcome("unknown"),
      finished(),
    ]);
    expect(left).toMatchObject({
      tone: "warning",
      title: "Outcome unknown",
      detail: "COMMIT left a transaction open, which was rolled back: …",
      notes: [UNKNOWN_NOTE],
      actions: ["refresh"],
    });
    const lost = report([
      { type: "error", message: "server closed the connection unexpectedly", code: "connection_lost" },
      outcome("unknown"),
      finished(),
    ]);
    expect(lost.actions).toEqual(["refresh", "reconnect"]);
    expect(report([outcome("unknown"), finished()]).detail).toBeNull(); // the backend's outcome, not "connection lost"
  });

  it("counts a partial outcome with grouped digits and its note", () => {
    const applied = Array.from({ length: 1234 }, (_, i) => `d:${i}`);
    const partial = report([outcome("partial", { applied }), finished()], { total: 5000, sqlModeRefusals: 0 });
    expect(partial).toMatchObject({
      tone: "warning",
      title: "Partly applied: 1,234 of 5,000 changes took effect",
      notes: [PARTIAL_NOTE],
      actions: [],
    });
    // its rollback failed: the transaction is still open on the session
    const stuck = report([outcome("partial", { applied: ["d:1"], failed_change: "u:2" }), finished(false, true)], {
      total: 2,
      sqlModeRefusals: 0,
    });
    expect(stuck.actions).toEqual(["show_in_grid", "reconnect"]);
  });

  it("reports a commit with the rows already gone", () => {
    const committed = report([
      outcome("committed", { applied: ["d:1", "d:2", "u:3"], already_gone: ["d:1", "d:2"] }),
      finished(),
    ]);
    expect(committed).toMatchObject({
      tone: "success",
      title: "Committed 3 changes",
      notes: ["2 rows were already gone"],
      actions: [],
    });
  });
});

describe("an outcome is never read as clean when it is not known", () => {
  it("is unknown when an error came but the outcome did not (the socket went between them)", () => {
    const lost = interruptExecution(
      apply([{ type: "started" }, { type: "error", message: "Row id = 7 was changed…", code: "edit_conflict" }]),
    );
    const commit = endedOf(lost);
    expect(commit).toMatchObject({ outcome: "unknown", event: null });
    expect(commitReport(commit, { total: 1, sqlModeRefusals: 0 }).detail).toBe(LOST_TEXT);
    expect(stagedOutcome(commit)).toMatchObject({ outcome: "unknown", applied: [], failedChange: null });
  });

  it("is unknown when the request finished without an outcome", () => {
    expect(commitState(apply([{ type: "started" }, finished()]))).toMatchObject({ kind: "ended", outcome: "unknown" });
  });

  it("is still running after an edit_outcome until its finished (the cancel and the title come with it)", () => {
    expect(commitState(apply([{ type: "started" }, outcome("rolled_back")]))).toEqual({ kind: "running", done: 0 });
  });

  it("is not sent only when the command never left: a sent apply's own error never reads as not sent", () => {
    const sentThenFailed = apply([{ type: "error", message: "Not connected", code: "connection_lost" }, finished()]);
    expect(commitState(sentThenFailed)).toMatchObject({ kind: "ended", outcome: "unknown" });
  });

  it("leaves DbaGuy's interruption line out of an apply's messages: the outcome says what it means", () => {
    const NOTICE = "NOTICE: audited";
    const late = interruptExecution(
      apply([{ type: "started" }, { type: "message", severity: "info", text: NOTICE }, outcome("committed")]),
    );
    expect(endedOf(late).messages).toEqual([{ severity: "info", text: NOTICE }]);
    // the same text from the server, on a request that was not interrupted, is the server's and stays
    const server = { type: "message", severity: "warning", text: INTERRUPTED_MESSAGE };
    expect(endedOf(apply([server, outcome("rolled_back"), finished()])).messages).toHaveLength(1);
  });
});

describe("what the staging takes", () => {
  it("takes nothing from a command that never left, and clears the apply's mark (pre-flight H1)", () => {
    expect(stagedOutcome({ kind: "not_sent" })).toEqual({
      outcome: "rolled_back",
      applied: [],
      failedChange: null,
      message: null,
    });
    expect(stagedOutcome({ kind: "not_sent" })).toBe(NOT_SENT_OUTCOME);
  });

  it("never turns a cancel into the change's problem (pre-flight L24)", () => {
    const cancelled = endedOf(
      apply([
        { type: "started" },
        { type: "error", message: "Cancelled: nothing was applied", code: "cancelled" },
        outcome("rolled_back", { failed_change: "u:1", statement: 0 }),
        finished(true),
      ]),
    );
    expect(stagedOutcome(cancelled)).toEqual({
      outcome: "rolled_back",
      applied: [],
      failedChange: "u:1",
      message: null,
    });
    const queued = endedOf(apply([{ type: "started" }, outcome("rolled_back"), finished(true)]));
    expect(stagedOutcome(queued).message).toBeNull();
  });

  it("keeps what took effect of a partial outcome", () => {
    const partial = endedOf(
      apply([
        { type: "error", message: "Row id = 2 was changed…", code: "edit_conflict" },
        outcome("partial", { applied: ["d:1"], failed_change: "u:2", statement: 1 }),
        finished(),
      ]),
    );
    expect(stagedOutcome(partial)).toEqual({
      outcome: "partial",
      applied: ["d:1"],
      failedChange: "u:2",
      message: "Row id = 2 was changed…",
    });
  });
});

describe("when the outcome does not come (pre-flight M2)", () => {
  it("closes with an unknown outcome, or the one that arrived, or not sent", () => {
    expect(abandonedCommit(null)).toEqual({
      kind: "ended",
      outcome: "unknown",
      event: null,
      error: null,
      messages: [],
      cancelled: false,
      inTransaction: false,
    });
    const waiting = apply([{ type: "started" }, { type: "affected", index: 0, rows: 1 }]);
    expect(abandonedCommit(waiting)).toMatchObject({ kind: "ended", outcome: "unknown", event: null });
    const answered = apply([{ type: "started" }, outcome("committed", { applied: ["u:1"] })]);
    expect(abandonedCommit(answered)).toMatchObject({ kind: "ended", outcome: "committed" });
    const never = {
      ...newExecution(R, "edits", ""),
      status: "done" as const,
      error: { message: "Not connected", code: "not_connected" },
    };
    expect(abandonedCommit(never)).toEqual({ kind: "not_sent" });
  });

  it("says the user stopped waiting, not that the connection was lost, after Close — outcome unknown (ruling K21)", () => {
    const waiting = apply([{ type: "started" }, { type: "affected", index: 0, rows: 1 }]);
    const stopped = abandonedCommit(waiting, true) as EndedCommit;
    expect(stopped).toMatchObject({ kind: "ended", outcome: "unknown", event: null, stoppedWaiting: true });
    expect(commitReport(stopped, { total: 3, sqlModeRefusals: 0 })).toMatchObject({
      title: "Outcome unknown",
      detail: STOPPED_WAITING_TEXT,
      notes: [UNKNOWN_NOTE],
      actions: ["refresh"],
    });
    expect((abandonedCommit(null, true) as EndedCommit).stoppedWaiting).toBe(true);
    // A vanished execution, or a lost socket, still says the connection was lost.
    expect(commitReport(abandonedCommit(waiting) as EndedCommit, { total: 3, sqlModeRefusals: 0 }).detail).toBe(LOST_TEXT);
    expect(commitReport(abandonedCommit(null) as EndedCommit, { total: 3, sqlModeRefusals: 0 }).detail).toBe(LOST_TEXT);
    // An outcome that arrived, or a command that never left, is what it is.
    const answered = apply([{ type: "started" }, outcome("committed", { applied: ["u:1"] })]);
    expect(abandonedCommit(answered, true)).not.toHaveProperty("stoppedWaiting");
    const unknownSent = apply([
      { type: "started" },
      { type: "error", message: "lost during COMMIT", code: "outcome_unknown" },
      outcome("unknown"),
    ]);
    const kept = abandonedCommit(unknownSent, true) as EndedCommit;
    expect(kept.stoppedWaiting).toBeUndefined();
    expect(commitReport(kept, { total: 1, sqlModeRefusals: 0 }).detail).toBe("lost during COMMIT");
    const never = { ...newExecution(R, "edits", ""), status: "done" as const, error: { message: "Not connected", code: "not_connected" } };
    expect(abandonedCommit(never, true)).toEqual({ kind: "not_sent" });
  });

  it("offers to close it unknown once the session is not open, or once COMMIT has gone unanswered too long", () => {
    expect(CLOSE_UNKNOWN_LABEL).toBe("Close — outcome unknown");
    const wait = { phase: "open" as const, counted: 3, statements: 3, quietMs: OUTCOME_WAIT_MS, sinceCancelMs: null };
    expect(mayStopWaiting(wait)).toBe(true);
    expect(mayStopWaiting({ ...wait, quietMs: OUTCOME_WAIT_MS - 1 })).toBe(false);
    expect(mayStopWaiting({ ...wait, counted: 2 })).toBe(false); // a statement still runs: Cancel is the way out
    expect(mayStopWaiting({ ...wait, counted: 2, quietMs: 0, phase: "reconnecting" })).toBe(true);
    expect(mayStopWaiting({ ...wait, quietMs: 0, phase: "failed" })).toBe(true);
    expect(mayStopWaiting({ ...wait, quietMs: 0, phase: "closed" })).toBe(true);
  });

  it("offers it when a Cancel brought nothing back for OUTCOME_WAIT_MS (ruling K11)", () => {
    const tenMinutes = 10 * 60_000;
    // the review's case: nothing counted of 3, Cancel sent, nothing back for ten minutes
    const stuck = { phase: "open" as const, counted: 0, statements: 3, quietMs: tenMinutes, sinceCancelMs: tenMinutes };
    expect(mayStopWaiting(stuck)).toBe(true);
    expect(mayStopWaiting({ ...stuck, sinceCancelMs: null })).toBe(false); // no Cancel yet: it is the way out
    expect(mayStopWaiting({ ...stuck, sinceCancelMs: OUTCOME_WAIT_MS - 1 })).toBe(false); // the Cancel is fresh
    expect(mayStopWaiting({ ...stuck, quietMs: OUTCOME_WAIT_MS - 1 })).toBe(false); // an event came since it
    expect(mayStopWaiting({ ...stuck, quietMs: OUTCOME_WAIT_MS, sinceCancelMs: OUTCOME_WAIT_MS })).toBe(true);
  });
});

describe("a commit's messages (pre-flight L10)", () => {
  const committed = (messages: { severity: string; text: string }[]) =>
    endedOf(apply([...messages.map((m) => ({ type: "message", ...m })), outcome("committed"), finished()]));
  const COMMIT_WARNING = "COMMIT reported an error after every statement had taken effect: disk full";

  it("toasts the COMMIT warning and lists nothing else when there is nothing else", () => {
    const commit = committed([{ severity: "warning", text: COMMIT_WARNING }]);
    expect(commitWarning(commit)).toBe(COMMIT_WARNING);
    expect(listedMessages(commit)).toEqual([]);
  });

  it("lists a trigger's messages, warnings among them, so the result stays open", () => {
    const notice = { severity: "info", text: "NOTICE: row audited" };
    const warning = { severity: "warning", text: "WARNING: price below cost" };
    const commit = committed([notice, warning]);
    expect(commitWarning(commit)).toBeNull();
    expect(listedMessages(commit)).toEqual([notice, warning]);
    expect(listedMessages(committed([{ severity: "info", text: COMMIT_WARNING }]))).toHaveLength(1); // only a warning
  });
});

describe("the answer of Roll back (pre-flight L22)", () => {
  const rollback = (events: object[]) =>
    events.reduce<ExecutionState>(
      (state, event) => applyEvent(state, { request_id: "rb", ...event } as ServerEvent),
      newExecution("rb", "transaction", ""),
    );

  it("says rolled back only once the rollback answered without an error", () => {
    expect(rollbackToast(rollback([{ type: "started" }, finished()]))).toEqual({
      kind: "success",
      text: ROLLED_BACK_TEXT,
    });
    const failed = rollback([{ type: "error", message: "no connection", code: "connection_lost" }, finished()]);
    expect(rollbackToast(failed)).toEqual({ kind: "error", text: "Could not roll back: no connection" });
    expect(rollbackToast(interruptExecution(rollback([{ type: "started" }])))).toEqual({
      kind: "warning",
      text: ROLLBACK_UNCONFIRMED,
    });
    expect(rollbackToast(rollback([{ type: "started" }, finished(true)])).text).toBe(ROLLBACK_UNCONFIRMED);
  });
});

describe("the problems of a preview, whatever the ids", () => {
  it("keeps an id that names an object's own properties as its own entry", () => {
    const preview = {
      problems: [
        { change_id: "__proto__", column: "n", code: "invalid_number", message: "Not a number" },
        { change_id: "constructor", column: null, code: "key_collision", message: "collides" },
      ],
    } as EditPreview;
    const problems = problemsOf(preview);
    expect(Object.hasOwn(problems, "__proto__")).toBe(true);
    expect(Object.getPrototypeOf(problems)).toBe(Object.prototype);
    expect(problems.constructor).toEqual([{ column: null, message: "collides" }]);
  });
});

describe("the text of a failed /edits request, every shape of the contract", () => {
  it("is the error's own message for the 128 MiB plan, a missing password and a non-422 with a detail", () => {
    const tooLarge =
      "The statements are too large to keep for the commit: at most 128 MiB of text. Commit fewer changes at once";
    expect(editsErrorMessage(new ApiError(tooLarge, "validation_failed", 422))).toBe(tooLarge);
    const locked = new ApiError("Password required", "password_required", 428, { needs: ["password"] });
    expect(editsErrorMessage(locked)).toBe("Password required");
    expect(editsErrorMessage(new ApiError("Locked", "query_failed", 400, [{ msg: "not this" }]))).toBe("Locked");
  });

  it("falls back to the message when the detail has no text", () => {
    for (const detail of [[], [{ msg: "" }], [{ loc: ["body"] }], [null], [{ msg: 7 }], { msg: "not a list" }]) {
      const error = new ApiError("Invalid request", "validation_failed", 422, detail);
      expect(editsErrorMessage(error)).toBe("Invalid request");
    }
    expect(editsErrorMessage(new TypeError("Failed to fetch"))).toBe("Failed to fetch");
  });
});
