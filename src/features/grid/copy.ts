import { toast } from "@/components/ui/Toast";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import type { ColumnMeta, Dialect, FormatResult, FormatRowsRequest } from "@/lib/api/types";
import { BYTES_PREVIEW, plainValue, type EncodedCell } from "@/lib/cells";
import { plural } from "@/lib/format";
import { writeClipboard } from "@/lib/native";
import { cellCount, type Rect } from "./selection";

export type CopyFormat = "tsv" | "csv" | "json" | "markdown" | "insert" | "in_list" | "column_names";

/** The most rows `/sql/format-rows` accepts; larger selections are exported instead. */
export const COPY_ROW_LIMIT = 100_000;
/** The most cells (rows × columns) a copy sends: a wide selection under the row limit would still stall the app. */
export const COPY_CELL_LIMIT = 1_000_000;

/** "Copy as" menu order. */
export const COPY_FORMATS: readonly CopyFormat[] = [
  "tsv",
  "csv",
  "json",
  "markdown",
  "insert",
  "in_list",
  "column_names",
];

/** SQL++ has no INSERT by columns (it inserts a key and a document). */
const SQLPP_COPY_FORMATS = COPY_FORMATS.filter((format) => format !== "insert");

/** The "Copy as" formats a dialect can produce (null: the connection's type is not known yet, so all of them). */
export function copyFormatsFor(dialect: Dialect | null): readonly CopyFormat[] {
  return dialect === "sqlpp" ? SQLPP_COPY_FORMATS : COPY_FORMATS;
}

export const COPY_FORMAT_LABELS: Record<CopyFormat, string> = {
  tsv: "TSV",
  csv: "CSV",
  json: "JSON",
  markdown: "Markdown",
  insert: "SQL INSERT",
  in_list: "IN list",
  column_names: "Column names",
};

/** Formats whose text depends on the SQL dialect (literals for booleans and bytes). */
const DIALECT_FORMATS: ReadonlySet<CopyFormat> = new Set<CopyFormat>(["insert", "in_list"]);
/** The formatter always takes a dialect; the other formats ignore it. */
const ANY_DIALECT: Dialect = "postgres";

export interface CopyInput {
  /** The selection's rectangle over the rows loaded now. */
  rect: Rect;
  rows: unknown[][];
  columns: ColumnMeta[];
  /** Null while the connection's type is unknown: INSERT and IN list refuse to guess it. */
  dialect: Dialect | null;
  /** INSERT target; without one the formatter writes a `table_name` placeholder. */
  tableName?: { schema: string | null; name: string } | null;
  /** The selection is whole columns (a column or select-all selection). */
  wholeColumns?: boolean;
}

/** TSV/CSV get a header row for whole columns or several rows: a single cell, or part of one row, is just values. */
const wantsHeader = (input: CopyInput) => !!input.wholeColumns || input.rect.bottom > input.rect.top;

/** The rectangle's cells as the grid holds them (encoded): one pass, one array per row, nothing per cell. */
function cellsOf(rows: unknown[][], rect: Rect): unknown[][] {
  const out = new Array<unknown[]>(rect.bottom - rect.top + 1);
  for (let r = rect.top; r <= rect.bottom; r += 1) out[r - rect.top] = rows[r].slice(rect.left, rect.right + 1);
  return out;
}

async function formatRows(body: FormatRowsRequest): Promise<FormatResult> {
  return unwrap(await (await api()).POST("/sql/format-rows", { body }));
}

/**
 * Copies the text being formatted and toasts the outcome; a format error wins over the clipboard error it causes.
 * Never rejects.
 */
async function copyText(formatted: Promise<FormatResult>, success: string, warnings: string[] = []): Promise<void> {
  const written = writeClipboard(formatted.then((result) => result.text));
  written.catch(() => undefined); // awaited below, once the format outcome is known
  let result: FormatResult;
  try {
    result = await formatted;
  } catch (error) {
    toast("error", `Copy failed: ${errorMessage(error)}`);
    return;
  }
  try {
    await written;
  } catch (error) {
    console.warn("Clipboard write failed", error);
    toast("error", `Could not write to the clipboard: ${errorMessage(error)}`);
    return;
  }
  toast("info", success);
  for (const warning of [...result.warnings, ...warnings]) toast("warning", warning);
}

