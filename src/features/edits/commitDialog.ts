import type { EditCounts, EditOutcome, EditPreview, PlannedStatement } from "@/lib/api/types";
import { plural } from "@/lib/format";
import { positionToOffset } from "../query/runPlan";
import { EXPORT_RUNNING, type ExecutionState } from "../session/execution";
import type { SessionPhase } from "../session/SessionController";
import {
  commitReport,
  commitToast,
  commitWarning,
  keyLabel,
  listedMessages,
  mayStopWaiting,
  NEW_ROW_LABEL,
  NEW_ROWS_NOTE,
  NOT_SENT_REPORT,
  SOME_CHANGE_LABEL,
  UNKNOWN_BEFORE,
  type CommitReport,
  type SettledCommit,
} from "./commitFlow";
import { hasChanges, type StagedEdits, type Staging } from "./staging";
import { REREAD_FIRST, ROWS_LOADING } from "./tableEditing";

/*
 * The commit dialog's decisions (spec ⑤ §3.6.5, R-05-6, R-05-7, 05a M7; pre-flight H2, H8, M1, M2, M5, L10, L15), as
 * pure functions: its steps and how one leads to the next, when Commit may start, what an outcome shows, what the grid
 * draws until the re-read is in (the afterglow), and the lists the dialog shows, built once per preview in time linear
 * in its size. `useCommit` does the I/O and asks these.
 */

type Messages = ExecutionState["messages"];

/**
 * The commit dialog's steps. The plan id lives only in `preview` (05a M7): a preview whose dialog has closed, or that a
 * newer one replaced, is never applied. `staging` is what the preview was made from (H8: Commit previews again when
 * the staging changed since); `unknownBefore` says the last commit's outcome was unknown (UNKNOWN_BEFORE). `key` is the
 * statements editor's: a new preview gets a new editor.
 */
export type CommitDialogState =
  | { readonly step: "previewing"; readonly ticket: number }
  | { readonly step: "failed"; readonly error: unknown; readonly message: string }
  | PreviewStep
  | (Omit<PreviewStep, "step"> & {
      readonly step: "applying";
      readonly requestId: string;
      /** The browse had not ended when the apply was sent: the apply ends it (L15). */
      readonly browseWasPaused: boolean;
    })
  | (Omit<PreviewStep, "step"> & {
      readonly step: "result";
      readonly report: CommitReport;
      readonly messages: Messages;
    });

export interface PreviewStep {
  readonly step: "preview";
  readonly preview: EditPreview;
  readonly staging: Staging;
  readonly key: number;
  readonly unknownBefore: boolean;
}

export type ShownPreview = Exclude<CommitDialogState, { step: "previewing" } | { step: "failed" }>;
export type ApplyingStep = Extract<CommitDialogState, { step: "applying" }>;

/** The steps that show a preview's statements. */
export const hasPreview = (state: CommitDialogState): state is ShownPreview =>
  state.step === "preview" || state.step === "applying" || state.step === "result";

/** The dialog closes on Escape, Cancel or Close, never while a commit runs (its outcome would go unseen). */
export const closable = (state: CommitDialogState | null): boolean => state?.step !== "applying";

/** A preview has been asked for: `ticket` tells its answer from an older one's. */
export const previewing = (ticket: number): CommitDialogState => ({ step: "previewing", ticket });

/**
 * A preview's answer: shown only when the dialog still waits for this very preview. A dialog that closed meanwhile, or
 * asked for a newer preview, gets `state` back unchanged (its plan is never applied, 05a M7).
 */
export function previewed(
  state: CommitDialogState | null,
  ticket: number,
  answer: Omit<PreviewStep, "step">,
): CommitDialogState | null {
  if (state?.step !== "previewing" || state.ticket !== ticket) return state;
  return { step: "preview", ...answer };
}

/** A preview that failed, under the same rule as `previewed`. */
export function previewFailed(
  state: CommitDialogState | null,
  ticket: number,
  error: unknown,
  message: string,
): CommitDialogState | null {
  if (state?.step !== "previewing" || state.ticket !== ticket) return state;
  return { step: "failed", error, message };
}

