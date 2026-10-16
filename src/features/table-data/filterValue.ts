import type { ColumnMeta, Dialect, PathSegment } from "@/lib/api/types";

/** Tree object kinds that hold rows: they open as a table data tab (keep in sync with select_builder.py). */
export const RELATION_KINDS: ReadonlySet<string> = new Set(["table", "view", "materialized_view", "partition"]);

export interface Relation {
  /** The path's database segment (PostgreSQL); null when the engine has none (SQLite). */
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

const NUMERIC = /^-?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;

/** A double-quoted identifier (`"` doubled): valid in PostgreSQL and SQLite. */
export const quoteIdent = (name: string) => `"${name.replace(/"/g, '""')}"`;

/** "Filter by this value": a condition matching the cell exactly; null when it cannot be matched (not filterable). */
export function filterForValue(column: ColumnMeta, value: unknown, dialect: Dialect): string | null {
  const col = quoteIdent(column.name);
  if (value === null || value === undefined) return `${col} IS NULL`;
  if (typeof value === "object") return null; // bytes / truncated text cells cannot be matched exactly
  if (typeof value === "boolean") {
    const literal = dialect === "sqlite" ? (value ? "1" : "0") : value ? "TRUE" : "FALSE";
    return `${col} = ${literal}`;
  }
  const numeric = typeof value === "number" || (column.category === "number" && NUMERIC.test(String(value)));
  if (numeric) return `${col} = ${value}`;
  return `${col} = '${String(value).replace(/'/g, "''")}'`;
}

/** The current filter AND the new condition (the current one parenthesised, so its ORs stay together). */
export function addCondition(current: string, condition: string): string {
  const trimmed = current.trim();
  return trimmed ? `(${trimmed}) AND ${condition}` : condition;
}
