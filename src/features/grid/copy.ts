import { toast } from "@/components/ui/Toast";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import type { ColumnMeta, Dialect, FormatResult, FormatRowsRequest } from "@/lib/api/types";
import { plural } from "@/lib/format";
import type { Rect } from "./selection";

export type CopyFormat = "tsv" | "csv" | "json" | "markdown" | "insert" | "in_list" | "column_names";

/** The most rows `/sql/format-rows` accepts; larger selections are exported instead. */
export const COPY_ROW_LIMIT = 100_000;

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
 * Puts text that may still be on its way onto the clipboard. The clipboard call starts synchronously, inside the
 * user's gesture: WebKit (the Tauri webview) only writes during one, and the formatter's round trip outlives it, so
 * the text goes in as a promise inside a ClipboardItem. Engines that take no promise there get `writeText` once the
 * text is known (Chromium writes without a gesture).
 */
async function writeClipboard(text: Promise<string>): Promise<void> {
  const clipboard = navigator.clipboard as Clipboard | undefined;
  if (!clipboard) throw new Error("The clipboard is not available");
  if (typeof ClipboardItem !== "undefined" && typeof clipboard.write === "function") {
    const blob = text.then((value) => new Blob([value], { type: "text/plain" }));
    blob.catch(() => undefined); // a format error is reported by the caller
    try {
      await clipboard.write([new ClipboardItem({ "text/plain": blob })]);
      return;
    } catch {
      // No promise support, or the text failed (then `await text` below rethrows that error).
    }
  }
  await clipboard.writeText(await text);
}

/**
 * Copies the text being formatted and toasts the outcome; a format error wins over the clipboard error it causes.
 * Never rejects.
 */
async function copyText(formatted: Promise<FormatResult>, success: string): Promise<void> {
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
  for (const warning of result.warnings) toast("warning", warning);
}

/** Copies the names of `columns`, tab-separated (client-side: nothing to format). */
export function copyColumnNames(columns: ColumnMeta[]): Promise<void> {
  const text = columns.map((column) => column.name).join("\t");
  return copyText(Promise.resolve({ text, warnings: [] }), `Copied ${plural(columns.length, "column name")}`);
}

/**
 * Copies the selected rectangle in `format` through the backend formatter (the one export uses), then toasts the
 * outcome: success, the formatter's warnings (values the grid truncated), or the error. Never rejects.
 */
export async function copySelection(format: CopyFormat, input: CopyInput): Promise<void> {
  const { rect, columns } = input;
  if (format === "column_names") return copyColumnNames(columns.slice(rect.left, rect.right + 1));
  const rowCount = rect.bottom - rect.top + 1;
  if (rowCount > COPY_ROW_LIMIT) {
    toast("warning", `Selection too large to copy (${rowCount.toLocaleString()} rows). Use Export instead.`);
    return;
  }
  if (input.dialect === null && DIALECT_FORMATS.has(format)) {
    toast("error", `Connection type unknown: cannot copy as ${COPY_FORMAT_LABELS[format]} yet`);
    return;
  }
  const body: FormatRowsRequest = {
    dialect: input.dialect ?? ANY_DIALECT,
    kind: format,
    columns: columns.slice(rect.left, rect.right + 1).map(({ name, category }) => ({ name, category })),
    rows: cellsOf(input.rows, rect),
    options: {
      header: wantsHeader(input),
      null_text: "",
      table_schema: input.tableName?.schema ?? null,
      table_name: input.tableName?.name ?? null,
    },
  };
  return copyText(formatRows(body), `Copied ${plural(rowCount, "row")} as ${COPY_FORMAT_LABELS[format]}`);
}