/** Copies text known on the client and toasts `success` (or the clipboard error). Never rejects. */
export function copyPlainText(text: string, success: string): Promise<void> {
  return copyText(Promise.resolve({ text, warnings: [] }), success);
}

/** The grid holds only the start of this value (long text, or bytes beyond the preview). */
function isPartial(value: unknown): boolean {
  if (value === null || typeof value !== "object") return false;
  const cell = value as EncodedCell;
  return cell.$type === "text" || (cell.$type === "bytes" && (cell.size ?? 0) > BYTES_PREVIEW);
}

/** How many of the cells hold only the start of their value (see `isPartial`). */
function countPartial(rows: unknown[][]): number {
  let count = 0;
  for (const row of rows) for (const value of row) if (isPartial(value)) count += 1;
  return count;
}

/**
 * The warning for values the grid cut (long text or JSON, bytes beyond the preview): TSV, CSV, JSON and Markdown write
 * what the grid holds, so a copy holds only their start and would say nothing. INSERT and IN list (the dialect
 * formats) write such a value as NULL and warn about it themselves.
 */
function partialWarnings(format: CopyFormat, rows: unknown[][]): string[] {
  if (DIALECT_FORMATS.has(format)) return [];
  const count = countPartial(rows);
  if (count === 0) return [];
  const each = count === 1 ? "it" : "each";
  return [
    `${plural(count, "value")} cut at the grid's size limit: the copy has only the start of ${each} — export the result for complete values`,
  ];
}

/**
 * Copies one cell's plain value (ruling R-02b-7): what a single-cell ⌘C and the value viewer's "Copy value" write.
 * Never rejects.
 */
export function copyValue(value: unknown): Promise<void> {
  const success = isPartial(value)
    ? "Copied the start of a truncated value; export the result for all of it"
    : "Copied value";
  return copyPlainText(plainValue(value), success);
}

/** Copies the names of `columns`, tab-separated (client-side: nothing to format). */
export function copyColumnNames(columns: ColumnMeta[]): Promise<void> {
  const text = columns.map((column) => column.name).join("\t");
  return copyPlainText(text, `Copied ${plural(columns.length, "column name")}`);
}

/**
 * Copies the selected rectangle in `format` through the backend formatter (the one export uses), then toasts the
 * outcome: success, the formatter's warnings (values the grid truncated), or the error. Never rejects.
 */
export async function copySelection(format: CopyFormat, input: CopyInput): Promise<void> {
  const { rect, columns } = input;
  if (format === "column_names") return copyColumnNames(columns.slice(rect.left, rect.right + 1));
  const rowCount = rect.bottom - rect.top + 1;
  const cells = cellCount(rect);
  if (rowCount > COPY_ROW_LIMIT || cells > COPY_CELL_LIMIT) {
    const size = rowCount > COPY_ROW_LIMIT ? plural(rowCount, "row") : plural(cells, "cell");
    toast("warning", `Selection too large to copy (${size}). Use Export instead.`);
    return;
  }
  if (input.dialect === null && DIALECT_FORMATS.has(format)) {
    toast("error", `Connection type unknown: cannot copy as ${COPY_FORMAT_LABELS[format]} yet`);
    return;
  }
  const rows = cellsOf(input.rows, rect);
  const body: FormatRowsRequest = {
    dialect: input.dialect ?? ANY_DIALECT,
    kind: format,
    columns: columns.slice(rect.left, rect.right + 1).map(({ name, category }) => ({ name, category })),
    rows,
    options: {
      header: wantsHeader(input),
      null_text: "",
      table_schema: input.tableName?.schema ?? null,
      table_name: input.tableName?.name ?? null,
    },
  };
  const success = `Copied ${plural(rowCount, "row")} as ${COPY_FORMAT_LABELS[format]}`;
  return copyText(formatRows(body), success, partialWarnings(format, rows));
}

/**
 * ⌘C and the menu's plain Copy: a single cell (not a whole column) copies its plain value on the client, so JSON or
 * multi-line text pastes as itself (ruling R-02b-7); anything larger copies as TSV. Never rejects.
 */
export function copyDefault(input: CopyInput): Promise<void> {
  const { rect } = input;
  if (cellCount(rect) === 1 && !input.wholeColumns) return copyValue(input.rows[rect.top][rect.left]);
  return copySelection("tsv", input);
}
