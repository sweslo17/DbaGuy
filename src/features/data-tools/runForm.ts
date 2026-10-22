import type { EngineInfo, FileInspection, Profile, QueryContext, RunFileCommand } from "@/lib/api/types";
import { fileNameOf, pathProblem } from "@/lib/filePaths";
import type { RunEntry } from "./entryPoints";
import type { RunPatch } from "./jobReport";

/*
 * Run SQL file's form (spec ⑥ §3.6 RunFileDialog, R-06-6, R-06-7, §12 CR-3): the modes the engine and the file allow
 * and why the others are greyed, what the inspection says first, the confirmation a red connection asks for, and the
 * command. A run is not classified statement by statement (R-06-6), so the dialog says that every statement runs.
 */

export type RunMode = RunFileCommand["mode"];
export type OnError = NonNullable<RunFileCommand["on_error"]>;

export interface RunForm {
  profileId: string | null;
  database: string | null;
  /** PostgreSQL: one schema; "" for none. */
  searchPath: string;
  path: string;
  mode: RunMode;
  onError: OnError;
  /** "Start at statement N", as typed. */
  fromStatement: string;
  /** CR-3: a red connection's run waits for this box. */
  confirmed: boolean;
}

/** CR-3: always said, because a run is not checked statement by statement (R-06-6). */
export const EVERY_STATEMENT_RUNS =
  "Every statement in the file runs as written, DROP and TRUNCATE included: DbaGuy does not check them one by one.";
export const RED_CONFIRM = "I've checked the file and the connection";

/** The backend's texts the dialog looks for in an inspection (sql/dump_header.py, services/data_service.py). */
const FILTERED_ROWS = "The file holds only the rows of ";
const DROPS_FIRST = "This dump drops and recreates its objects";
const OTHER_ENGINE = "Written for ";
const CUT_SHORT = "The file does not end with its 'Dump complete' line: it was cut short";

/** The engines whose files usually run statement by statement (R-06-7: MySQL commits around DDL; Couchbase has no transactions). */
const STATEMENTS_FIRST: ReadonlySet<string> = new Set(["mysql", "couchbase"]);

export function defaultMode(engine: EngineInfo | null): RunMode {
  return !engine || STATEMENTS_FIRST.has(engine.engine) || !engine.transactions ? "statements" : "all_or_nothing";
}

export function initialRunForm(entry: RunEntry, engine: EngineInfo | null): RunForm {
  const preset = entry.preset ?? {};
  return {
    profileId: entry.profileId,
    database: entry.database,
    searchPath: entry.searchPath ?? "",
    path: entry.path,
    mode: preset.mode ?? defaultMode(engine),
    onError: preset.on_error ?? "stop",
    fromStatement: String(preset.from_statement ?? 1),
    confirmed: false,
  };
}

/**
 * Whether the entry already chose the mode: a "run again" preset counts as chosen (06b pre-flight L7), so the engine's
 * info arriving a render later never turns its "statement by statement" into the engine's default.
 */
export const presetPicksMode = (entry: RunEntry): boolean => entry.preset?.mode !== undefined;

/**
 * The form with its mode following the engine's default while nobody has chosen one (`picked` false: the engine's info
 * loads, or the target changes). The same form object when nothing changes.
 */
export function followEngineMode(form: RunForm, engine: EngineInfo | null, picked: boolean): RunForm {
  const mode = defaultMode(engine);
  return picked || mode === form.mode ? form : { ...form, mode };
}

/**
 * The Couchbase query context a run is sent in: the entry's (the bucket or scope node), only while the form still runs
 * on the entry's own connection: another connection's context means nothing there (06b pre-flight L6); null: the
 * connection's default.
 */
export const queryContextOf = (entry: RunEntry, form: RunForm): QueryContext | null =>
  form.profileId === entry.profileId ? entry.queryContext : null;

/** A "run again" action's changes on the form (`RunPatch`); the confirmation is asked again. */
export function patchedForm(form: RunForm, patch: RunPatch | null): RunForm {
  if (!patch) return { ...form, confirmed: false };
  return { ...form, mode: patch.mode, onError: patch.on_error, fromStatement: String(patch.from_statement), confirmed: false };
}

export interface ModeChoice {
  value: RunMode;
  label: string;
  disabled: string | null;
  /** What to know about it here (MySQL's DDL). */
  note: string | null;
}

export interface RunChoices {
  modes: ModeChoice[];
  /** "Skip errors and continue": why it cannot be chosen, or null. */
  skip: string | null;
  /** PostgreSQL takes a search path. */
  searchPath: boolean;
  /** Which nodes the database picker lists: database (PostgreSQL, SQL Server), schema (MySQL), none. */
  databases: "database" | "schema" | null;
}

