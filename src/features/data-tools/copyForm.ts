import { ApiError } from "@/lib/api/client";
import type {
  CopyDataCommand,
  EngineInfo,
  ObjectNode,
  Profile,
  TargetSpec,
  TransferPlan,
  TransferRequest,
} from "@/lib/api/types";
import { defaultSchema, relationHasRows, type CopyEntry } from "./entryPoints";

/*
 * The copy dialog's form (spec ⑥ §3.6 CopyDataDialog, §3.5.1): where the rows go, how, and which columns change on the
 * way; the preview request it makes (POST /data/transfer/plan) and the command that runs that preview once (D-06-8: the
 * command carries the plan's id only). Pure.
 */

// A target connection starts in its default schema: `entryPoints` writes the rule once (L-e), the dialog takes it from here
export { defaultSchema };

export type CopyMode = TargetSpec["mode"];
export type IdentityChoice = "keep" | "generate";

/** A copy holds at most this many tables (`MAX_TRANSFER_TABLES` in domain/transfer.py). */
export const MAX_COPY_TABLES = 500;

export const MODE_LABELS: Record<CopyMode, string> = {
  create: "Create a new table",
  append: "Append to an existing table",
  replace_rows: "Delete its rows, then append",
};
/** What each mode does, under it (R-06-9). */
export const MODE_HINTS: Record<CopyMode, string> = {
  create: "Its columns, NOT NULL and the primary key only: no defaults, indexes, foreign keys or identities",
  append: "Columns are matched by name; a key that is already there stops the table, and its rows are rolled back where the table can",
  replace_rows: "Every row of the target goes first (DELETE FROM), then the new ones follow, in one transaction where the table can roll back",
};
/** The new table's name of a query's result, until the user names it. */
export const QUERY_TABLE = "query_result";

export interface CopyForm {
  targetProfileId: string | null;
  /** The target's database (PostgreSQL, SQL Server); null: the connection's own. */
  targetDatabase: string | null;
  /** The target's schema (MySQL: its database; SQLite: main); null until known. */
  targetSchema: string | null;
  /** One source: the target table's name. Several keep their own names. */
  table: string;
  mode: CopyMode;
  /** "Only rows matching the filter": the table-data tab's filter goes with the copy. */
  onlyFiltered: boolean;
  /** A schema's tables checked; null for one table or a query. */
  checked: readonly string[] | null;
  /** By the source table's key (`TableTransfer.source_key`), then the source column: a new column's type. */
  overrides: Readonly<Record<string, Readonly<Record<string, string>>>>;
  identity: Readonly<Record<string, Readonly<Record<string, IdentityChoice>>>>;
  leftOut: Readonly<Record<string, readonly string[]>>;
}

export function initialCopyForm(entry: CopyEntry, checked: readonly string[] | null = null): CopyForm {
  const { source } = entry;
  return {
    targetProfileId: null,
    targetDatabase: null,
    targetSchema: null,
    table: source.kind === "relation" ? source.name : source.kind === "query" ? QUERY_TABLE : "",
    mode: "create",
    onlyFiltered: source.kind === "relation" && source.filter !== null,
    checked: source.kind === "tables" ? (checked ?? []) : null,
    overrides: {},
    identity: {},
    leftOut: {},
  };
}

/**
 * A new target connection: its own database, and that database's default schema. The types typed over a new table's
 * columns went with the connection before (they were written in its engine's types: another engine reads them
 * differently, or not at all); the choices about the source's own columns and the mode stay.
 */
export function withTarget(form: CopyForm, profile: Profile, engine: EngineInfo): CopyForm {
  const database = "database" in profile ? (profile.database ?? null) : null;
  return {
    ...form,
    overrides: {},
    targetProfileId: profile.id,
    targetDatabase: engine.per_database_connection ? database : null,
    targetSchema: defaultSchema(engine.engine, database),
  };
}

/** Why a connection cannot be a copy's target, or null: a read-only one is refused before anything is sent (403). */
export const targetRefusal = (profile: Profile): string | null =>
  profile.read_only ? "Read-only: nothing can be copied into it" : null;

/** One source (a table, a view or a query's result): the form names its target table; a schema's tables keep their names. */
export const isOne = (entry: CopyEntry): boolean => entry.source.kind !== "tables";

/** The relations a schema's tables source copies: the checked ones, in the tree's order. */
function checkedPaths(entry: CopyEntry, form: CopyForm, tables: readonly ObjectNode[] | null) {
  if (entry.source.kind !== "tables") return null;
  const on = new Set(form.checked ?? []);
  return (tables ?? []).filter((node) => on.has(node.name)).map((node) => node.path);
}

/** What is wrong with the checked tables of a schema's copy (none, or more than a copy takes), or null: said by the list. */
export function tablesProblem(entry: CopyEntry, form: CopyForm, tables: readonly ObjectNode[] | null): string | null {
  if (entry.source.kind !== "tables" || tables === null) return null;
  const count = checkedPaths(entry, form, tables)?.length ?? 0;
  if (count === 0) return "Check the tables to copy";
  return count > MAX_COPY_TABLES ? `A copy takes at most ${MAX_COPY_TABLES.toLocaleString()} tables` : null;
}

/** Why the preview cannot be asked for yet, or null. */
export function requestProblem(entry: CopyEntry, form: CopyForm, tables: readonly ObjectNode[] | null): string | null {
  if (!form.targetProfileId) return "Choose the connection to copy into";
  if (!form.targetSchema) return "Choose the schema to copy into";
  if (isOne(entry) && !form.table.trim()) return "Name the table to copy into";
  if (entry.source.kind === "tables" && tables === null) return "Reading the tables…";
  return tablesProblem(entry, form, tables);
}

