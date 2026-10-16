import type { ColumnMeta } from "@/lib/api/types";
import { describeCell } from "@/lib/cells";

export const ROW_HEIGHT = 22;
export const HEADER_HEIGHT = 40;
export const ROW_NUMBER_MIN = 44;
export const MIN_WIDTH = 60;
export const MAX_WIDTH = 400;
export const AUTOFIT_MAX = 600;

/** Width in px of `text` drawn in one of the grid's fonts. */
export type TextMeasure = (text: string, font: "cell" | "header" | "type") => number;

const PADDING = 17; // px-2 on both sides of a cell plus its 1 px right border
/** Room for the sort control next to a header name (GridHeader), when the grid sorts. */
const SORT_CONTROL = 24;
const SAMPLE_ROWS = 100;
const AUTOFIT_SAMPLE = 2000;
const MEASURE_CHARS = 200; // a column is never wider than this many characters
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

function headerWidth(column: ColumnMeta, measure: TextMeasure, sortable: boolean): number {
  return Math.max(measure(column.name, "header") + (sortable ? SORT_CONTROL : 0), measure(column.type_name, "type"));
}

function cellWidth(value: unknown, column: ColumnMeta, measure: TextMeasure): number {
  const display = describeCell(value, column.category, MEASURE_CHARS);
  return measure(display.text, "cell") + (display.detail ? measure(display.detail, "cell") : 0);
}

/** Number of leading rows `estimateWidths` looks at: widths are final once this many rows are loaded. */
export const sampledRows = (rows: unknown[][]) => Math.min(rows.length, SAMPLE_ROWS);

/** Initial widths: header text vs. the first rows' display text, clamped to MIN_WIDTH–MAX_WIDTH. */
export function estimateWidths(
  columns: ColumnMeta[],
  rows: unknown[][],
  measure: TextMeasure,
  sortable: boolean,
): number[] {
  const sample = rows.slice(0, SAMPLE_ROWS);
  return columns.map((column, c) => {
    let width = headerWidth(column, measure, sortable);
    for (const row of sample) width = Math.max(width, cellWidth(row[c], column, measure));
    return clamp(Math.ceil(width) + PADDING, MIN_WIDTH, MAX_WIDTH);
  });
}

/** Width fitting the loaded rows (at most AUTOFIT_SAMPLE, evenly spaced), clamped to MIN_WIDTH–AUTOFIT_MAX. */
export function autofitWidth(
  column: ColumnMeta,
  rows: unknown[][],
  col: number,
  measure: TextMeasure,
  sortable: boolean,
): number {
  const step = Math.max(1, Math.ceil(rows.length / AUTOFIT_SAMPLE));
  let width = headerWidth(column, measure, sortable);
  for (let r = 0; r < rows.length; r += step) width = Math.max(width, cellWidth(rows[r][col], column, measure));
  return clamp(Math.ceil(width) + PADDING, MIN_WIDTH, AUTOFIT_MAX);
}

/** The fixed row-number column: wide enough for `digits` digits (the digit count of the row count). */
export function rowNumberWidth(digits: number, measure: TextMeasure): number {
  return Math.max(ROW_NUMBER_MIN, Math.ceil(measure("9".repeat(digits), "cell")) + PADDING);
}

let shared: TextMeasure | null = null;

/**
 * One shared canvas measuring the grid's fonts, read from the CSS theme (`--font-mono`, `--font-sans`). Keep these in
 * sync with the classes the grid renders: cells `font-mono text-[12px]`, header name `font-mono font-semibold
 * text-[12px]`, header type `font-sans text-[11px]`.
 */
export function canvasMeasure(): TextMeasure {
  if (shared) return shared;
  const context = document.createElement("canvas").getContext("2d");
  if (!context) return (shared = (text) => text.length * 7.5); // no canvas: roughly 12 px monospace
  const css = getComputedStyle(document.documentElement);
  const mono = css.getPropertyValue("--font-mono").trim() || "monospace";
  const sans = css.getPropertyValue("--font-sans").trim() || "sans-serif";
  const fonts = { cell: `12px ${mono}`, header: `600 12px ${mono}`, type: `11px ${sans}` };
  let current = "";
  shared = (text, font) => {
    if (current !== font) {
      context.font = fonts[font];
      current = font;
    }
    return context.measureText(text).width;
  };
  return shared;
}
