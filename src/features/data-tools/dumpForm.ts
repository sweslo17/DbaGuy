import type { DumpCommand, DumpObjectKinds, DumpPlanObject, DumpPlanRequest, DumpScope, Engine } from "@/lib/api/types";
import { fileNameOf, pathProblem } from "@/lib/filePaths";
import { relationHasRows, type DumpEntry } from "./entryPoints";

/*
 * The dump dialog's form (spec ⑥ §3.6 DumpDialog, R-06-2, §3.3.1): what can be chosen for this entry and engine, why the
 * rest is greyed, and the plan request and the command it makes. The plan and the dump get the same fields (06a carry:
 * `DumpPlanRequest.drop_first` and `filter`). Pure.
 */

export type DumpContent = NonNullable<DumpCommand["content"]>;

export interface DumpForm {
  content: DumpContent;
  objects: DumpObjectKinds;
  dropFirst: boolean;
  /** "Only rows matching the filter": the table-data tab's filter goes with the dump. */
  onlyFiltered: boolean;
  /** The Tables folder's tables checked; null for any other entry. */
  checked: readonly string[] | null;
  path: string;
}

/** A tables scope names at most this many (`MAX_DUMP_NAMES` in domain/data_jobs.py). */
export const MAX_DUMP_TABLES = 500;

const ALL_KINDS: DumpObjectKinds = { views: true, sequences: true, types: true, routines: true, triggers: true };

/** Each engine's kinds besides tables, as its catalog names them (06a S17: MySQL's events go with its routines, SQL Server's synonyms with its views). */
const KIND_LABELS: Record<Engine, Partial<Record<keyof DumpObjectKinds, string>>> = {
  postgres: { views: "Views and materialized views", sequences: "Sequences", types: "Types", routines: "Functions and procedures", triggers: "Triggers" },
  mysql: { views: "Views", routines: "Routines and events", triggers: "Triggers" },
  mssql: { views: "Views and synonyms", sequences: "Sequences", types: "Types", routines: "Functions and procedures", triggers: "Triggers" },
  sqlite: { views: "Views", triggers: "Triggers" },
  couchbase: {},
};

export const CONTENT_LABELS: Record<DumpContent, string> = {
  both: "Schema and data",
  schema: "Schema only",
  data: "Data only",
};
const NO_ROWS = "A view has no rows of its own: its definition is dumped";
const SCHEMA_ONLY_FILTER = "A schema-only dump writes no rows";
const DATA_ONLY_DROP = "A data-only dump creates no objects, so it drops none";

const isView = (entry: DumpEntry) => entry.target.kind === "relation" && !relationHasRows(entry.target.relationKind);

export function initialDumpForm(entry: DumpEntry, path: string, tables: readonly string[] | null = null): DumpForm {
  return {
    content: isView(entry) ? "schema" : "both",
    objects: ALL_KINDS,
    dropFirst: false,
    onlyFiltered: entry.filter !== null,
    checked: entry.target.kind === "tables" ? (tables ?? []) : null,
    path,
  };
}

export interface Choice<T> {
  value: T;
  label: string;
  /** Why it cannot be chosen; null when it can. */
  disabled: string | null;
}

export interface DumpChoices {
  contents: Choice<DumpContent>[];
  /** The object kinds the entry and engine offer (a table or the Tables folder: its triggers only). */
  kinds: Choice<keyof DumpObjectKinds>[];
  dropFirst: string | null;
  /** "Only rows matching the filter": shown only with a filter; null when it can be checked. */
  filter: { shown: boolean; disabled: string | null };
}

