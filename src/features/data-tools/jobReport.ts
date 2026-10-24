import type {
  JobError,
  JobObject,
  JobOutcomeEvent,
  JobOutcomeKind,
  JobProgressEvent,
  RunFileCommand,
  StoppedAt,
} from "@/lib/api/types";
import { formatElapsed, formatSize, plural } from "@/lib/format";
import { isSqliteBusy, sqliteBusyNote } from "@/lib/sqliteBusy";
import { FILTER_RULE } from "../table-data/filterValue";
import type { JobCommand, JobKind, JobState } from "./jobState";

/*
 * What a job's dialog and the Jobs list say about it (spec ⑥ §3.6, §4 "結果的說明由前端依 job_outcome 組成"): its
 * progress while it runs and, once it ended, its outcome in words, the error with a label and what to do, the skipped
 * errors, a copy's tables, and the actions that make sense now. Pure. Every action that would run something again goes
 * back to the form, prefilled, so a job is always started (and, on a red connection, confirmed) from the form (B6).
 */

export interface JobView {
  kind: JobKind;
  /** What was sent; null for a job listed after a reload (it was started by another page load). */
  command: JobCommand | null;
  state: JobState;
  /** A copy started here: its source tables' names in the copy's order, for "Copy the rest…" (06c; a JobRecord's). */
  copy?: { sources: readonly string[] } | null;
}

export type Tone = "success" | "warning" | "danger" | "muted";

/** The form's fields a "run again" action changes. */
export interface RunPatch {
  mode: RunFileCommand["mode"];
  on_error: NonNullable<RunFileCommand["on_error"]>;
  from_statement: number;
  /**
   * The line `from_statement` starts at, as the run before placed it (`resume_at`, when exact): the run again sends it
   * (`RunFileCommand.from_line`), so a file edited in between is refused (`file_changed`) instead of skipping or
   * repeating a statement (06b Task 2 review M3, M-b). Absent: nothing is checked.
   */
  from_line?: number;
}

export type JobAction =
  | { kind: "show_in_finder"; label: string; path: string }
  /**
   * A copy back to its form with its own choices (06c): `tables` null as it was; else only these source tables
   * checked ("Copy the rest…": the ones not copied, so none is copied twice).
   */
  | { kind: "copy_again"; label: string; tables: readonly string[] | null }
  /**
   * Back to the form with the job's own settings, changed by `patch` (null: as they were); `note`: said above the
   * form's file (a run again after `file_changed`, whose start is the refused statement's number).
   */
  | { kind: "again"; label: string; patch: RunPatch | null; note?: string };

export interface ErrorLine {
  line: number | null;
  statement: number | null;
  code: string | null;
  label: string | null;
  message: string;
}

/** One table of a copy and how it ended (06c, R-06-12). */
export interface TableLine {
  source: string | null;
  target: string;
  outcome: string;
  rows: number;
  tone: Tone;
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
  /** A copy's tables, in its order, each with how it ended (empty for a dump or a run). */
  tables: TableLine[];
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
  session_busy: "The job's session had closed",
  file_changed: "The file changed since the run before",
  // Couchbase (06b fix wave): a BEGIN WORK the file holds, an answer the query service sent cut or malformed, and TLS
  // failing as a statement opened a new connection to the query node
  transactions_unsupported: "Couchbase runs no transactions",
  protocol_error: "An answer DbaGuy cannot read",
  tls_failed: "TLS failed",
  // Copy data (06c, spec §4)
  plan_expired: "The preview expired",
  plan_mismatch: "Not this connection's preview",
  source_failed: "Reading the source failed",
  row_count_mismatch: "A trigger skipped or added rows",
  row_count_unknown: "The rows written were not confirmed",
  columns_changed: "The columns changed since the preview",
  value_not_copyable: "A value the target cannot hold",
  query_not_select: "Not a query that only reads",
  password_required: "The source connection needs its password",
  connection_failed: "The source connection could not be opened",
  session_not_found: "The query's tab was closed",
  // The source connection was deleted since the preview (`SOURCE_UNAVAILABLE` carries the code of what failed)
  profile_not_found: "The source connection was deleted",
} as const;
export type JobErrorCode = keyof typeof CODE_LABELS;

