import type { JobOutcomeEvent, JobProgressEvent } from "@/lib/api/types";
import { formatElapsed, formatSize, plural } from "@/lib/format";
import { isSqliteBusy, sqliteBusyNote } from "@/lib/sqliteBusy";
import { FILTER_RULE } from "../table-data/filterValue";
import type { JobCommand, JobKind, JobState } from "./jobState";

/*
 * What a job's dialog and the Jobs list say about it (spec ⑥ §3.6, §4 "結果的說明由前端依 job_outcome 組成"): its
 * progress while it runs and, once it ended, its outcome in words, the error with a label and what to do, the skipped
 * errors and the actions that make sense now. Pure. Every action that would run something again goes back to the form,
 * prefilled, so a run is always started (and, on a red connection, confirmed) from the form (ruling B6).
 */

export interface JobView {
  kind: JobKind;
  /** What was sent; null for a job listed after a reload (it was started by another page load). */
  command: JobCommand | null;
  state: JobState;
}

export type Tone = "success" | "warning" | "danger" | "muted";

/** The form's fields a "run again" action changes. */
export interface RunPatch {
  mode: "all_or_nothing" | "statements";
  on_error: "stop" | "continue";
  from_statement: number;
}

export type JobAction =
  | { kind: "show_in_finder"; label: string; path: string }
  /** Back to the form with the job's own settings, changed by `patch` (null: as they were). */
  | { kind: "again"; label: string; patch: RunPatch | null };

export interface ErrorLine {
  line: number | null;
  statement: number | null;
  code: string | null;
  label: string | null;
  message: string;
}

export interface JobReport {
  tone: Tone;
  title: string;
  /** The facts: counts, where, the error's text. */
  detail: string | null;
  /** The error that ended it, with its label; null when none. */
  error: ErrorLine | null;
  /** What to do about it, when the backend's text does not say. */
  hint: string | null;
  /** The outcome's warnings (what it means, what was left out). */
  notes: string[];
  /** The errors a run skipped (the first ones), and how many more there were. */
  skipped: ErrorLine[];
  moreSkipped: number;
  actions: JobAction[];
}

/** A short name for each of DbaGuy's own error codes (spec ⑥ §4, the 06a carries); a server's own code has none. */
export const CODE_LABELS = {
  implicit_commit: "MySQL commits around this statement",
  procedure_call: "A procedure call may commit inside it",
  transaction_in_file: "The file has its own transactions",
  transaction_ended: "The transaction ended inside the file",
  transaction_failed: "The transaction was aborted",
  commit_unconfirmed: "COMMIT left a transaction open",
  file_truncated: "The dump is cut short",
  file_not_utf8: "Not UTF-8 text",
  statement_too_large: "A statement over 256 MiB",
  psql_meta_command: "A psql command",
  unsupported_copy: "COPY … FROM stdin",
  file_not_found: "Not a file",
  file_io: "The file cannot be read",
  dump_io: "The file cannot be written",
  invalid_path: "Not a file to dump into",
  invalid_filter: "Not one condition",
  invalid_text: "Text a UTF-8 file cannot hold",
  not_supported: "Not available on this connection",
  unsupported_option: "An option this connection does not take",
  read_only: "Read-only connection",
  transaction_open: "A transaction was already open",
  connection_lost: "Connection lost",
  cancelled: "Cancelled",
  invalid_command: "Refused by the backend",
  session_busy: "The job's session was busy",
} as const;
export type JobErrorCode = keyof typeof CODE_LABELS;

