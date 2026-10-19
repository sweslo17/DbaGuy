import type { EditOutcome, EditOutcomeEvent, EditPreview } from "@/lib/api/types";
import { plainValue } from "@/lib/cells";
import { plural } from "@/lib/format";
import { CONNECTION_LOST, INTERRUPTED_MESSAGE, NOT_CONNECTED_CODE, type ExecutionState } from "../session/execution";
import type { SessionPhase } from "../session/SessionController";
import { findChange, type AppliedOutcome, type StagedProblem, type Staging } from "./staging";

/*
 * How a commit stands and what the dialog and the toast say about it (spec ⑤ R-05-6, §3.6.5, the 05a appendix), as pure
 * functions of the apply request's execution. Nothing is ever sent again: an apply whose outcome did not arrive is
 * unknown, and only the user's Refresh (fresh rows) lets a commit start again. Only a command that never left the
 * socket (`not_connected`) is "not sent": then nothing happened.
 */

type Messages = ExecutionState["messages"];

/** An apply that is over, with what came back. */
export interface EndedCommit {
  readonly kind: "ended";
  readonly outcome: EditOutcome;
  /** The backend's `edit_outcome`; null when the request ended without one (then `outcome` is "unknown"). */
  readonly event: EditOutcomeEvent | null;
  readonly error: ExecutionState["error"];
  /**
   * A trigger's NOTICE / PRINT, warnings (05a: "COMMIT reported an error after every statement…"): the backend's own,
   * never DbaGuy's "Connection interrupted" line (the outcome, or LOST_TEXT, says what that means for an apply).
   */
  readonly messages: Messages;
  readonly cancelled: boolean;
  /**
   * The session still had a transaction open when the apply finished (`finished.in_transaction`): the apply's rollback
   * failed, or the transaction was open before it (`transaction_open`).
   */
  readonly inTransaction: boolean;
}

/** The command never left: the session was not connected (`not_connected`), so nothing happened. */
export interface NotSentCommit {
  readonly kind: "not_sent";
}

/** An apply that is over, one way or the other. */
export type SettledCommit = NotSentCommit | EndedCommit;

export type CommitState =
  /** Statements whose rows have been counted so far (`affected`). */
  | { readonly kind: "running"; readonly done: number }
  | SettledCommit;

const NOT_SENT: NotSentCommit = { kind: "not_sent" };

const ended = (execution: ExecutionState, outcome: EditOutcome, event: EditOutcomeEvent | null): EndedCommit => ({
  kind: "ended",
  outcome,
  event,
  error: execution.error,
  messages: execution.interrupted
    ? execution.messages.filter((message) => message.text !== INTERRUPTED_MESSAGE)
    : execution.messages,
  cancelled: execution.cancelled,
  inTransaction: execution.inTransaction,
});

/**
 * An apply's execution read as over: its `edit_outcome` decides, also when the socket went after it arrived; without
 * one it is unknown (R-05-6), unless the command never left (only `SessionController` sets `not_connected`).
 */
function settled(execution: ExecutionState): SettledCommit {
  const event = execution.editOutcome;
  if (event) return ended(execution, event.outcome, event);
  if (execution.error?.code === NOT_CONNECTED_CODE) return NOT_SENT;
  return ended(execution, "unknown", null);
}

/** Where an apply request stands (R-05-6: an apply that ended without `edit_outcome` is unknown, never resent). */
export function commitState(execution: ExecutionState): CommitState {
  if (execution.status !== "done") return { kind: "running", done: execution.affected.length };
  return settled(execution);
}

/**
 * The apply as it stands when the dialog stops waiting for it, or when its execution is gone from the session (`null`:
 * a closed or replaced session). An outcome that arrived stands; a command that never left was not sent; anything else
 * is unknown, and nothing is sent again (pre-flight M2).
 */
export function abandonedCommit(execution: ExecutionState | null): SettledCommit {
  if (execution) return settled(execution);
  return {
    kind: "ended",
    outcome: "unknown",
    event: null,
    error: null,
    messages: [],
    cancelled: false,
    inTransaction: false,
  };
}

/** How long the apply may go without an event before the dialog offers to close it unknown (pre-flight M2). */
export const OUTCOME_WAIT_MS = 30_000;
/** The applying dialog's way out when the outcome does not come (pre-flight M2, ruling K11). */
export const CLOSE_UNKNOWN_LABEL = "Close — outcome unknown";

