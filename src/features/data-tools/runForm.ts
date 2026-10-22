import type { EngineInfo, FileInspection, Profile, QueryContext, RunFileCommand } from "@/lib/api/types";
import { fileNameOf, pathProblem } from "@/lib/filePaths";
import { FILTERED_ROWS } from "./dumpForm";
import type { RunEntry } from "./entryPoints";
import type { RunPatch } from "./jobReport";

/*
 * Run SQL file's form (spec ⑥ §3.6 RunFileDialog, R-06-6, R-06-7, §12 CR-3): the modes the engine and the file allow
 * and why the others are greyed, what the inspection says first, the confirmation a red connection asks for, and the
 * command. A run is not classified statement by statement (R-06-6), so the dialog says that every statement runs.
 */

export type RunMode = RunFileCommand["mode"];
export type OnError = NonNullable<RunFileCommand["on_error"]>;

/** Where a statement of a file started, as a run before found it (`resume_at`). */
export interface FromLine {
  statement: number;
  line: number;
  path: string;
}

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
  /**
   * A run again's place (`RunPatch.from_line`): statement `statement` of the file at `path` starts at `line`. Sent while
   * the form still starts there in that file; another statement or another file is not checked (`runCommand`).
   */
  fromLine: FromLine | null;
  /** CR-3: a red connection's run waits for this box. */
  confirmed: boolean;
  /**
   * The mode was chosen (by the user, or by a "run again" preset or patch): until then it follows the engine's default
   * as the engine's info loads or the target changes (`followEngineMode`).
   */
  modePicked: boolean;
}

/** CR-3: always said, because a run is not checked statement by statement (R-06-6). */
export const EVERY_STATEMENT_RUNS =
  "Every statement in the file runs as written, DROP and TRUNCATE included: DbaGuy does not check them one by one.";
export const RED_CONFIRM = "I've checked the file and the connection";

/** The backend's texts the dialog looks for in an inspection (sql/dump_header.py, services/data_service.py). */
const DROPS_FIRST = "This dump drops and recreates its objects";
const OTHER_ENGINE = "Written for ";
const CUT_SHORT = "The file does not end with its 'Dump complete' line: it was cut short";

/** The engines whose files usually run statement by statement (R-06-7: MySQL commits around DDL; Couchbase has no transactions). */
const STATEMENTS_FIRST: ReadonlySet<string> = new Set(["mysql", "couchbase"]);

export function defaultMode(engine: EngineInfo | null): RunMode {
  return !engine || STATEMENTS_FIRST.has(engine.engine) || !engine.transactions ? "statements" : "all_or_nothing";
}

/**
 * Whether the entry already chose the mode: a "run again" preset counts as chosen (06b pre-flight L7), so the engine's
 * info arriving a render later never turns its "statement by statement" into the engine's default.
 */
export const presetPicksMode = (entry: RunEntry): boolean => entry.preset?.mode !== undefined;

/** A run again's place in the file at `path`; null when the patch gives no line. */
const placeOf = (patch: Partial<RunPatch>, path: string): FromLine | null =>
  patch.from_line ? { statement: patch.from_statement ?? 1, line: patch.from_line, path: path.trim() } : null;

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
    fromLine: placeOf(preset, entry.path),
    confirmed: false,
    modePicked: presetPicksMode(entry),
  };
}

/**
 * The form with its mode following the engine's default while nobody has chosen one (`form.modePicked` false: the
 * engine's info loads, or the target changes). The same form object when nothing changes.
 */
export function followEngineMode(form: RunForm, engine: EngineInfo | null): RunForm {
  const mode = defaultMode(engine);
  return form.modePicked || mode === form.mode ? form : { ...form, mode };
}

/** The user chose a mode: it stays, whatever the engine. An all-or-nothing run stops at the first error. */
export function pickMode(form: RunForm, mode: RunMode): RunForm {
  return { ...form, mode, modePicked: true, onError: mode === "all_or_nothing" ? "stop" : form.onError };
}

/**
 * The user chose what an error does. Skipping errors is a statement-by-statement choice: it picks that mode too, so the
 * engine's info arriving later never turns it into an all-or-nothing run that stops at the first error.
 */
export function pickOnError(form: RunForm, onError: OnError): RunForm {
  return { ...form, onError, modePicked: form.modePicked || onError === "continue" };
}

/**
 * The form's mode as the engine and the file allow it: an unchosen mode follows the engine (`followEngineMode`), and
 * a mode that is greyed here (all or nothing on a connection without transactions, or for a dump cut short) moves to
 * statement by statement, as the backend would refuse it. The same form object when the mode stays.
 */
export function settledMode(form: RunForm, engine: EngineInfo | null, inspection: FileInspection | null): RunForm {
  const followed = followEngineMode(form, engine);
  const greyed = runChoices(engine, inspection, followed).modes.find((choice) => choice.value === followed.mode)?.disabled;
  const mode: RunMode = greyed ? "statements" : followed.mode;
  return mode === form.mode ? form : { ...form, mode };
}

/**
 * The query context a Couchbase run is sent in, with the three states of a query tab's: undefined is the connection's
 * own default (the job runner fills it in from the job's session), null is none (names must be qualified), an object is
 * explicit. The entry's (its bucket or scope node) only while the form still runs on the entry's own connection: another
 * connection's context means nothing there, so it gets its own default (06b pre-flight L6).
 */