export function runChoices(engine: EngineInfo | null, inspection: FileInspection | null, form: RunForm): RunChoices {
  const cutShort = !!inspection?.header && inspection.warnings.includes(CUT_SHORT);
  const noTransactions = engine && !engine.transactions;
  return {
    modes: [
      {
        value: "all_or_nothing",
        label: "All or nothing",
        disabled: noTransactions
          ? "This connection has no transactions: each statement takes effect on its own"
          : cutShort
            ? "The dump is cut short: all or nothing refuses it"
            : null,
        note:
          engine?.engine === "mysql"
            ? "MySQL commits around DDL (CREATE, ALTER, DROP…): all or nothing only fits files without it, and DbaGuy's own MySQL dumps run statement by statement."
            : null,
      },
      { value: "statements", label: "Statement by statement", disabled: null, note: null },
    ],
    skip: form.mode === "all_or_nothing" ? "All or nothing stops at the first error" : null,
    searchPath: engine?.engine === "postgres",
    databases: engine?.engine === "postgres" || engine?.engine === "mssql" ? "database" : engine?.engine === "mysql" ? "schema" : null,
  };
}

/** What the inspection says, the warnings that change what a run does first (F2: a filtered file replaces its table). */
export function inspectionNotes(inspection: FileInspection | null): { prominent: string[]; other: string[] } {
  const warnings = inspection?.warnings ?? [];
  const prominent = warnings.filter((w) => w.startsWith(FILTERED_ROWS) || w === DROPS_FIRST || w.startsWith(OTHER_ENGINE));
  return { prominent, other: warnings.filter((w) => !prominent.includes(w)) };
}

export function fromStatementProblem(text: string): string | null {
  return /^\s*[1-9][0-9]{0,9}\s*$/.test(text) ? null : "A statement number, 1 or more";
}

export interface RunGate {
  /** CR-3: a red (production) connection's Run is a danger button and needs the box checked. */
  red: boolean;
  /** Why Run cannot be pressed now; null when it can. */
  blocked: string | null;
}

export function runGate(profile: Profile | null, engine: EngineInfo | null, form: RunForm, inspected: boolean): RunGate {
  const red = profile?.color === "red";
  const blocked = !profile
    ? "Choose a connection"
    : profile.read_only
      ? "This connection is read-only: it cannot run files"
      : !engine?.run_file
        ? "This connection cannot run SQL files"
        : !form.path.trim()
          ? "Choose a file"
          : (pathProblem(form.path) ??
            (!inspected
              ? "The file has not been read yet"
              : (fromStatementProblem(form.fromStatement) ?? (red && !form.confirmed ? `Check “${RED_CONFIRM}” first` : null))));
  return { red, blocked };
}

/** The command the form sends; `queryContext` is the entry's (Couchbase: `queryContextOf`), else the connection's default. */
export function runCommand(form: RunForm, engine: EngineInfo, queryContext: QueryContext | null, requestId: string): RunFileCommand {
  return {
    type: "run_file",
    request_id: requestId,
    path: form.path.trim(),
    mode: form.mode,
    on_error: form.mode === "statements" ? form.onError : "stop",
    from_statement: Number(form.fromStatement.trim()),
    search_path: engine.engine === "postgres" && form.searchPath.trim() ? form.searchPath.trim() : null,
    query_context: engine.query_context ? queryContext : null,
  };
}

/** What the job list calls a run (as the backend's `RunFileJob.title` does). */
export const runTitle = (path: string, connection: string): string => `Run ${fileNameOf(path)} on ${connection}`;

/** "Run again…": the job's own target and file, its settings changed by `patch` (null: as they were). */
export function againEntry(
  job: { profileId: string; database: string | null; command: RunFileCommand },
  patch: RunPatch | null,
): RunEntry {
  const { command } = job;
  return {
    profileId: job.profileId,
    database: job.database,
    searchPath: command.search_path ?? null,
    queryContext: command.query_context ?? null,
    path: command.path,
    preset: patch ?? { mode: command.mode, on_error: command.on_error ?? "stop", from_statement: command.from_statement ?? 1 },
  };
}

/** A file's lines, numbered from `firstLine` (an excerpt, or the inspection's first lines). */
export function numberedLines(text: string, firstLine: number): { number: number; text: string }[] {
  if (!text) return [];
  const lines = text.split("\n");
  if (lines.at(-1) === "") lines.pop();
  return lines.map((line, i) => ({ number: firstLine + i, text: line.endsWith("\r") ? line.slice(0, -1) : line }));
}