/**
 * Whether the applying dialog offers CLOSE_UNKNOWN_LABEL (pre-flight M2, ruling K11: otherwise the modal can hold the
 * whole app), when any of these holds:
 * - the session is not open: the outcome can no longer arrive on this socket;
 * - every statement was counted (COMMIT is on its way, and Cancel no longer reaches it) and no event came for
 *   OUTCOME_WAIT_MS after the last `affected`;
 * - a Cancel was sent and no event came for OUTCOME_WAIT_MS since (the statement does not stop).
 * Before that, while a statement runs, Cancel is the way out: it ends in a known outcome.
 */
export function mayStopWaiting(wait: {
  phase: SessionPhase;
  /** Statements counted so far (`affected`). */
  counted: number;
  /** The plan's statements. */
  statements: number;
  /** Milliseconds since the apply's last event (or since it was sent). */
  quietMs: number;
  /** Milliseconds since the Cancel was sent; null when none was. */
  sinceCancelMs: number | null;
}): boolean {
  if (wait.phase !== "open") return true;
  if (wait.counted >= wait.statements && wait.quietMs >= OUTCOME_WAIT_MS) return true;
  return wait.sinceCancelMs !== null && Math.min(wait.sinceCancelMs, wait.quietMs) >= OUTCOME_WAIT_MS;
}

/** What the staging takes from a command that never left (pre-flight H1): nothing happened; "applying" is cleared. */
export const NOT_SENT_OUTCOME: AppliedOutcome = {
  outcome: "rolled_back",
  applied: [],
  failedChange: null,
  message: null,
};

const isCancel = (commit: EndedCommit) => commit.cancelled || commit.error?.code === "cancelled";

/**
 * What the staging takes from a settled apply. A cancel's text never becomes the change's problem (pre-flight L24):
 * the row would turn red for something the user did.
 */
export function stagedOutcome(commit: SettledCommit): AppliedOutcome {
  if (commit.kind === "not_sent") return NOT_SENT_OUTCOME;
  return {
    outcome: commit.outcome,
    applied: commit.event?.applied ?? [],
    failedChange: commit.event?.failed_change ?? null,
    message: isCancel(commit) ? null : (commit.error?.message ?? null),
  };
}

/**
 * Whether the browse is run again after it (§3.6.5): always after a commit or a partial one; after a rollback only when
 * the apply ended a paused result (its rows stopped loading); never after an unknown one (the user's Refresh does it).
 */
export function rerunAfter(outcome: EditOutcome, browseWasPaused: boolean): boolean {
  if (outcome === "committed" || outcome === "partial") return true;
  return outcome === "rolled_back" && browseWasPaused;
}

/** What the result dialog offers besides Close. */
export type CommitAction = "show_in_grid" | "preview_again" | "rollback" | "reconnect" | "refresh";

export interface CommitReport {
  readonly tone: "success" | "warning" | "danger";
  readonly title: string;
  /** The server's text (or DbaGuy's fixed one) about what failed. */
  readonly detail: string | null;
  /** Why it happened and what to do. */
  readonly notes: readonly string[];
  readonly actions: readonly CommitAction[];
  readonly failedChange: string | null;
  /** The failed statement's index in the preview's `statements`. */
  readonly failedStatement: number | null;
  /** Where in the failed statement the server put its error (pre-flight L12): relative to that statement's text. */
  readonly failedPosition: { readonly line: number; readonly column: number } | null;
}

export const UNKNOWN_NOTE =
  "The changes may or may not have been applied. Refresh to see the table as it is now: Commit is off until then, and nothing is sent again by itself.";
export const LOST_TEXT = "The connection was lost while committing: the changes may or may not have been applied.";
export const TRANSACTION_OPEN_NOTE =
  "This tab's session has a transaction open (for example from SQL Server's IMPLICIT_TRANSACTIONS login option): roll it back or reconnect, then preview again.";
export const SQL_MODE_NOTE =
  "This tab's connection and the connection DbaGuy reads the table with treat backslashes differently: the server's sql_mode changed while they were open. Reconnect this tab and preview again; setting sql_mode in the connection's options keeps it from happening.";
export const SQLITE_BUSY_NOTE =
  "Another tab still has an open result of this database file, or another program is using it: finish or close that result, then commit again.";