const SQLITE_BUSY_LABEL = "SQLite file in use";
/** What to do, where the backend's own text does not say it. */
const CODE_HINTS: Partial<Record<JobErrorCode, string>> = {
  connection_lost:
    "Check the target before running anything again: the statement that was running may or may not have taken effect.",
  transaction_open:
    "The connection had a transaction open before the job began (SQL Server: a login whose user options turn IMPLICIT_TRANSACTIONS on). Nothing was sent.",
  invalid_filter: FILTER_RULE,
};
/** Codes after which "Skip it and continue" cannot help: the file cannot be read past them, or nothing can run. */
const NOT_SKIPPABLE: ReadonlySet<string> = new Set([
  "file_not_utf8",
  "statement_too_large",
  "file_io",
  "psql_meta_command",
  "unsupported_copy",
  "read_only",
]);
/** All-or-nothing refusals whose remedy is the other mode (the backend's text says "run it statement by statement"). */
const NEEDS_STATEMENTS: ReadonlySet<string> = new Set([
  "transaction_in_file",
  "implicit_commit",
  "procedure_call",
  "file_truncated",
  "SQLITE_CONSTRAINT_FOREIGNKEY",
]);

export function codeLabel(code: string | null | undefined): string | null {
  if (!code) return null;
  if (isSqliteBusy(code)) return SQLITE_BUSY_LABEL;
  return code in CODE_LABELS ? CODE_LABELS[code as JobErrorCode] : null;
}

function codeHint(code: string | null, kind: JobKind): string | null {
  if (isSqliteBusy(code)) return sqliteBusyNote(kind === "dump" ? "dump again" : "run the file again");
  return code && code in CODE_HINTS ? (CODE_HINTS[code as JobErrorCode] ?? null) : null;
}

const fileName = (path: string | null | undefined) => (path ? (path.split("/").pop() ?? path) : "the file");
const runCommand = (command: JobCommand | null) => (command?.type === "run_file" ? command : null);
const statementsApplied = (count: number) => `${plural(count, "statement")} applied`;

function errorLine(outcome: JobOutcomeEvent | null, state: JobState): ErrorLine | null {
  const error = outcome?.error ?? (state.error ? { ...state.error, line: null, statement: null } : null);
  if (!error) return null;
  const code = error.code ?? null;
  return { line: error.line ?? null, statement: error.statement ?? null, code, label: codeLabel(code), message: error.message };
}

const NOTHING_WRITTEN_NOTE = "A file that was already at that path is as it was.";
const NOT_KNOWN_NOTE = "What was applied is not known: check the target.";

type Words = Omit<JobReport, "error" | "skipped" | "moreSkipped" | "hint">;

function dumpReport(outcome: JobOutcomeEvent, command: JobCommand | null, error: ErrorLine | null): Words {
  // Only a dump this page started can be started again from its form (a listed one has no command here, L4).
  const again: JobAction[] = command ? [{ kind: "again", label: "Dump again…", patch: null }] : [];
  const notes = outcome.warnings;
  switch (outcome.outcome) {
    case "completed": {
      const tables = outcome.objects.length;
      const path = outcome.path ?? "";
      // A schema-only dump lists no objects (the backend names them as it writes their rows): say size and place only (L3)
      const written = tables
        ? `${plural(outcome.rows, "row")} in ${plural(tables, "table")} · ${formatSize(outcome.bytes)}`
        : formatSize(outcome.bytes);
      return {
        tone: notes.length ? "warning" : "success",
        title: `Dumped to ${fileName(path)}`,
        detail: `${written} · ${path}`,
        notes,
        actions: [{ kind: "show_in_finder", label: "Show in Finder", path }],
      };
    }
    case "failed":
      return outcome.cancelled
        ? { tone: "muted", title: "Dump cancelled: nothing was written", detail: NOTHING_WRITTEN_NOTE, notes, actions: again }
        : { tone: "danger", title: "Dump failed: nothing was written", detail: NOTHING_WRITTEN_NOTE, notes, actions: again };
    case "not_started":
      return {
        tone: outcome.cancelled ? "muted" : "danger",
        title: outcome.cancelled ? "Cancelled before the dump started" : "The dump did not start",
        detail: error ? null : "Nothing was written.",
        notes,
        actions: again,
      };
    default: // unknown (the backend's list: its session closed first)
      return {
        tone: "danger",
        title: "The dump's outcome is unknown",
        detail: `Check whether ${outcome.path ?? "the file"} was written.`,
        notes,
        actions: again,
      };
  }
}

