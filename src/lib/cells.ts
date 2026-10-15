export type CellKind = "null" | "bytes" | "truncated" | "plain";

/** Shape of the objects the backend sends for cells that do not fit a JSON scalar (see adapters/values.py). */
interface EncodedCell {
  $type?: string;
  preview?: string;
  size?: number;
}

const BYTES_PREVIEW = 64; // keep in sync with BYTES_PREVIEW in the backend

/** Display text for an encoded cell. */
export function formatCell(value: unknown): { text: string; kind: CellKind } {
  if (value === null || value === undefined) return { text: "NULL", kind: "null" };
  if (typeof value === "object") {
    const encoded = value as EncodedCell;
    if (encoded.$type === "bytes") {
      return { text: `0x${encoded.preview ?? ""}${(encoded.size ?? 0) > BYTES_PREVIEW ? "…" : ""}`, kind: "bytes" };
    }
    if (encoded.$type === "text") return { text: `${encoded.preview ?? ""}…`, kind: "truncated" };
    return { text: JSON.stringify(value), kind: "plain" };
  }
  return { text: String(value), kind: "plain" };
}