/** What a click on Commit does: apply the plan on screen, preview again (the staging changed since: H8), or nothing. */
export type CommitStep = { readonly kind: "apply"; readonly planId: string } | { readonly kind: "repreview" } | { readonly kind: "none" };

/** Whether the staging is the one a preview was made from: the same parts (every change keeps its object until edited). */
export const sameStaging = (a: Staging, b: Staging): boolean =>
  a.updates === b.updates && a.inserts === b.inserts && a.deletes === b.deletes;

export function commitStep(state: CommitDialogState | null, now: Staging | null): CommitStep {
  if (state?.step !== "preview" || !state.preview.plan_id) return { kind: "none" };
  if (!now || !sameStaging(now, state.staging)) return { kind: "repreview" };
  return { kind: "apply", planId: state.preview.plan_id };
}

/** The preview's plan is being applied as request `requestId`. */
export function applyingStep(state: PreviewStep, requestId: string, browseWasPaused: boolean): ApplyingStep {
  return { ...state, step: "applying", requestId, browseWasPaused };
}

/** The apply is over and its outcome is shown. */
export function resultStep(state: ShownPreview, report: CommitReport, messages: Messages): CommitDialogState {
  const { preview, staging, key, unknownBefore } = state;
  return { step: "result", preview, staging, key, unknownBefore, report, messages };
}

/** L15: the browse had not ended (paused, or loading more): an apply ends it, and a rollback then reads it again. */
export const browseWasPaused = (execution: ExecutionState | null): boolean =>
  execution !== null && execution.status !== "done";

export const COMMIT_RUNNING = "A commit is running: wait for it to end";
export const NOT_CONNECTED_TEXT = "Not connected";
export const UNKNOWN_REFRESH_FIRST = "The last commit's outcome is unknown: Refresh first";
export const NO_CHANGES = "No changes to commit";

export interface CommitFacts {
  /** A commit is running now. */
  applying: boolean;
  sessionOpen: boolean;
  exporting: boolean;
  /** The last commit's outcome is unknown and fresh rows have not been read since (R-05-6). */
  unknownPending: boolean;
  /** The rows are being read (held while a re-run loads, or not there yet). */
  rowsLoading: boolean;
  /** The grid draws the staging from before the last commit, whose re-read has not been delivered (M5). */
  afterglow: boolean;
  /** Why the table's rows cannot be edited (its target), or null. */
  readOnlyReason: string | null;
}

/** Why Commit… cannot start now, or null; the order is what is said first. */
export function commitBlocked(facts: CommitFacts): string | null {
  if (facts.applying) return COMMIT_RUNNING;
  if (!facts.sessionOpen) return NOT_CONNECTED_TEXT;
  if (facts.exporting) return EXPORT_RUNNING;
  if (facts.unknownPending) return UNKNOWN_REFRESH_FIRST;
  if (facts.rowsLoading) return ROWS_LOADING;
  if (facts.afterglow) return REREAD_FIRST; // its re-read was cut short (K17): the rows shown are from before
  return facts.readOnlyReason;
}

/**
 * Pre-flight M5: the staging as it was before an outcome took changes away (a commit, or a partial one), drawn by the
 * grid instead of the store's until the re-read's rows are in, so committed values never flip back to the loaded ones
 * on the rows held meanwhile, deleted rows stay struck through and new rows stay on top (the selection and the anchor
 * keep their rows). `since` is the browse request delivered at the outcome and `position` the browse's position then.
 */
export interface Afterglow {
  readonly staged: StagedEdits;
  readonly since: string | null;
  readonly position: number;
}

/** The browse as the afterglow follows it. */
export interface BrowseNow {
  /** The request whose first page is in whole, or null while one loads (or ended before it was, K17). */
  readonly deliveredRequest: string | null;
  /** Changes when a new sort or filter starts the grid at the top. */
  readonly position: number;
}