/** What a run offers to do again: from where it can start without skipping or repeating a statement that took effect. */
function runActions(outcome: JobOutcomeEvent, command: JobCommand | null, code: string | null): JobAction[] {
  const run = runCommand(command);
  // Only a run this page started can be started again from its form (a listed one has no command here).
  if (!run) return [];
  const at = outcome.stopped_at;
  const resume = outcome.resume_at ?? null;
  const onError = run.on_error;
  if (resume) {
    const actions: JobAction[] = [
      {
        kind: "again",
        label: `Run again from line ${resume.line.toLocaleString()}`,
        patch: { mode: "statements", on_error: onError, from_statement: resume.statement },
      },
    ];
    if (at && resume.statement === at.statement && !(code && NOT_SKIPPABLE.has(code))) {
      actions.push({
        kind: "again",
        label: "Skip it and continue",
        patch: { mode: "statements", on_error: onError, from_statement: at.statement + 1 },
      });
    }
    return actions;
  }
  if (outcome.outcome === "partial" && at) {
    return [
      {
        kind: "again",
        label: "Run the rest statement by statement…",
        patch: { mode: "statements", on_error: "stop", from_statement: at.statement + 1 },
      },
    ];
  }
  if ((outcome.outcome === "rolled_back" || outcome.outcome === "not_started") && code && NEEDS_STATEMENTS.has(code)) {
    return [
      {
        kind: "again",
        label: "Run statement by statement…",
        patch: { mode: "statements", on_error: "stop", from_statement: 1 },
      },
    ];
  }
  if (outcome.outcome !== "completed" && outcome.outcome !== "unknown") {
    return [{ kind: "again", label: "Run again…", patch: null }];
  }
  return [];
}

function runReport(outcome: JobOutcomeEvent, command: JobCommand | null, error: ErrorLine | null): Words {
  const run = runCommand(command);
  const notes = outcome.warnings;
  const at = outcome.stopped_at;
  const resume = outcome.resume_at ?? null;
  const actions = runActions(outcome, command, error?.code ?? null);
  const where = at ? ` at line ${at.line.toLocaleString()}` : "";
  switch (outcome.outcome) {
    case "completed": {
      const undone = resume !== null;
      const skipped = outcome.error_count;
      const title = undone
        ? "Read to the end, but some statements were rolled back"
        : skipped
          ? `Ran ${fileName(outcome.path)} with ${plural(skipped, "error")} skipped`
          : `Ran ${fileName(outcome.path)}`;
      return {
        tone: undone || skipped || notes.length ? "warning" : "success",
        title,
        detail: `${statementsApplied(outcome.statements)} · ${plural(outcome.rows, "row")} affected`,
        notes,
        actions,
      };
    }
    case "rolled_back":
      return {
        tone: outcome.cancelled ? "muted" : "danger",
        title: outcome.cancelled ? "Cancelled: nothing was applied" : "Nothing was applied: the transaction was rolled back",
        detail: null,
        notes,
        actions,
      };
    case "partial":
      return {
        tone: "danger",
        title: `Partly applied: the transaction ended${where}`,
        detail: "The statements before it are applied; the rest are not.",
        notes,
        actions,
      };
    case "stopped":
      return {
        tone: outcome.cancelled ? "warning" : "danger",
        title: `${outcome.cancelled ? "Cancelled" : "Stopped"}${where}`,
        detail: statementsApplied(outcome.statements),
        notes,
        actions,
      };
    case "not_started":
      return {
        tone: outcome.cancelled ? "muted" : "danger",
        title: outcome.cancelled ? "Cancelled before anything ran" : "Not started: nothing was run",
        detail: null,
        notes,
        actions,
      };
    default: // unknown, failed (never for a run)
      return {
        tone: "danger",
        title: `Outcome unknown${where}`,
        // Statement by statement and the backend can place the stop (it gave `resume_at`), the statements before it
        // took effect; in one transaction, or when what ran before it is not known, nothing can be said
        detail:
          run?.mode === "statements" && at && resume
            ? `${statementsApplied(outcome.statements)} before line ${at.line.toLocaleString()}; that line may or may not have taken effect.`
            : NOT_KNOWN_NOTE,
        notes,
        actions,
      };
  }
}