export const PARTIAL_NOTE = "This table cannot roll back: what took effect is no longer staged, the rest still is.";
const CANCELLED_TITLE = "Cancelled: nothing was applied";
const CLOSED_TITLE = "The session closed before the commit started: nothing was applied";

/** The command never left (the session was not connected): nothing happened, so nothing changes in the staging. */
export const NOT_SENT_REPORT: CommitReport = {
  tone: "danger",
  title: "Not sent",
  detail: "This tab is not connected: nothing was sent. Reconnect, then commit again.",
  notes: [],
  actions: ["reconnect"],
  failedChange: null,
  failedStatement: null,
  failedPosition: null,
};

/** The preview's line after an unknown outcome (R-05-6): what committing again does. */
export const UNKNOWN_BEFORE =
  "The last commit's outcome was unknown: an UPDATE or DELETE checks the loaded values again, but a new row would be inserted a second time.";

/** SQLite's busy and locked errors (`sqlite_errorname`): another connection holds the file. */
export const isSqliteBusy = (code: string | null | undefined): boolean =>
  !!code && (code.startsWith("SQLITE_BUSY") || code.startsWith("SQLITE_LOCKED"));

/** The warning of a commit that stood though its COMMIT failed (non-transactional): `COMMIT_WARNING` in edit_apply.py. */
const COMMIT_WARNING = "COMMIT reported an error after every statement had taken effect";
/**
 * The warning of a rollback that failed with the transaction still open: `ROLLBACK_FAILED` in edit_apply.py. Only a
 * fallback: `finished.in_transaction` says it (`EndedCommit.inTransaction`).
 */
const ROLLBACK_FAILED = "The transaction could not be rolled back";
/** The message of a queued request whose session closed first: `CLOSED_BEFORE_START` in worker.py. */
const CLOSED_BEFORE_START = "Session closed before the query started";

const isCommitWarning = (message: Messages[number]) =>
  message.severity === "warning" && message.text.startsWith(COMMIT_WARNING);

/** A committed apply's warning that its COMMIT reported an error (every statement had taken effect): a second toast. */
export function commitWarning(commit: EndedCommit): string | null {
  return commit.messages.find(isCommitWarning)?.text ?? null;
}

/**
 * The messages no toast carries (a trigger's NOTICE / PRINT, other warnings): a commit with any keeps the result view
 * open to list them (pre-flight L10; spec §3.6.5 "trigger 的訊息列在下方").
 */
export function listedMessages(commit: EndedCommit): Messages {
  return commit.messages.filter((message) => !isCommitWarning(message));
}

/**
 * The session's own database connection needs a new session (pre-flight L11): it was lost, or a transaction is still
 * open on it once the apply finished (its rollback failed: "close and reopen this tab"). The warning's text is only a
 * fallback for `finished.in_transaction`.
 */
const needsReconnect = (commit: EndedCommit) =>
  commit.error?.code === CONNECTION_LOST ||
  commit.inTransaction ||
  commit.messages.some((message) => message.text.startsWith(ROLLBACK_FAILED));

/** The title of a rollback: a closed session and a cancel each say so (neither is the server's refusal). */
function rolledBackTitle(commit: EndedCommit): string {
  if (commit.messages.some((message) => message.text === CLOSED_BEFORE_START)) return CLOSED_TITLE;
  return isCancel(commit) ? CANCELLED_TITLE : "Nothing was applied";
}

/** "1 row was already gone", "3 rows were already gone": the report's note and the toast's part. */
const alreadyGone = (gone: number) => `${plural(gone, "row")} ${gone === 1 ? "was" : "were"} already gone`;

/**
 * The dialog's account of an ended apply. `sqlModeRefusals`: this tab's `sql_mode_changed` refusals in a row, this
 * apply's own included (so the first refusal is 1).
 */