/** The afterglow of an outcome, or null when it took nothing away (rolled back, unknown, nothing staged). */
export function afterglowOf(outcome: EditOutcome, before: StagedEdits | null, browse: BrowseNow): Afterglow | null {
  if (outcome !== "committed" && outcome !== "partial") return null;
  if (!before || !hasChanges(before)) return null;
  return { staged: before, since: browse.deliveredRequest, position: browse.position };
}

/**
 * Whether the afterglow is still drawn. It goes once another request has delivered its rows (the re-read, or the
 * user's Refresh), and with a new sort or filter (its rows are not the ones it was drawn over). A re-read cut short
 * (cancelled, failed, interrupted: K17) delivers nothing and the held rows stay, so it stays too, until a request
 * delivers. It is never drawn again once it went: the caller drops it then.
 */
export function afterglowHolds(afterglow: Afterglow, browse: BrowseNow): boolean {
  if (browse.position !== afterglow.position) return false;
  return browse.deliveredRequest === null || browse.deliveredRequest === afterglow.since;
}

/** The afterglow kept for `browse` now: itself while it holds, else null (the caller then drops it for good). */
export const afterglowNow = (afterglow: Afterglow | null, browse: BrowseNow): Afterglow | null =>
  afterglow && afterglowHolds(afterglow, browse) ? afterglow : null;

/**
 * How the result's Refresh reads the table again: the browse, or (its session gone, e.g. the backend restarted) a new
 * session, which sends the browse again; either delivers the fresh rows an unknown outcome waits for (R-05-6).
 */
export const refreshBy = (phase: SessionPhase | null): "refresh" | "reconnect" =>
  phase === null || phase === "failed" || phase === "closed" ? "reconnect" : "refresh";

/** This tab's `sql_mode_changed` refusals in a row after an apply (a command that never left changes nothing). */
export function nextSqlModeRefusals(previous: number, commit: SettledCommit): number {
  if (commit.kind === "not_sent") return previous;
  return commit.error?.code === "sql_mode_changed" ? previous + 1 : 0;
}

/** What an outcome shows: a commit with nothing more to say closes the dialog with toasts; anything else stays. */
export type OutcomeView =
  | { readonly kind: "toasts"; readonly toasts: readonly { kind: "success" | "warning"; text: string }[] }
  | { readonly kind: "result"; readonly report: CommitReport; readonly messages: Messages };

export function outcomeView(
  commit: SettledCommit,
  context: { table: string; counts: EditCounts; sqlModeRefusals: number },
): OutcomeView {
  if (commit.kind === "not_sent") return { kind: "result", report: NOT_SENT_REPORT, messages: [] };
  const { counts } = context;
  if (commit.outcome === "committed" && commit.event && listedMessages(commit).length === 0) {
    const toasts: { kind: "success" | "warning"; text: string }[] = [
      { kind: "success", text: commitToast(commit.event, context.table, counts.inserts) },
    ];
    // A COMMIT that reported an error after every statement took effect (non-transactional): its own toast, never a
    // trigger's warning (L10).
    const warning = commitWarning(commit);
    if (warning) toasts.push({ kind: "warning", text: warning });
    return { kind: "toasts", toasts };
  }
  const total = counts.updates + counts.inserts + counts.deletes;
  const report = commitReport(commit, { total, sqlModeRefusals: context.sqlModeRefusals });
  // L10: a commit with a trigger's NOTICE / PRINT stays open to list them, and says where its new rows went.
  const committed = commit.outcome === "committed" && counts.inserts > 0;
  return {
    kind: "result",
    report: committed ? { ...report, notes: [...report.notes, NEW_ROWS_NOTE] } : report,
    messages: commit.messages,
  };
}

/** The counts as the dialog's header shows them, the deletes first (they are drawn in the danger colour). */
export function countParts(counts: EditCounts): { text: string; danger: boolean }[] {
  const parts: { text: string; danger: boolean }[] = [];
  if (counts.deletes) parts.push({ text: plural(counts.deletes, "delete"), danger: true });
  if (counts.updates) parts.push({ text: plural(counts.updates, "update"), danger: false });
  if (counts.inserts) parts.push({ text: plural(counts.inserts, "insert"), danger: false });
  return parts;
}