export function dumpChoices(entry: DumpEntry, engine: Engine, form: DumpForm): DumpChoices {
  const view = isView(entry);
  const contents = (["both", "schema", "data"] as const).map((value) => ({
    value,
    label: CONTENT_LABELS[value],
    disabled: view && value !== "schema" ? NO_ROWS : null,
  }));
  const labels = KIND_LABELS[engine];
  const wanted: (keyof DumpObjectKinds)[] =
    entry.target.kind === "relation" ? (view ? [] : ["triggers"]) : entry.target.kind === "tables" ? ["triggers"] : ["views", "sequences", "types", "routines", "triggers"];
  const kinds = wanted.flatMap((key) => (labels[key] ? [{ value: key, label: labels[key], disabled: form.content === "data" ? "A data-only dump writes no objects" : null }] : []));
  return {
    contents,
    kinds,
    dropFirst: form.content === "data" ? DATA_ONLY_DROP : null,
    filter: { shown: entry.filter !== null, disabled: form.content === "schema" ? SCHEMA_ONLY_FILTER : null },
  };
}

/** What the form dumps; null while the Tables folder has none checked. */
export function dumpScope(entry: DumpEntry, form: DumpForm): DumpScope | null {
  const target = entry.target;
  switch (target.kind) {
    case "database":
      return { kind: "database", names: [] };
    case "schema":
      return { kind: "schema", schema_name: target.schema, names: [] };
    case "relation":
      return { kind: "tables", schema_name: target.schema, names: [target.name] };
    case "tables":
      return form.checked?.length ? { kind: "tables", schema_name: target.schema, names: [...form.checked] } : null;
  }
}

/** The filter the form sends: the entry's, while "Only rows matching the filter" is checked and rows are written. */
const sentFilter = (entry: DumpEntry, form: DumpForm) =>
  entry.filter !== null && form.onlyFiltered && form.content !== "schema" ? entry.filter : null;

/** Why the Tables folder's checked tables cannot be dumped (none, or more than a dump names); null when they can. */
export function tablesProblem(entry: DumpEntry, form: DumpForm): string | null {
  if (entry.target.kind !== "tables") return null;
  if (!form.checked?.length) return "Check at least one table";
  if (form.checked.length > MAX_DUMP_TABLES) return `A dump of chosen tables names at most ${MAX_DUMP_TABLES}: dump the schema instead`;
  return null;
}

/** Why Dump cannot start yet; null when it can. */
export function dumpProblem(entry: DumpEntry, form: DumpForm): string | null {
  const tables = tablesProblem(entry, form);
  if (tables) return tables;
  if (!form.path.trim()) return "Choose a file to dump into";
  return pathProblem(form.path);
}

/**
 * The plan's request (the same fields as the command); null while there is no scope, or more tables are checked than a
 * dump names (the backend would refuse the request: nothing is asked).
 */
export function dumpPlanRequest(entry: DumpEntry, form: DumpForm): DumpPlanRequest | null {
  const scope = dumpScope(entry, form);
  if (!scope || tablesProblem(entry, form)) return null;
  const path = form.path.trim();
  return {
    profile_id: entry.profileId,
    database: entry.database,
    scope,
    content: form.content,
    objects: form.objects,
    path: path && !pathProblem(path) ? path : null,
    drop_first: form.dropFirst && form.content !== "data",
    filter: sentFilter(entry, form),
  };
}

export function dumpCommand(entry: DumpEntry, form: DumpForm, requestId: string): DumpCommand {
  const scope = dumpScope(entry, form);
  if (!scope || dumpProblem(entry, form)) throw new Error("The dump's form is not complete");
  return {
    type: "dump",
    request_id: requestId,
    path: form.path.trim(),
    scope,
    content: form.content,
    objects: form.objects,
    drop_first: form.dropFirst && form.content !== "data",
    filter: sentFilter(entry, form),
  };
}

/** The dump dialog's title (spec ⑥ §3.6: "Dump <schema> to a SQL file"). */
export function dumpDialogTitle(entry: DumpEntry): string {
  const target = entry.target;
  const what =
    target.kind === "database"
      ? `the database ${target.database}`
      : target.kind === "schema"
        ? target.schema
        : target.kind === "tables"
          ? `tables of ${target.schema}`
          : target.schema === "main"
            ? target.name
            : `${target.schema}.${target.name}`;
  return `Dump ${what} to a SQL file`;
}