export const queryContextOf = (entry: RunEntry, form: RunForm): QueryContext | null | undefined =>
  form.profileId === entry.profileId ? entry.queryContext : undefined;

/** A query context in words, for the dialog: "bucket B · scope S", "the connection's default" or "none". */
export function queryContextLabel(context: QueryContext | null | undefined): string {
  if (context === undefined) return "the connection's default";
  if (context === null) return "none: names must be qualified";
  return context.scope ? `bucket ${context.bucket} · scope ${context.scope}` : `bucket ${context.bucket}`;
}

/**
 * The form on another connection (the target picker): what belonged to the old one goes: the confirmation (CR-3: it
 * was for that connection), the database and the search path. Going back to the entry's own connection restores the
 * entry's database and search path. The same form object when the connection is the same.
 */
export function retargeted(form: RunForm, profileId: string | null, entry: RunEntry | null = null): RunForm {
  if (profileId === form.profileId) return form;
  const own = entry && profileId === entry.profileId ? entry : null;
  return { ...form, profileId, database: own?.database ?? null, searchPath: own?.searchPath ?? "", confirmed: false };
}

/**
 * Another database of the same connection (the target picker): the confirmation goes (CR-3 is for the connection and
 * the database it runs in). The same form object when the database is the same.
 */
export function withDatabase(form: RunForm, database: string | null): RunForm {
  return database === form.database ? form : { ...form, database, confirmed: false };
}

/**
 * Another file (typed or chosen): the confirmation goes, as for another target (CR-3: "I've checked the file and the
 * connection"). The same form object when the path is the same.
 */
export function withPath(form: RunForm, path: string): RunForm {
  return path === form.path ? form : { ...form, path, confirmed: false };
}

/** A "run again" action's changes on the form (`RunPatch`): its mode counts as chosen; the confirmation is asked again. */
export function patchedForm(form: RunForm, patch: RunPatch | null): RunForm {
  if (!patch) return { ...form, confirmed: false };
  return {
    ...form,
    mode: patch.mode,
    modePicked: true,
    onError: patch.on_error,
    fromStatement: String(patch.from_statement),
    fromLine: placeOf(patch, form.path),
    confirmed: false,
  };
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

/** Which nodes the target's database picker lists: database (PostgreSQL, SQL Server), schema (MySQL), none. */
export function databaseNodes(engine: EngineInfo | null): RunChoices["databases"] {
  if (engine?.engine === "postgres" || engine?.engine === "mssql") return "database";
  return engine?.engine === "mysql" ? "schema" : null;
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
    databases: databaseNodes(engine),
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
  const problem = (): string | null => {
    if (!profile) return "Choose a connection";
    if (profile.read_only) return "This connection is read-only: it cannot run files";
    if (!engine) return "Reading the connection's details…"; // the engines are still loading: waiting, not refusing
    if (!engine.run_file) return "This connection cannot run SQL files";
    if (!form.path.trim()) return "Choose a file";
    const path = pathProblem(form.path);
    if (path) return path;
    if (!inspected) return "The file has not been read yet";
    const from = fromStatementProblem(form.fromStatement);
    if (from) return from;
    return red && !form.confirmed ? `Check “${RED_CONFIRM}” first` : null;
  };
  return { red, blocked: problem() };
}

/**
 * The command the form sends. `queryContext` (Couchbase only; `queryContextOf`) keeps its three states on the wire:
 * undefined is left out (the job runner fills in the connection's default once the job's session is open), null is sent
 * (no context), an object as it is; any other engine sends null. `from_line` goes only while the form starts at the
 * statement of the run again's place in that same file (`form.fromLine`): the backend then refuses a file edited since.
 */
export function runCommand(
  form: RunForm,
  engine: EngineInfo,
  queryContext: QueryContext | null | undefined,
  requestId: string,
): RunFileCommand {
  const path = form.path.trim();
  const fromStatement = Number(form.fromStatement.trim());
  const place = form.fromLine;
  const checked = place && place.statement === fromStatement && place.path === path ? { from_line: place.line } : {};
  return {
    type: "run_file",
    request_id: requestId,
    path,
    mode: form.mode,
    on_error: form.mode === "statements" ? form.onError : "stop",
    from_statement: fromStatement,
    ...checked,
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
    queryContext: command.query_context, // undefined: the connection's default, null: none, as it was sent
    path: command.path,
    // as it was: its place in the file too, when it had one (a run again of a run again checks the file again)
    preset: patch ?? {
      mode: command.mode,
      on_error: command.on_error ?? "stop",
      from_statement: command.from_statement ?? 1,
      ...(command.from_line ? { from_line: command.from_line } : {}),
    },
  };
}

/** A file's lines, numbered from `firstLine` (an excerpt, or the inspection's first lines). */
export function numberedLines(text: string, firstLine: number): { number: number; text: string }[] {
  if (!text) return [];
  const lines = text.split("\n");
  if (lines.at(-1) === "") lines.pop();
  return lines.map((line, i) => ({ number: firstLine + i, text: line.endsWith("\r") ? line.slice(0, -1) : line }));
}