/** The statements editor's text: `begin`, each statement and `commit` as sent, a line each; where each statement starts. */
export interface StatementLayout {
  readonly text: string;
  readonly starts: readonly number[];
}

export function statementLayout(preview: EditPreview): StatementLayout {
  const parts = [preview.begin];
  const starts: number[] = [];
  let at = preview.begin.length + 1;
  for (const statement of preview.statements) {
    starts.push(at);
    parts.push(statement.sql);
    at += statement.sql.length + 1;
  }
  parts.push(preview.commit);
  return { text: parts.join("\n"), starts };
}

/** The failed statement in the editor: its range, its lines' starts (marked red), and where the cursor goes (L12). */
export interface FailedPlace {
  readonly from: number;
  readonly to: number;
  readonly lineStarts: readonly number[];
  readonly cursor: number;
}

export function failedPlace(preview: EditPreview, layout: StatementLayout, report: CommitReport): FailedPlace | null {
  const index = report.failedStatement;
  const statement = index === null ? undefined : preview.statements[index];
  const from = index === null ? undefined : layout.starts[index];
  if (!statement || from === undefined) return null;
  const to = from + statement.sql.length;
  const lineStarts = [from];
  for (let at = statement.sql.indexOf("\n"); at >= 0; at = statement.sql.indexOf("\n", at + 1)) {
    lineStarts.push(from + at + 1);
  }
  const position = report.failedPosition;
  const cursor = position ? positionToOffset(statement.sql, from, position.line, position.column) : from;
  return { from, to, lineStarts, cursor };
}

/** The most items a list of the dialog shows; the rest are counted ("… and N more"). */
export const LIST_MAX = 200;

export interface ProblemItem {
  readonly changeId: string;
  readonly column: string | null;
  /** Its row ("Row id = 7", "New row"). */
  readonly label: string;
  readonly message: string;
}

export interface WarningItem {
  /** Its row for a loaded row's statement ("Row id = 7"); null for the table's, and for a new row's (it says so). */
  readonly label: string | null;
  readonly message: string;
  /** The unchecked columns' reasons ("price: …"), or null. */
  readonly reasons: string | null;
}

export interface Listed<T> {
  readonly shown: readonly T[];
  /** Items not shown. */
  readonly more: number;
}

export interface PreviewLists {
  readonly problems: Listed<ProblemItem>;
  /** UNKNOWN_BEFORE first after an unknown outcome, then the preview's warnings. */
  readonly warnings: Listed<WarningItem>;
  /** The table can roll back (no `non_transactional` warning). */
  readonly transactional: boolean;
}

/**
 * The labels of the changes `ids` names, from the staging a preview was made from, in one pass over it (a preview with
 * problems has no statements to name its rows).
 */
function labelsFor(staging: Staging, ids: ReadonlySet<string>): Map<string, string> {
  const labels = new Map<string, string>();
  if (ids.size === 0) return labels;
  for (const change of Object.values(staging.updates)) if (ids.has(change.id)) labels.set(change.id, keyLabel(change.key));
  for (const change of Object.values(staging.deletes)) if (ids.has(change.id)) labels.set(change.id, keyLabel(change.key));
  for (const change of staging.inserts) if (ids.has(change.id)) labels.set(change.id, NEW_ROW_LABEL);
  return labels;
}

const statementLabel = (statement: PlannedStatement): string | null =>
  statement.row ? `Row ${statement.row}` : statement.kind === "insert" ? NEW_ROW_LABEL : null;

/** How the result names the failed change's row (one search: once per outcome). */
export function rowLabel(preview: EditPreview, staging: Staging, changeId: string): string {
  const statement = preview.statements.find((s) => s.change_id === changeId);
  const label = statement ? statementLabel(statement) : null;
  return label ?? labelsFor(staging, new Set([changeId])).get(changeId) ?? SOME_CHANGE_LABEL;
}

/**
 * The dialog's problems and warnings (pre-flight H2): a map of the statements by change, built once, and at most
 * LIST_MAX items of each list, so a preview of 10,000 changes costs one pass over it, never a search per item. Built
 * once per preview (the caller memoizes on it), never again while the apply's progress comes in.
 */