export function commitReport(commit: EndedCommit, context: { total: number; sqlModeRefusals: number }): CommitReport {
  const failedChange = commit.event?.failed_change ?? null;
  const failedStatement = commit.event?.statement ?? null;
  const code = commit.error?.code ?? null;
  const applied = commit.event?.applied.length ?? 0;
  const base = {
    failedChange,
    failedStatement,
    failedPosition: failedStatement === null ? null : (commit.error?.position ?? null),
    detail: commit.error?.message ?? null,
  };
  const actions: CommitAction[] = failedChange ? ["show_in_grid"] : [];
  const offer = (action: CommitAction) => {
    if (!actions.includes(action)) actions.push(action);
  };
  switch (commit.outcome) {
    case "committed": {
      const gone = commit.event?.already_gone.length ?? 0;
      const notes = gone ? [alreadyGone(gone)] : [];
      return { ...base, tone: "success", title: `Committed ${plural(applied, "change")}`, notes, actions: [] };
    }
    case "partial":
      return {
        ...base,
        tone: "warning",
        title: `Partly applied: ${applied.toLocaleString()} of ${plural(context.total, "change")} took effect`,
        notes: [PARTIAL_NOTE],
        actions: needsReconnect(commit) ? [...actions, "reconnect"] : actions,
      };
    case "unknown":
      return {
        ...base,
        tone: "warning",
        title: "Outcome unknown",
        detail: commit.event ? base.detail : LOST_TEXT,
        notes: [UNKNOWN_NOTE],
        actions: needsReconnect(commit) ? ["refresh", "reconnect"] : ["refresh"],
      };
    case "rolled_back": {
      const notes: string[] = [];
      if (code === "plan_expired" || code === "plan_mismatch") offer("preview_again");
      if (code === "transaction_open") {
        notes.push(TRANSACTION_OPEN_NOTE);
        offer("rollback");
        offer("reconnect");
      }
      if (code === "sql_mode_changed") {
        if (context.sqlModeRefusals >= 2) {
          notes.push(SQL_MODE_NOTE);
          offer("reconnect");
        } else offer("preview_again");
      }
      if (isSqliteBusy(code)) notes.push(SQLITE_BUSY_NOTE);
      if (needsReconnect(commit)) offer("reconnect");
      const title = rolledBackTitle(commit);
      const detail = base.detail === title ? null : base.detail; // a cancel's text is the title itself
      return { ...base, tone: "danger", title, detail, notes, actions };
    }
  }
}

/** The toast of a commit (§3.6.5): how many, rows already gone, and where new rows went. */
export function commitToast(event: EditOutcomeEvent, table: string, inserts: number): string {
  const gone = event.already_gone.length;
  const parts = [`Committed ${plural(event.applied.length, "change")} to ${table}`];
  if (gone) parts.push(alreadyGone(gone));
  const text = parts.join(" · ");
  return inserts
    ? `${text}. New rows appear where the table's order puts them, which may be past the rows loaded`
    : text;
}

export const ROLLED_BACK_TEXT = "Rolled back this tab's open transaction: preview the changes again";
export const ROLLBACK_UNCONFIRMED = "The rollback may not have run: reconnect this tab, then preview the changes again";

/**
 * The toast once the result dialog's Roll back (`transaction_open`) has its answer (pre-flight L22): never "rolled
 * back" before it is.
 */
export function rollbackToast(execution: ExecutionState): { kind: "success" | "warning" | "error"; text: string } {
  if (execution.error) return { kind: "error", text: `Could not roll back: ${execution.error.message}` };
  if (execution.interrupted || execution.cancelled) return { kind: "warning", text: ROLLBACK_UNCONFIRMED };
  return { kind: "success", text: ROLLED_BACK_TEXT };
}

/**
 * A preview's problems by change, as the staging keeps them (they mark the cells). Grouped in a Map, so the record
 * holds own entries whatever an id is (a restored id is any short text).
 */
export function problemsOf(preview: EditPreview): Record<string, StagedProblem[]> {
  const problems = new Map<string, StagedProblem[]>();
  for (const problem of preview.problems) {
    const list = problems.get(problem.change_id) ?? [];
    list.push({ column: problem.column ?? null, message: problem.message });
    problems.set(problem.change_id, list);
  }
  return Object.fromEntries(problems);
}

/** A change as the dialog names it: "New row", or its key ("id = 7"); the key is the user's data, never logged. */
export function changeLabel(staged: Staging, changeId: string): string {
  const found = findChange(staged, changeId);
  if (!found) return "A change";
  if (found.kind === "insert") return "New row";
  const key = Object.entries(found.change.key).map(([column, cell]) => `${column} = ${plainValue(cell)}`);
  return `Row ${key.join(", ")}`;
}
