import type { ColumnMeta } from "@/lib/api/types";
import { isMissing, jsonCellText } from "@/lib/cells";
import type { Rect } from "./selection";

const NUMERIC = /^-?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;
/** Larger selections only show their cell count: summing them would stall a selection change (ruling R-02b-3). */
export const AGGREGATE_CAP = 200_000;

export interface Aggregates {
  cells: number;
  nonNull: number;
  numeric: number;
  sum?: number;
  avg?: number;
  min?: number;
  max?: number;
  /** A value was beyond exact double precision. */
  approximate: boolean;
  /** The selection is larger than the cap: only `cells` is meaningful. */
  capped: boolean;
}

/**
 * A cell's number: a JS number, or the text of a numeric string (a `number` column's big integers and decimals) or of
 * a json cell (Couchbase). `typedOnly` (a `mixed` column: its type is on the cell, so a string is text) leaves plain
 * strings out.
 */
function numberOf(value: unknown, typedOnly: boolean): { value: number; exact: boolean } | null {
  if (typeof value === "number") return { value, exact: Number.isSafeInteger(value) || !Number.isInteger(value) };
  const text = jsonCellText(value) ?? (!typedOnly && typeof value === "string" ? value : null);
  if (text !== null && NUMERIC.test(text.trim())) {
    const digits = text.replace(/[^0-9]/g, "").replace(/^0+/, "").length;
    return { value: Number(text), exact: digits <= 15 };
  }
  return null;
}

/** Status-bar figures over the loaded rows inside `rect`; numbers only from `number` and `mixed` columns. */
export function aggregate(rect: Rect, rows: unknown[][], columns: ColumnMeta[], cap = AGGREGATE_CAP): Aggregates {
  const cells = Math.max(0, rect.bottom - rect.top + 1) * Math.max(0, rect.right - rect.left + 1);
  const result: Aggregates = { cells, nonNull: 0, numeric: 0, approximate: false, capped: cells > cap };
  if (result.capped) return result;
  let sum = 0;
  for (let r = rect.top; r <= rect.bottom; r += 1) {
    const row = rows[r];
    if (!row) continue;
    for (let c = rect.left; c <= rect.right; c += 1) {
      const value = row[c];
      if (value === null || value === undefined || isMissing(value)) continue;
      result.nonNull += 1;
      const category = columns[c]?.category;
      if (category !== "number" && category !== "mixed") continue;
      const n = numberOf(value, category === "mixed");
      if (!n || !Number.isFinite(n.value)) continue;
      result.numeric += 1;
      result.approximate ||= !n.exact;
      sum += n.value;
      result.min = result.min === undefined ? n.value : Math.min(result.min, n.value);
      result.max = result.max === undefined ? n.value : Math.max(result.max, n.value);
    }
  }
  if (result.numeric > 0) {
    result.approximate ||= Math.abs(sum) > Number.MAX_SAFE_INTEGER; // exact values can still add up past 2^53
    result.sum = sum;
    result.avg = sum / result.numeric;
  }
  return result;
}
