import type { ColumnCategory } from "@/lib/api/types";

export type CellKind = "null" | "empty" | "number" | "bool" | "json" | "bytes" | "truncated" | "text";

/** How one encoded cell is shown: `text`, then `detail` (muted) when there is one. */
export interface CellDisplay {
  text: string;
  kind: CellKind;
  /** Muted suffix after the text, e.g. the size of a bytes or truncated value. */
  detail?: string;
  title?: string;
  alignRight: boolean;
}

/** Shape of the objects the backend sends for cells that do not fit a JSON scalar (see adapters/values.py). */
export interface EncodedCell {
  $type?: string;
  preview?: string;
  size?: number;
}

export const BYTES_PREVIEW = 64; // keep in sync with BYTES_PREVIEW in the backend (adapters/values.py)

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"];
const JSON_WHITESPACE = new Set([" ", "\n", "\r", "\t"]);
/** Characters a JSON value can start / end with (string, object, array, number, true / false / null). */
const VALUE_START = new Set('"{[-0123456789tfn');
const VALUE_END = new Set('"}]0123456789el');

/** "512 B", "1.2 KB", "3.0 MB": base 1024, one decimal. */
export function formatBytes(size: number): string {
  let value = size;
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${size} B` : `${value.toFixed(1)} ${BYTE_UNITS[unit]}`;
}

/** At most `max` UTF-16 units, never ending in half a surrogate pair. */
function clip(text: string, max: number): string {
  if (text.length <= max) return text;
  const code = text.charCodeAt(max - 1);
  return text.slice(0, code >= 0xd800 && code <= 0xdbff ? max - 1 : max);
}

/**
 * JSON text without the whitespace between tokens, stopping after `max` characters. Lexical on purpose: parsing and
 * re-serialising would round big numbers.
 */
function compactJson(text: string, max: number): string {
  let out = "";
  let inString = false;
  let escaped = false;
  for (let i = 0; i < text.length && out.length < max; i += 1) {
    const ch = text[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
    } else if (ch === '"') {
      inString = true;
    } else if (JSON_WHITESPACE.has(ch)) {
      continue;
    }
    out += ch;
  }
  return out;
}

/** Index of the first non-whitespace character at or after `from` (`step` 1) or at or before it (`step` -1). */
function skipSpace(text: string, from: number, step: 1 | -1): number {
  let i = from;
  while (i >= 0 && i < text.length && JSON_WHITESPACE.has(text[i])) i += step;
  return i;
}

/**
 * Text that looks like a JSON object or array (e.g. JSON kept in a text column), judged by the two non-space
 * characters at each end only: it runs for every rendered cell, so it never parses. The value viewer parses.
 */
export function looksLikeJson(text: string): boolean {
  const start = skipSpace(text, 0, 1);
  const end = skipSpace(text, text.length - 1, -1);
  if (end <= start) return false;
  const open = text[start];
  if (!((open === "{" && text[end] === "}") || (open === "[" && text[end] === "]"))) return false;
  const first = skipSpace(text, start + 1, 1);
  if (first === end) return true; // {} or []
  const last = skipSpace(text, end - 1, -1);
  return (open === "{" ? text[first] === '"' : VALUE_START.has(text[first])) && VALUE_END.has(text[last]);
}

function describeBytes(cell: EncodedCell, maxLength: number): CellDisplay {
  const size = cell.size ?? 0;
  return {
    text: `0x${clip(cell.preview ?? "", maxLength)}${size > BYTES_PREVIEW ? "…" : ""}`,
    detail: ` (${formatBytes(size)})`,
    title: `${size.toLocaleString()} bytes`,
    kind: "bytes",
    alignRight: false,
  };
}

function describeTruncated(cell: EncodedCell, maxLength: number): CellDisplay {
  const size = cell.size ?? 0;
  return {
    text: `${clip(cell.preview ?? "", maxLength)}…`,
    detail: ` (${size.toLocaleString()} chars)`,
    title: `Truncated: ${size.toLocaleString()} characters in total`,
    kind: "truncated",
    alignRight: false,
  };
}

/** The kind of an encoded cell (see adapters/values.py): from the value and its column's category alone. */
export function cellKind(value: unknown, category: ColumnCategory): CellKind {
  if (value === null || value === undefined) return "null";
  if (typeof value === "boolean") return "bool";
  if (typeof value === "number") return "number";
  if (typeof value === "object") {
    const type = (value as EncodedCell).$type;
    return type === "bytes" ? "bytes" : type === "text" ? "truncated" : "json";
  }
  const text = String(value);
  if (text === "") return "empty";
  // Big integers and decimals arrive as strings so that no digit is lost.
  if (category === "number") return "number";
  if (category === "json" || ((category === "text" || category === "other") && looksLikeJson(text))) return "json";
  return "text";
}

/**
 * Display of an encoded cell (see adapters/values.py). `maxLength` caps the text for callers that show one line of
 * it (a grid cell, a width estimate); the full text is returned without it.
 */
export function describeCell(value: unknown, category: ColumnCategory, maxLength = Infinity): CellDisplay {
  const kind = cellKind(value, category);
  switch (kind) {
    case "null":
      return { text: "NULL", kind, alignRight: false };
    case "empty":
      return { text: "", kind, title: "empty string", alignRight: false };
    case "bool":
      return { text: String(value), kind, alignRight: false };
    case "number":
      return { text: clip(String(value), maxLength), kind, alignRight: true };
    case "bytes":
      return describeBytes(value as EncodedCell, maxLength);
    case "truncated":
      return describeTruncated(value as EncodedCell, maxLength);
    case "json": {
      const text = typeof value === "string" ? compactJson(value, maxLength) : clip(JSON.stringify(value), maxLength);
      return { text, kind, alignRight: false };
    }
    case "text":
      return { text: clip(String(value), maxLength), kind, alignRight: false };
  }
}

/**
 * A cell as plain text (ruling R-02b-7): what a single-cell copy and the value viewer's "Copy value" write. NULL is
 * empty, bytes are `0x` + the hex preview, truncated text is its preview, anything else is its text.
 */
export function plainValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value !== "object") return String(value);
  const cell = value as EncodedCell;
  if (cell.$type === "bytes") return `0x${cell.preview ?? ""}`;
  if (cell.$type === "text") return cell.preview ?? "";
  return JSON.stringify(value);
}
