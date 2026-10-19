import type { EditKind } from "@/lib/api/types";
import { BYTES_PREVIEW, plainValue, type EncodedCell } from "@/lib/cells";

/*
 * Staged cell values and the loaded cells they replace (spec ⑤ R-05-8, D-05-10). A staged value is always text as typed:
 * a number's digits never pass through a JS number, so `9007199254740993` and `1.10` reach the backend as they were
 * typed. NULL and the empty string are different values; DEFAULT exists only for a new row's cell.
 */

/** A staged cell value: typed text, NULL, or DEFAULT (a new row's cell left to the column's default). */
export type CellInput = { kind: "value"; text: string } | { kind: "null" } | { kind: "default" };

export const NULL_INPUT: CellInput = { kind: "null" };

/** A loaded cell as the text its editor starts with, and whether that text is the whole value. */
export interface EditText {
  text: string;
  /** False for a cut text (over 64 KiB, a preview only) and for bytes the grid holds only the first 64 of. */
  complete: boolean;
}

/**
 * The text a cell's editor starts with (`plainValue`, ruling R-02b-7, so it is what a single-cell copy gives): NULL is
 * "", bytes are `0x` + hex, a cut text is its preview (not complete: it is never staged from), a bool is "true" /
 * "false".
 */
export function editText(cell: unknown): EditText {
  if (cell === null || cell === undefined) return { text: "", complete: true };
  if (typeof cell === "object") {
    const encoded = cell as EncodedCell;
    if (encoded.$type === "text") return { text: encoded.preview ?? "", complete: false };
    if (encoded.$type === "missing") return { text: "", complete: false };
    if (encoded.$type === "bytes") return { text: plainValue(cell), complete: (encoded.size ?? 0) <= BYTES_PREVIEW };
  }
  return { text: plainValue(cell), complete: true };
}

const asNull = (cell: unknown) => (cell === undefined ? null : cell);

/**
 * Whether two encoded cells hold the same value. A re-read row's cells are new objects, so bytes and cut texts are
 * compared by what they carry; everything else by value.
 */
export function sameCell(a: unknown, b: unknown): boolean {
  const x = asNull(a);
  const y = asNull(b);
  if (x === y) return true;
  if (x === null || y === null || typeof x !== "object" || typeof y !== "object") return false;
  const p = x as EncodedCell;
  const q = y as EncodedCell;
  return p.$type === q.$type && p.preview === q.preview && p.size === q.size && p.text === q.text;
}

/** Whether two staged values are the same. */
export function sameInput(a: CellInput, b: CellInput): boolean {
  if (a.kind !== b.kind) return false;
  return a.kind !== "value" || (b.kind === "value" && a.text === b.text);
}

/**
 * Whether staging `input` over the loaded cell `original` would change nothing: NULL over NULL, or exactly the text the
 * cell's editor starts with over a complete value (spec §3.6.1: there is no no-op UPDATE). DEFAULT never matches.
 */
export function inputMatches(input: CellInput, original: unknown): boolean {
  const cell = asNull(original);
  if (input.kind === "null") return cell === null;
  if (input.kind === "default" || cell === null) return false;
  const { text, complete } = editText(cell);
  return complete && text === input.text;
}

const TRUE_WORDS: ReadonlySet<string> = new Set(["true", "t", "1", "yes"]);
const FALSE_WORDS: ReadonlySet<string> = new Set(["false", "f", "0", "no"]);
/** Hex as the backend reads it (`literals._hex`): an optional 0x, then two digits a byte. */
const HEX = /^(?:0[xX])?((?:[0-9a-fA-F]{2})*)$/;

/**
 * A staged value as the grid draws it (D-05-10), in the encoding of a loaded cell, so copy, the aggregates, the value
 * viewer and the record view need no new cell kind: text stays a string (a number column still aligns it right), a bool
 * spelled as the backend accepts it is a boolean, hex is bytes (`{$type: "bytes", size, preview}`), NULL and DEFAULT are
 * null (the overlay marks DEFAULT). Text the preview will refuse is drawn as typed.
 */
export function displayCell(input: CellInput, kind: EditKind | undefined): unknown {
  if (input.kind !== "value") return null;
  const { text } = input;
  if (kind === "bool") {
    const word = text.trim().toLowerCase();
    if (TRUE_WORDS.has(word)) return true;
    if (FALSE_WORDS.has(word)) return false;
    return text;
  }
  if (kind === "binary") {
    const match = HEX.exec(text.trim());
    if (!match) return text;
    const hex = match[1].toLowerCase();
    return { $type: "bytes", size: hex.length / 2, preview: hex.slice(0, BYTES_PREVIEW * 2) };
  }
  return text;
}
