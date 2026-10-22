import { api, unwrap } from "@/lib/api/client";
import type { ColumnMeta, Dialect, PathSegment } from "@/lib/api/types";

/**
 * What a filter may be (06a ruling U3: the backend refuses anything else, `invalid_filter`): the table-data tab's filter
 * bar and a filtered dump say it.
 */
export const FILTER_RULE =
  "A filter is one condition on the rows, as after WHERE: no ORDER BY, LIMIT or second statement (sort with the column headers).";

/** Tree object kinds that hold rows: they open as a table data tab (keep in sync with select_builder.py). */
export const RELATION_KINDS: ReadonlySet<string> = new Set(["table", "view", "materialized_view", "partition"]);

export interface Relation {
  /** The path's database segment (PostgreSQL); null when the engine has none (SQLite, MySQL: schemas are the root). */
  database: string | null;
  schema: string | null;
  name: string;
  kind: string;
}

/** The relation a tree path points at, or null when its last segment is not a relation (a function, an index, …). */
export function relationOf(path: PathSegment[]): Relation | null {
  const last = path.at(-1);
  if (!last || !RELATION_KINDS.has(last.kind)) return null;
  const database = path[0]?.kind === "database" ? path[0].name : null;
  const schema = path.findLast((segment) => segment.kind === "schema")?.name ?? null;
  return { database, schema, name: last.name, kind: last.kind };
}

/**
 * "Filter by this value": the backend builds a condition matching the grid cell exactly, quoted for the dialect; null
 * when the cell holds only the beginning of its value (cut text or bytes), so no condition can match it.
 */
export async function valueCondition(dialect: Dialect, column: ColumnMeta, value: unknown): Promise<string | null> {
  const client = await api();
  // `value` is required: JSON drops `undefined`, so a missing cell is sent as null (IS NULL) explicitly.
  const body = { dialect, column: column.name, value: value ?? null, category: column.category };
  return unwrap(await client.POST("/sql/value-condition", { body })).condition;
}

/** The current filter AND the new condition (the current one parenthesised, so its ORs stay together). */
export function addCondition(current: string, condition: string): string {
  const trimmed = current.trim();
  return trimmed ? `(${trimmed}) AND ${condition}` : condition;
}
