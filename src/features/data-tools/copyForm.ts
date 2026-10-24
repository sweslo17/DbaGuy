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
  append: "Columns are matched by name; a key that is already there rolls the table back",
  replace_rows: "Every row of the target goes first (DELETE FROM), in the same transaction as the new ones",
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

/** A new target connection: its own database, and that database's default schema. */
export function withTarget(form: CopyForm, profile: Profile, engine: EngineInfo): CopyForm {
  const database = "database" in profile ? (profile.database ?? null) : null;
  return {
    ...form,
    targetProfileId: profile.id,
    targetDatabase: engine.per_database_connection ? database : null,
    targetSchema: defaultSchema(engine.engine, database),
  };
}

/** Why a connection cannot be a copy's target, or null: a read-only one is refused before anything is sent (403). */
export const targetRefusal = (profile: Profile): string | null =>
  profile.read_only ? "Read-only: nothing can be copied into it" : null;

const isOne = (entry: CopyEntry) => entry.source.kind !== "tables";

/** The relations a schema's tables source copies: the checked ones, in the tree's order. */
function checkedPaths(entry: CopyEntry, form: CopyForm, tables: readonly ObjectNode[] | null) {
  if (entry.source.kind !== "tables") return null;
  const on = new Set(form.checked ?? []);
  return (tables ?? []).filter((node) => on.has(node.name)).map((node) => node.path);
}

/** Why the preview cannot be asked for yet, or null. */
export function requestProblem(entry: CopyEntry, form: CopyForm, tables: readonly ObjectNode[] | null): string | null {
  if (!form.targetProfileId) return "Choose the connection to copy into";
  if (!form.targetSchema) return "Choose the schema to copy into";
  if (isOne(entry) && !form.table.trim()) return "Name the table to copy into";
  if (entry.source.kind === "tables") {
    if (tables === null) return "Reading the tables…";
    const count = checkedPaths(entry, form, tables)?.length ?? 0;
    if (count === 0) return "Check the tables to copy";
    if (count > MAX_COPY_TABLES) return `A copy takes at most ${MAX_COPY_TABLES.toLocaleString()} tables`;
  }
  return null;
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

/** The command that runs the preview shown (its plan, once). A query's copy reads on its tab's session. */
export function copyCommand(plan: TransferPlan, entry: CopyEntry, requestId: string): CopyDataCommand {
  return {
    type: "copy_data",
    request_id: requestId,
    plan_id: plan.plan_id ?? "",
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