/** The frontend's own ending (no outcome from the backend): what is known, and nothing more. */
function lossReport(view: JobView): JobReport {
  const { state, kind } = view;
  const base = { error: null, hint: null, notes: [], skipped: [], moreSkipped: 0 };
  const again: JobAction = { kind: "again", label: kind === "dump" ? "Dump again…" : "Run again…", patch: null };
  const canAgain = view.command ? [again] : [];
  switch (state.loss) {
    case "open_failed":
      return { ...base, tone: "danger", title: "Could not connect: nothing was sent", detail: state.openError?.message ?? null, actions: [] };
    case "not_sent":
      return {
        ...base,
        tone: "muted",
        title: state.cancelRequestedAt !== null ? "Cancelled before it was sent" : "Not sent: the connection went away first",
        detail: "Nothing ran.",
        actions: canAgain,
      };
    default: {
      const notListed = state.loss === "not_listed";
      const title = notListed
        ? "The backend has no record of this job: it restarted, or never got the command"
        : "The backend stopped before the job reported its outcome";
      if (kind === "dump") {
        return {
          ...base,
          tone: "danger",
          title,
          detail: "A dump that does not finish writes no file (at most a hidden .part file beside it, which can be deleted).",
          actions: canAgain,
        };
      }
      const run = runCommand(view.command);
      const statements = state.progress?.statements ?? 0;
      const detail =
        run === null
          ? NOT_KNOWN_NOTE
          : run.mode === "all_or_nothing"
            ? state.progress?.stage === "commit"
              ? "It stopped during COMMIT: what was applied is not known. Check the target."
              : "The run was one transaction, which the server rolls back when its connection ends: nothing should be applied. Check the target."
            : // the progress counts the statements inside an open file transaction until it settles (pre-flight M5)
              `About ${plural(statements, "statement")} had run by then (the last progress); any in a transaction the file had open are rolled back by the server, and the statement running may or may not have taken effect.`;
      return { ...base, tone: "danger", title, detail, actions: [] };
    }
  }
}

/** How the job ended, in words; null while it runs. */
export function jobReport(view: JobView): JobReport | null {
  const { state, kind, command } = view;
  if (state.phase !== "ended") return null;
  const outcome = state.outcome;
  if (!outcome) return lossReport(view);
  const error = errorLine(outcome, state);
  const words = kind === "dump" ? dumpReport(outcome, command, error) : runReport(outcome, command, error);
  const skipped = outcome.errors.map((item) => ({
    line: item.line ?? null,
    statement: item.statement ?? null,
    code: item.code ?? null,
    label: codeLabel(item.code),
    message: item.message,
  }));
  return {
    ...words,
    error,
    hint: codeHint(error?.code ?? null, kind),
    skipped,
    moreSkipped: Math.max(0, outcome.error_count - skipped.length),
  };
}

export interface ProgressView {
  headline: string;
  detail: string | null;
  /** 0–100 when the job knows its total (a run: the file's bytes). */
  percent: number | null;
  eta: string | null;
}

const ETA_AFTER_MS = 3_000;
const ETA_MIN_SHARE = 0.01;