/** The preview's request, or null while it cannot be asked (`requestProblem`). The command's plan comes from it. */
export function copyRequest(
  entry: CopyEntry,
  form: CopyForm,
  tables: readonly ObjectNode[] | null,
): TransferRequest | null {
  if (requestProblem(entry, form, tables) !== null || !form.targetProfileId) return null;
  const { source } = entry;
  const filter = source.kind === "relation" && source.filter !== null && form.onlyFiltered ? source.filter : null;
  return {
    source: {
      profile_id: entry.profileId,
      database: entry.database,
      ...(source.kind === "query"
        ? { query: { sql: source.sql, columns: source.columns, title: source.title } }
        : { tables: source.kind === "relation" ? [source.path] : checkedPaths(entry, form, tables) }),
      ...(filter !== null ? { filter } : {}),
    },
    target: {
      profile_id: form.targetProfileId,
      database: form.targetDatabase,
      schema_name: form.targetSchema,
      ...(isOne(entry) ? { table: form.table.trim() } : {}),
      mode: form.mode,
      overrides: form.overrides as Record<string, Record<string, string>>,
      identity: form.identity as Record<string, Record<string, IdentityChoice>>,
      left_out: form.leftOut as Record<string, string[]>,
    },
  };
}

/**
 * Why Copy cannot be pressed, or null: the form is not complete, the preview shown is not this form's (being read,
 * or of the choices before), or it has problems (no plan id: ⑤ E8).
 */
export function copyProblem(
  entry: CopyEntry,
  form: CopyForm,
  tables: readonly ObjectNode[] | null,
  plan: TransferPlan | null,
  behind: boolean,
): string | null {
  const problem = requestProblem(entry, form, tables);
  if (problem) return problem;
  if (!plan || behind) return "Reading the preview…";
  if (!plan.plan_id) return "The preview has problems: see them above";
  return null;
}

/**
 * The command that runs the preview shown (its plan, once); null for a preview without a plan id (it has problems: ⑤ E8),
 * which is never sent. A query's copy reads on its tab's session.
 */
export function copyCommand(plan: TransferPlan, entry: CopyEntry, requestId: string): CopyDataCommand | null {
  if (!plan.plan_id) return null;
  return {
    type: "copy_data",
    request_id: requestId,
    plan_id: plan.plan_id,
    source_session_id: entry.source.kind === "query" ? entry.source.sessionId : null,
  };
}

/** The target tables in the order the copy runs (its progress: "table k of n"). */
export const planTargets = (plan: TransferPlan): string[] => plan.tables.map((table) => table.target);

/** The dialog's title: what is copied. */
export function copyDialogTitle(entry: CopyEntry): string {
  const { source } = entry;
  switch (source.kind) {
    case "relation":
      // `relationHasRows` is the dump's rule (a table or partition holds rows of its own; a view is its definition): here it
      // only tells "Copy public.orders" from "Copy the rows of public.v" (a view's rows are copied too)
      return `Copy ${relationHasRows(source.relationKind) ? "" : "the rows of "}${source.schema}.${source.name}`;
    case "tables":
      return `Copy tables of ${source.schema}`;
    default:
      return `Copy the result of ${source.title} to a table`;
  }
}

/** The job's title, as the Jobs list shows it: `Copy public.orders from Local PG to Dev MySQL`. */
export function copyJobTitle(plan: TransferPlan, from: string, to: string): string {
  const what = plan.tables.length === 1 ? plan.tables[0].source : `${plan.tables.length.toLocaleString()} tables`;
  return `Copy ${what} from ${from} to ${to}`;
}

/**
 * "Copy the rest…" after a copy that stopped part way: the form as it was, with only the tables not copied checked
 * (their source names: the schema's tables are listed by name), so the ones copied are not copied twice.
 */
export function restForm(form: CopyForm, notCopied: readonly string[]): CopyForm {
  return { ...form, checked: notCopied };
}

/**
 * Whose password a preview's `password_required` asks for (pre-flight M3): the preview reads two connections, and the
 * backend names the one it needed (`detail.profile_id`), which is not always the source's. `fallback`: the source, as a
 * dump's or a run's single connection is.
 */
export function passwordOwner(error: Error, fallback: string): string {
  const detail = error instanceof ApiError && typeof error.detail === "object" ? error.detail : null;
  const named = detail !== null && Object.hasOwn(detail, "profile_id") ? (detail as { profile_id: unknown }).profile_id : null;
  return typeof named === "string" ? named : fallback;
}

/** `text` cut at `units` UTF-16 units, never between the two halves of a surrogate pair (an emoji keeps both or neither). */
function cutAt(text: string, units: number): string {
  const end = units > 0 && units < text.length && /[\uD800-\uDBFF]/.test(text[units - 1]) ? units - 1 : units;
  return text.slice(0, end);
}

/** What a query's statement shows in the form: its first line that says something, cut at `max` characters. */
export function statementFirstLine(sql: string, max = 200): string {
  // only the start is read: a statement may be megabytes, and its first line is what is shown
  const line = cutAt(sql, 4 * max).split(/\r\n|\r|\n/).find((text) => text.trim() !== "") ?? "";
  const text = line.trim();
  return text.length > max ? `${cutAt(text, max).trimEnd()}…` : text;
}

/** The backend's answer to a preview that a newer one of the same dialog stopped (its `preview_key`, 06c final review). */
export const PREVIEW_SUPERSEDED = "preview_superseded";

/**
 * Whether a failed preview should be asked once more: the backend stopped it for a newer one of this dialog, and the
 * dialog still waits for it (its read was not given up: the form came back to these choices while it ran).
 */
export function askPreviewAgain(error: unknown, givenUp: boolean): boolean {
  return !givenUp && error instanceof ApiError && error.code === PREVIEW_SUPERSEDED;
}