const SQLITE_BUSY_LABEL = "SQLite file in use";
/** A copy's own words for a code a run has too: a run's "file" is not a copy's table. */
const COPY_LABELS: Partial<Record<string, string>> = {
  transaction_ended: "The table's transaction ended before its COMMIT",
};
/** What to do, where the backend's own text does not say it (a lost connection's depends on the job: `connectionLostHint`). */
const CODE_HINTS: Partial<Record<JobErrorCode, string>> = {
  transaction_open:
    "The connection had a transaction open before the job began (SQL Server: a login whose user options turn IMPLICIT_TRANSACTIONS on). Nothing was sent.",
  invalid_filter: FILTER_RULE,
  file_changed:
    "The file was edited after the run that gave this place, so its statement numbers are not the ones they were. Look at the file, then set where to start again.",
  plan_expired: "Copy again: the preview is read once more.",
  value_not_copyable: "Leave that row out with a filter, or give the column a text type, then copy again.",
  row_count_mismatch: "A trigger on the target changed what was written: copy into a table without it.",
  session_not_found: "Copy again from the result of a tab that is open.",
  // The preview read the source with its password; the backend has forgotten it since (a restart): ask for it again
  password_required: "Unlock that connection (opening it in the tree asks for its password), then copy again.",
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
/** A run again whose statement no longer starts at the line the run before gave (`RunFileCommand.from_line`). */
const FILE_CHANGED = "file_changed";
/** Its form starts at the statement number that was refused: said above the file (06b final review). */
export const FILE_CHANGED_NOTE = "The file changed since the last run: check where to start.";

/** Own properties only: "constructor" and "__proto__" are not codes (the K2 rule). */
const has = <T extends object>(table: T, key: string): key is Extract<keyof T, string> => Object.hasOwn(table, key);

export function codeLabel(code: string | null | undefined, kind?: JobKind): string | null {
  if (!code) return null;
  if (isSqliteBusy(code)) return SQLITE_BUSY_LABEL;
  if (kind === "copy_data" && Object.hasOwn(COPY_LABELS, code)) return COPY_LABELS[code] ?? null;
  return has(CODE_LABELS, code) ? CODE_LABELS[code] : null;
}

/**
 * What a lost connection (the target's: a source that is lost is `source_failed`) means for a copy: each table is a
 * transaction of its own, which the server rolls back as the connection ends, so the tables before it stay.
 */
function copyLostHint(outcome: JobOutcomeEvent): string {
  if (outcome.outcome === "unknown") return "Check the target before copying again.";
  if (outcome.objects.every((table) => table.outcome === "not_started")) {
    return "The connection was lost before the first table started: nothing was copied.";
  }
  return outcome.outcome === "partial"
    ? "The table being copied was rolled back by the server as the connection ended; the tables before it stay copied."
    : "The table being copied was rolled back by the server as the connection ended: nothing was copied.";
}

/** What a lost connection means for what to do next: a run that is unknown must be checked, a dump wrote nothing. */
function connectionLostHint(kind: JobKind, outcome: JobOutcomeKind): string | null {
  if (kind === "run_file") {
    return outcome === "unknown"
      ? // the detail says what is known (a line that may or may not have run, or a transaction the server rolled back)
        "Check the target before running anything again."
      : null; // rolled back (or not started): the server undid what was open, nothing to check
  }
  return outcome === "failed" || outcome === "not_started"
    ? "The connection was lost: nothing was written. Dump again when it is back."
    : null;
}

function codeHint(code: string | null, kind: JobKind, outcome: JobOutcomeEvent): string | null {
  if (isSqliteBusy(code)) {
    return sqliteBusyNote(kind === "dump" ? "dump again" : kind === "copy_data" ? "copy again" : "run the file again");
  }
  if (code === "connection_lost") return kind === "copy_data" ? copyLostHint(outcome) : connectionLostHint(kind, outcome.outcome);
  return code && has(CODE_HINTS, code) ? (CODE_HINTS[code] ?? null) : null;
}

const fileName = (path: string | null | undefined) => (path ? (path.split("/").pop() ?? path) : "the file");
const runCommand = (command: JobCommand | null) => (command?.type === "run_file" ? command : null);
const statementsApplied = (count: number) => `${plural(count, "statement")} applied`;

/** An error of a job (the one that ended it, or one a run skipped) with its label. */
function toErrorLine(error: JobError, kind: JobKind): ErrorLine {
  const code = error.code ?? null;
  return { line: error.line ?? null, statement: error.statement ?? null, code, label: codeLabel(code, kind), message: error.message };
}

/** The error that ended the job: the outcome's, or the live one before an ending without an outcome. */
function endingError(outcome: JobOutcomeEvent | null, state: JobState, kind: JobKind): ErrorLine | null {
  const error = outcome?.error ?? state.error;
  return error ? toErrorLine(error, kind) : null;
}

/**
 * Starting the same job again from its form. Only a job this page started can be (a listed one has no command here:
 * a dump's, a lost run's or any other).
 */
function againAction(kind: JobKind, command: JobCommand | null): JobAction[] {
  if (!command) return [];
  if (kind === "copy_data") return [{ kind: "copy_again", label: "Copy again…", tables: null }];
  return [{ kind: "again", label: kind === "dump" ? "Dump again…" : "Run again…", patch: null }];
}

const NOTHING_WRITTEN_NOTE = "A file that was already at that path is as it was.";
const NOT_KNOWN_NOTE = "What was applied is not known: check the target.";
/**
 * A statement-by-statement run that stopped where the backend cannot say what the statements before it did (no
 * `resume_at`): no run again from the form, whose start would repeat the applied ones (06b final review I1).
 */
export const NO_RESUME_HINT = "Check the target, then choose where to start in a new run (Run SQL file… from the tree).";

type Words = Omit<JobReport, "error" | "skipped" | "moreSkipped" | "hint" | "tables">;

function dumpReport(outcome: JobOutcomeEvent, command: JobCommand | null, error: ErrorLine | null): Words {
  const again = againAction("dump", command);
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

const TABLE_WORDS: Record<NonNullable<JobObject["outcome"]>, { text: string; tone: Tone }> = {
  copied: { text: "copied", tone: "success" },
  rolled_back: { text: "rolled back", tone: "danger" },
  not_started: { text: "not copied", tone: "muted" },
  unknown: { text: "unknown: check it", tone: "danger" },
};

/** A copy's tables as the view lists them (R-06-12: each table's own outcome). */
function tableLines(objects: readonly JobObject[]): TableLine[] {
  return objects.map((object) => {
    const words = TABLE_WORDS[object.outcome ?? "unknown"];
    return { source: object.source ?? null, target: object.name, outcome: words.text, rows: object.rows ?? 0, tone: words.tone };
  });
}

/**
 * What a copy offers to do again (06c): nothing applied (rolled back, not started) — the form as it was; stopped part
 * way — the tables not copied only ("Copy the rest…", so none is copied twice); copied, or not known (check the target
 * first) — nothing.
 */
function copyActions(outcome: JobOutcomeEvent, command: JobCommand | null, sources: readonly string[] | null): JobAction[] {
  if (outcome.outcome === "rolled_back" || outcome.outcome === "not_started") return againAction("copy_data", command);
  if (outcome.outcome !== "partial" || !command || !sources || sources.length !== outcome.objects.length) return [];
  const rest = sources.filter((_, i) => outcome.objects[i].outcome !== "copied");
  return rest.length ? [{ kind: "copy_again", label: "Copy the rest…", tables: rest }] : [];
}

/**
 * A copy that stopped part way: what became of the table it stopped at. A Cancel between two tables, or a connection made
 * read-only meanwhile, stops it with no table rolled back (the next one never started).
 */
function partialDetail(tables: readonly JobObject[]): string {
  const undone = tables.find((table) => table.outcome === "rolled_back");
  if (!undone) return "The tables copied stay; the rest were not started.";
  const rest = tables.some((table) => table.outcome === "not_started") ? "; the rest were not started" : "";
  return `The tables copied stay. ${undone.name} was rolled back${rest}.`;
}

/** A copy that is not known: the table whose COMMIT or rollback could not be told, when the backend names one. */
const copyUnknownDetail = (tables: readonly JobObject[]): string =>
  tables.some((table) => table.outcome === "unknown")
    ? "The tables marked unknown may or may not hold their rows: check the target before copying again."
    : NOT_KNOWN_NOTE;

function copyReport(outcome: JobOutcomeEvent, command: JobCommand | null, sources: readonly string[] | null): Words {
  const notes = outcome.warnings;
  const tables = outcome.objects;
  const copied = tables.filter((table) => table.outcome === "copied").length;
  const actions = copyActions(outcome, command, sources);
  const into = tables.length === 1 ? ` to ${tables[0].name}` : ` in ${plural(tables.length, "table")}`;
  // Nothing of any table was sent: the target's setup failed, or a Cancel came before the first table (no table "was
  // rolled back"; the backend says `not_started` when it had sent nothing at all)
  const noTableStarted = tables.every((table) => table.outcome === "not_started");
  switch (outcome.outcome) {
    case "completed":
      return { tone: notes.length ? "warning" : "success", title: `Copied ${plural(outcome.rows, "row")}${into}`, detail: null, notes, actions };
    case "partial":
      return {
        tone: outcome.cancelled ? "warning" : "danger",
        title: `${outcome.cancelled ? "Cancelled" : "Stopped"}: ${copied.toLocaleString()} of ${plural(tables.length, "table")} copied`,
        detail: partialDetail(tables),
        notes,
        actions,
      };
    case "rolled_back":
      return {
        tone: outcome.cancelled ? "muted" : "danger",
        title: outcome.cancelled
          ? "Cancelled: nothing was copied"
          : noTableStarted
            ? "Nothing was copied"
            : "Nothing was copied: the table was rolled back",
        detail: null,
        notes,
        actions,
      };
    case "not_started":
      return {
        tone: outcome.cancelled ? "muted" : "danger",
        title: outcome.cancelled ? "Cancelled before anything was copied" : "The copy did not start",
        detail: "Nothing was copied.",
        notes,
        actions,
      };
    default: // unknown
      return { tone: "danger", title: "What was copied is not known", detail: copyUnknownDetail(tables), notes, actions };
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
    // The line goes with it only when it is where that statement starts (`exact`): then a file edited since is refused
    // instead of run from another statement (M3); a stop inside a statement (not UTF-8 text…) gives no line to check (M-b)
    const line = resume.exact === false ? {} : { from_line: resume.line };
    const actions: JobAction[] = [
      {
        kind: "again",
        label: `Run again from line ${resume.line.toLocaleString()}`,
        patch: { mode: "statements", on_error: onError, from_statement: resume.statement, ...line },
      },
    ];
    // Not after a Cancel: the user stopped it, there is no failure to skip (running it again from there stays)
    if (at && resume.statement === at.statement && !outcome.cancelled && !(code && NOT_SKIPPABLE.has(code))) {
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
  if (code === FILE_CHANGED) {
    // The place the run before gave is not that statement's any more: the form again as it was, without the line to
    // check (it would be refused again), for the user to set where to start in the file as it is now
    return [
      {
        kind: "again",
        label: "Run again…",
        patch: { mode: run.mode, on_error: run.on_error, from_statement: run.from_statement },
        note: FILE_CHANGED_NOTE,
      },
    ];
  }
  // Stopped, and what ran before is not known (no resume point): starting again from the form would repeat what was
  // applied, as an unknown outcome would (06b final review I1); the hint says to choose the start in a new run
  if (outcome.outcome === "stopped") return [];
  if ((outcome.outcome === "rolled_back" || outcome.outcome === "not_started") && code && NEEDS_STATEMENTS.has(code)) {
    return [
      {
        kind: "again",
        label: "Run statement by statement…",
        patch: { mode: "statements", on_error: "stop", from_statement: 1 },
      },
    ];
  }
  if (outcome.outcome !== "completed" && outcome.outcome !== "unknown") return againAction("run_file", command);
  return [];
}

function runReport(outcome: JobOutcomeEvent, command: JobCommand | null, error: ErrorLine | null): Words {
  const run = runCommand(command);
  const notes = outcome.warnings;
  const at = outcome.stopped_at;
  const resume = outcome.resume_at ?? null;
  const actions = runActions(outcome, command, error?.code ?? null);
  const where = at ? ` at line ${at.line.toLocaleString()}` : "";
  // A run that stopped statement by statement and cannot place what it did (no `resume_at`) says why in its notes: it
  // counts statements it may not have applied, so the count is not promised (pre-flight M5, D1). A completed run
  // always says "applied": the backend takes a rolled-back tail out of its count, and its notes show beside it.
  // Only statement by statement stops (`stopped`); a job listed after a reload has no command and reads the same.
  const doubtful = run?.mode !== "all_or_nothing" && resume === null && notes.length > 0;
  const stoppedApplied = (count: number) =>
    doubtful ? `${plural(count, "statement")} counted as applied; see the notes` : statementsApplied(count);
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
        detail: stoppedApplied(outcome.statements),
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
        detail: run?.mode === "statements" && at && resume ? lostAt(outcome.statements, at, resume) : NOT_KNOWN_NOTE,
        notes,
        actions,
      };
  }
}

/**
 * A statement-by-statement run whose link died at `at`. When the file's own transaction was open, the server rolled
 * it back as the connection ended: `resume` (where a run again starts) is then the transaction's first statement, and
 * what was applied is what came before that one, not before the line that was running.
 */
function lostAt(statements: number, at: StoppedAt, resume: StoppedAt): string {
  const count = statementsApplied(statements);
  if (resume.statement >= at.statement) {
    return `${count} before line ${at.line.toLocaleString()}; that line may or may not have taken effect.`;
  }
  return `${count} before line ${resume.line.toLocaleString()}; the file's transaction from there was rolled back by the server when the connection was lost at line ${at.line.toLocaleString()}.`;
}

/** The title of a job the backend could not report on. */
const LOSS_TITLES: Partial<Record<NonNullable<JobState["loss"]>, string>> = {
  not_listed: "The backend has no record of this job: it restarted, or never got the command",
  no_outcome: "The job finished without reporting an outcome",
  session_gone: "The backend stopped before the job reported its outcome",
};

/** The frontend's own ending (no outcome from the backend): what is known, and nothing more. */
function lossReport(view: JobView): JobReport {
  const { state, kind } = view;
  const base = { error: null, hint: null, notes: [], skipped: [], moreSkipped: 0, tables: [] };
  const canAgain = againAction(kind, view.command);
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
      const title = LOSS_TITLES[state.loss ?? "session_gone"] ?? "The backend stopped before the job reported its outcome";
      if (kind === "dump") {
        return {
          ...base,
          tone: "danger",
          title,
          detail: "A dump that does not finish writes no file (at most a hidden .part file beside it, which can be deleted).",
          actions: canAgain,
        };
      }
      if (kind === "copy_data") {
        return {
          ...base,
          tone: "danger",
          title,
          // each table is a transaction of its own: the server rolled back the one open, except during its COMMIT
          detail: state.progress?.stage === "commit"
            ? "It stopped during a table's COMMIT: whether that table is copied is not known. Check the target."
            : "The table being copied was rolled back by the server as its connection ended; the tables before it stay copied. Check the target.",
          actions: [],
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

/** A run that stopped statement by statement where nothing places what ran before it (I1). */
const stoppedUnplaced = (kind: JobKind, outcome: JobOutcomeEvent) =>
  kind === "run_file" && outcome.outcome === "stopped" && (outcome.resume_at ?? null) === null;

/** How the job ended, in words; null while it runs. */
export function jobReport(view: JobView): JobReport | null {
  const { state, kind, command } = view;
  if (state.phase !== "ended") return null;
  const outcome = state.outcome;
  if (!outcome) return lossReport(view);
  const error = endingError(outcome, state, kind);
  const words =
    kind === "dump"
      ? dumpReport(outcome, command, error)
      : kind === "copy_data"
        ? copyReport(outcome, command, view.copy?.sources ?? null)
        : runReport(outcome, command, error);
  const skipped = outcome.errors.map((skippedError) => toErrorLine(skippedError, kind));
  const hints = [codeHint(error?.code ?? null, kind, outcome), stoppedUnplaced(kind, outcome) ? NO_RESUME_HINT : null];
  return {
    ...words,
    error,
    hint: hints.filter((hint) => hint !== null).join(" ") || null,
    skipped,
    moreSkipped: Math.max(0, outcome.error_count - skipped.length),
    tables: kind === "copy_data" ? tableLines(outcome.objects) : [],
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
  if (kind === "copy_data") {
    const object = progress.object ?? null;
    const index = object && tables ? tables.indexOf(object) : -1;
    const which = index >= 0 && tables && tables.length > 1 ? ` (${index + 1} of ${tables.length})` : "";
    const headline = !object ? "Starting…" : progress.stage === "commit" ? `Committing ${object}${which}` : `Copying ${object}${which}`;
    // rows read so far: the table being copied commits (and counts as copied) only at its end, so none is "copied" yet
    return { headline, detail: `${plural(progress.rows, "row")} read · ${elapsed}`, percent: null, eta: null };
  }
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

/**
 * What Cancel does to a running job, said beside the button (spec ⑥ §3.6): a dump writes no file; a run in one
 * transaction is rolled back; a run statement by statement keeps what took effect, except a transaction the file opened
 * and did not commit, which is rolled back (pre-flight M5). Nothing for a job listed after a reload (its mode is not
 * known here), nor during a run's COMMIT, which Cancel no longer stops.
 */
export function cancelNote(view: JobView): string | null {
  const { command, state } = view;
  if (!command || state.phase === "ended") return null;
  if (command.type === "dump") return "Cancel writes no file";
  if (command.type === "copy_data") {
    return state.progress?.stage === "commit" ? null : "Cancel rolls back the table being copied; the tables before it stay";
  }
  if (state.progress?.stage === "commit") return null;
  return command.mode === "all_or_nothing"
    ? "Cancel rolls the whole run back"
    : "Cancel stops the run: what took effect stays, except a transaction the file opened and did not commit (rolled back)";
}

/** One line for the Jobs list: the progress while it runs, the outcome's title once it ended. */
export function jobSummary(view: JobView, now: number): { text: string; tone: Tone | null } {
  const report = jobReport(view);
  if (report) return { text: report.title, tone: report.tone };
  const progress = progressView(view, now);
  return { text: [progress.headline, progress.eta].filter(Boolean).join(" · "), tone: null };
}

/**
 * Whether a job's ending is news, for its notification and its toast (R-06-15, V11): not when it ended cancelled (the
 * outcome says so, whoever asked: its user here, or in another window) nor when its command never left (`not_sent`).
 * A Cancel that lost the race (the job completed, or ended otherwise first) is news, and so is a cancelled run whose
 * outcome still asks the user to check the target (`partial`, `unknown`: what stood is not what was asked for). The two differ in one case: the
 * notification also says nothing of `open_failed` (nothing ran: no long job ended), while the toast tells it (its view
 * asks for the password or offers Retry).
 */
export const endingIsNews = (state: JobState): boolean =>
  state.phase === "ended" && !quietlyCancelled(state) && state.loss !== "not_sent";

const quietlyCancelled = (state: JobState): boolean =>
  state.outcome?.cancelled === true && state.outcome.outcome !== "partial" && state.outcome.outcome !== "unknown";

/**
 * The notification for a job that ended after at least `thresholdS` seconds (S8's `notifications.long_query_seconds`,
 * 0: never) while DbaGuy was in the background (R-06-15); none for an ending that is not news (`endingIsNews`) or a
 * job that never connected.
 */
export function jobNotification(
  view: JobView & { title: string },
  thresholdS: number,
  focused: boolean,
): { title: string; body: string } | null {
  const { state } = view;
  const report = jobReport(view);
  if (!report || focused || thresholdS <= 0 || state.endedAt === null) return null;
  if (!endingIsNews(state) || state.loss === "open_failed") return null;
  const took = state.endedAt - state.startedAt;
  if (took < thresholdS * 1000) return null;
  const what = view.kind === "dump" ? "Dump" : view.kind === "copy_data" ? "Copy" : "SQL file run";
  const verdict = report.tone === "success" ? "finished" : "ended";
  return { title: `${what} ${verdict} after ${formatElapsed(took)}`, body: `${report.title} · ${view.title}` };
}