export function previewLists(preview: EditPreview, staging: Staging, unknownBefore: boolean): PreviewLists {
  const byChange = new Map<string, PlannedStatement>();
  for (const statement of preview.statements) byChange.set(statement.change_id, statement);
  const problems = preview.problems.slice(0, LIST_MAX);
  const room = LIST_MAX - (unknownBefore ? 1 : 0);
  const warnings = preview.warnings.slice(0, room);
  // A row named by a statement is labelled by it; the others (a preview with problems has none) by the staging.
  const missing = new Set<string>();
  for (const problem of problems) if (!byChange.has(problem.change_id)) missing.add(problem.change_id);
  for (const warning of warnings) if (warning.change_id && !byChange.has(warning.change_id)) missing.add(warning.change_id);
  const fromStaging = labelsFor(staging, missing);
  const labelOf = (id: string): string => {
    const statement = byChange.get(id);
    return (statement && statementLabel(statement)) ?? fromStaging.get(id) ?? SOME_CHANGE_LABEL;
  };
  const warningItems: WarningItem[] = unknownBefore ? [{ label: null, message: UNKNOWN_BEFORE, reasons: null }] : [];
  for (const warning of warnings) {
    const statement = warning.change_id ? byChange.get(warning.change_id) : undefined;
    const reasons = statement?.unchecked?.map((u) => `${u.column}: ${u.reason}`).join("; ") || null;
    // A new row's warning names it itself ("New row: no value for …").
    const label = warning.change_id && warning.code !== "missing_required" ? labelOf(warning.change_id) : null;
    warningItems.push({ label, message: warning.message, reasons });
  }
  return {
    problems: {
      shown: problems.map((problem) => ({
        changeId: problem.change_id,
        column: problem.column ?? null,
        label: labelOf(problem.change_id),
        message: problem.message,
      })),
      more: preview.problems.length - problems.length,
    },
    warnings: { shown: warningItems, more: preview.warnings.length - warnings.length },
    transactional: !preview.warnings.some((warning) => warning.code === "non_transactional"),
  };
}

/** "… and 9,800 more" under a list cut at LIST_MAX, or null. */
export const moreText = (more: number): string | null => (more > 0 ? `… and ${more.toLocaleString()} more` : null);

export const TRANSACTIONAL_NOTE =
  "Runs in one transaction: if any statement fails or does not match exactly one row, nothing is applied.";
export const NON_TRANSACTIONAL_NOTE =
  "This table cannot roll back: if a statement fails, the statements before it stay applied.";
export const PROBLEMS_INTRO = "Nothing can be committed until these are fixed in the grid:";
export const NOT_SHOWN = "That row is not among the rows shown (filtered out or not loaded)";
export const BUILDING = "Building the statements…";

/**
 * The apply's last warning, shown under the progress line while it runs (SQL Server's "Press Cancel again to drop the
 * connection…" after a Cancel that cannot reach the statement), or null.
 */
export function lastWarning(messages: Messages): string | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) if (messages[i].severity === "warning") return messages[i].text;
  return null;
}

/** The applying dialog's progress line and controls (pre-flight M1, M2, ruling K11). */
export interface ApplyingView {
  readonly progress: string;
  /** Cancel still reaches a statement: some statements' counts have not come in (then COMMIT is on its way). */
  readonly canCancel: boolean;
  /** CLOSE_UNKNOWN_LABEL is offered: the outcome may never come (`mayStopWaiting`). */
  readonly closeUnknown: boolean;
}

export function applyingView(wait: {
  phase: SessionPhase;
  counted: number;
  statements: number;
  quietMs: number;
  sinceCancelMs: number | null;
}): ApplyingView {
  const { counted, statements } = wait;
  const progress =
    counted < statements
      ? `Committing… statement ${(counted + 1).toLocaleString()} of ${statements.toLocaleString()}`
      : "Committing…";
  return { progress, canCancel: counted < statements, closeUnknown: mayStopWaiting(wait) };
}