/** "about 3 min left" once a run has read 1% of its file for 3 s; null before, or when the total is unknown. */
export function etaText(done: number, total: number | null | undefined, elapsedMs: number): string | null {
  if (!total || done <= 0 || done >= total || elapsedMs < ETA_AFTER_MS || done / total < ETA_MIN_SHARE) return null;
  const left = (elapsedMs * (total - done)) / done;
  if (left < 60_000) return "less than a minute left";
  const minutes = Math.round(left / 60_000);
  return minutes < 120 ? `about ${minutes} min left` : `about ${Math.round(minutes / 60)} h left`;
}

const DUMP_STAGES: Partial<Record<JobProgressEvent["stage"], string>> = {
  starting: "Reading the catalog",
  schema: "Writing the schema",
  post_data: "Writing what comes after the data",
};

/** What a running job's dialog and row say: `tables` are the dump's tables in their order (from its plan), if known. */
export function progressView(view: JobView, now: number, tables: readonly string[] | null = null): ProgressView {
  const { state, kind } = view;
  const elapsed = formatElapsed(now - state.startedAt);
  if (state.phase === "opening") return { headline: "Connecting…", detail: null, percent: null, eta: null };
  const progress = state.progress;
  if (!progress) return { headline: "Starting…", detail: elapsed, percent: null, eta: null };
  if (kind === "dump") {
    let headline = DUMP_STAGES[progress.stage] ?? "Writing the rows";
    if (progress.stage === "data" && progress.object) {
      const index = tables?.indexOf(progress.object) ?? -1;
      headline = `Writing ${progress.object}${index >= 0 && tables ? ` (${index + 1} of ${tables.length})` : ""}`;
    }
    return {
      headline,
      detail: `${plural(progress.rows, "row")} · ${formatSize(progress.bytes)} written · ${elapsed}`,
      percent: null,
      eta: null,
    };
  }
  if (progress.stage === "commit") return { headline: "Committing…", detail: elapsed, percent: 100, eta: null };
  const total = progress.total_bytes ?? null;
  const parts = [
    plural(progress.statements, "statement"),
    ...(progress.errors ? [`${plural(progress.errors, "error")} skipped`] : []),
    ...(progress.line ? [`line ${progress.line.toLocaleString()}`] : []),
    elapsed,
  ];
  return {
    headline: total ? `${formatSize(progress.bytes)} of ${formatSize(total)}` : formatSize(progress.bytes),
    detail: parts.join(" · "),
    percent: total ? Math.min(100, (progress.bytes / total) * 100) : null,
    eta: etaText(progress.bytes, total, now - state.startedAt),
  };
}

/** One line for the Jobs list: the progress while it runs, the outcome's title once it ended. */
export function jobSummary(view: JobView, now: number): { text: string; tone: Tone | null } {
  const report = jobReport(view);
  if (report) return { text: report.title, tone: report.tone };
  const progress = progressView(view, now);
  return { text: [progress.headline, progress.eta].filter(Boolean).join(" · "), tone: null };
}

/**
 * The notification for a job that ended after at least `thresholdS` seconds (S8's `notifications.long_query_seconds`,
 * 0: never) while DbaGuy was in the background (R-06-15); none for a job the user cancelled or one that never started.
 */
export function jobNotification(
  view: JobView & { title: string },
  thresholdS: number,
  focused: boolean,
): { title: string; body: string } | null {
  const { state } = view;
  const report = jobReport(view);
  if (!report || focused || thresholdS <= 0 || state.endedAt === null) return null;
  if (state.cancelRequestedAt !== null || state.loss === "open_failed" || state.loss === "not_sent") return null;
  const took = state.endedAt - state.startedAt;
  if (took < thresholdS * 1000) return null;
  const what = view.kind === "dump" ? "Dump" : "SQL file run";
  const verdict = report.tone === "success" ? "finished" : "ended";
  return { title: `${what} ${verdict} after ${formatElapsed(took)}`, body: `${report.title} · ${view.title}` };
}