/** What the job list calls a dump (as the backend's `DumpJob.title` does). */
export function dumpTitle(scope: DumpScope, connection: string, path: string): string {
  const names = scope.names ?? [];
  const what =
    scope.kind === "database"
      ? "the database"
      : scope.kind === "schema"
        ? (scope.schema_name ?? "")
        : names.length === 1
          ? `${scope.schema_name}.${names[0]}`
          : `${names.length} tables`;
  return `Dump ${what} of ${connection} to ${fileNameOf(path)}`;
}

/** "Dump again…": the form as the job was sent (its path too: the file is written again, as a whole). */
export function dumpFormFromCommand(entry: DumpEntry, command: DumpCommand): DumpForm {
  const names = command.scope.names ?? [];
  return {
    content: command.content ?? "both",
    objects: command.objects ?? ALL_KINDS,
    dropFirst: command.drop_first ?? false,
    onlyFiltered: command.filter != null,
    checked: entry.target.kind === "tables" ? names : null,
    path: command.path,
  };
}

export interface PlanLine {
  kind: string;
  count: number;
  /** ≈ the rows of the tables of that kind (the catalog's statistics); null when none is known. */
  rows: number | null;
}

/** The plan's objects by kind, in the order they first appear (the file's order): "12 tables · ≈ 1,234 rows". */
export function planSummary(objects: readonly DumpPlanObject[]): PlanLine[] {
  const lines = new Map<string, PlanLine>();
  for (const object of objects) {
    const line = lines.get(object.kind) ?? { kind: object.kind, count: 0, rows: null };
    line.count += 1;
    if (object.rows_estimate != null) line.rows = (line.rows ?? 0) + object.rows_estimate;
    lines.set(object.kind, line);
  }
  return [...lines.values()];
}

/** A count of objects of a kind, as the catalog names it: "12 tables", "1 view", "2 policies", "3 statistics". */
export function kindCount(count: number, kind: string): string {
  if (count === 1) return `1 ${kind}`;
  const many = kind.endsWith("statistics")
    ? kind
    : /(?:s|sh|ch|x)$/.test(kind)
      ? `${kind}es`
      : /[^aeiou]y$/.test(kind)
        ? `${kind.slice(0, -1)}ies`
        : `${kind}s`;
  return `${count.toLocaleString()} ${many}`;
}

/** One line of the plan's summary: "12 tables · ≈ 1,234,567 rows", "2 views". */
export const planLineText = (line: PlanLine): string =>
  line.rows === null ? kindCount(line.count, line.kind) : `${kindCount(line.count, line.kind)} · ≈ ${line.rows.toLocaleString()} rows`;

/**
 * The backend's warning that a filtered dump drops its table and so replaces it with only the matching rows (06a F2,
 * `filter_warnings`); an inspection of such a file starts the same way (`dump_header.FILTERED_ROWS`).
 */
export const FILTERED_ROWS = "The file holds only the rows of ";

/**
 * The backend's warning that a Drop objects first file drops an object something outside it depends on, so that
 * restoring it where that exists fails at the DROP (06b I2, `dump_script.drop_warnings`); its text starts this way.
 */
export const DROP_BLOCKED = "Drop objects first: ";

const PROMINENT = [FILTERED_ROWS, DROP_BLOCKED];
const isProminent = (warning: string) => PROMINENT.some((start) => warning.startsWith(start));

/**
 * The plan's warnings, those that change what restoring the file does first (F2, I2: shown in red, above the others),
 * each group in the backend's order.
 */
export function planWarnings(warnings: readonly string[]): { prominent: string[]; other: string[] } {
  return { prominent: warnings.filter(isProminent), other: warnings.filter((warning) => !isProminent(warning)) };
}

/** The dump's tables in the plan's order: the job's "table k of n". */
export const planTables = (objects: readonly DumpPlanObject[]): string[] =>
  objects.filter((object) => object.kind === "table").map((object) => object.name);
