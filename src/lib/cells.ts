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
interface EncodedCell {
  $type?: string;
  preview?: string;
  size?: number;
}

export const BYTES_PREVIEW = 64; // keep in sync with BYTES_PREVIEW in the backend (adapters/values.py)

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"];
const JSON_WHITESPACE = new Set([" ", "\n", "\r", "\t"]);

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

/** An object or array written as text (e.g. JSON kept in a text column). */
function isJsonText(text: string): boolean {
  const trimmed = text.trim();
  const pair = trimmed[0] + trimmed[trimmed.length - 1];
  if (pair !== "{}" && pair !== "[]") return false;
  try {
    JSON.parse(trimmed);
    return true;
  } catch {
    return false;
  }
}

function describeEncoded(cell: EncodedCell, maxLength: number): CellDisplay {
  const size = cell.size ?? 0;
  const preview = cell.preview ?? "";
  if (cell.$type === "bytes") {
    return {
      text: `0x${clip(preview, maxLength)}${size > BYTES_PREVIEW ? "…" : ""}`,
      detail: ` (${formatBytes(size)})`,
      title: `${size.toLocaleString()} bytes`,
      kind: "bytes",
      alignRight: false,
    };
  }
  if (cell.$type === "text") {
    return {
      text: `${clip(preview, maxLength)}…`,
      detail: ` (${size.toLocaleString()} chars)`,
      title: `Truncated: ${size.toLocaleString()} characters in total`,
      kind: "truncated",
      alignRight: false,
    };
  }
  return { text: clip(JSON.stringify(cell), maxLength), kind: "json", alignRight: false };
}

/**
 * Display of an encoded cell (see adapters/values.py). `maxLength` caps the text for callers that show one line of
 * it (a grid cell, a width estimate); the full text is returned without it.
 */
export function describeCell(value: unknown, category: ColumnCategory, maxLength = Infinity): CellDisplay {
  if (value === null || value === undefined) return { text: "NULL", kind: "null", alignRight: false };
  if (typeof value === "boolean") return { text: String(value), kind: "bool", alignRight: false };
  if (typeof value === "number") return { text: String(value), kind: "number", alignRight: true };
  if (typeof value === "object") return describeEncoded(value as EncodedCell, maxLength);
  const text = String(value);
  if (text === "") return { text, kind: "empty", title: "empty string", alignRight: false };
  // Big integers and decimals arrive as strings so that no digit is lost.
  if (category === "number") return { text: clip(text, maxLength), kind: "number", alignRight: true };
  if (category === "json" || ((category === "text" || category === "other") && isJsonText(text))) {
    return { text: compactJson(text, maxLength), kind: "json", alignRight: false };
  }
  return { text: clip(text, maxLength), kind: "text", alignRight: false };
}
