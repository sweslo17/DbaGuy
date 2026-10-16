import { Copy } from "lucide-react";
import { useMemo, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import type { ColumnMeta } from "@/lib/api/types";
import { cellKind, formatBytes, plainValue, type EncodedCell } from "@/lib/cells";
import { plural } from "@/lib/format";
import { copyPlainText, copyValue } from "./copy";
import { DrawerFrame } from "./DrawerFrame";
import { formatJson, parseJson, type ParsedJson } from "./json";
import { JsonTree } from "./JsonTree";

/** What the viewer shows for one cell; computed once per value (the JSON parse included). */
type ValueView =
  | { kind: "null" }
  | { kind: "scalar"; text: string; type: "boolean" | "number" }
  | { kind: "text"; text: string; chars: number }
  | { kind: "truncated"; text: string; chars: number; size: number }
  | { kind: "json"; json: ParsedJson; chars: number }
  | { kind: "bytes"; hex: string; size: number };

const BYTES_PER_LINE = 16;

/** Length in characters (code points), as the backend counts a truncated value's size. */
const charCount = (text: string) => Array.from(text).length;

function textView(text: string): ValueView {
  return { kind: "text", text, chars: charCount(text) };
}

function viewOf(value: unknown, column: ColumnMeta): ValueView {
  const kind = cellKind(value, column.category);
  switch (kind) {
    case "null":
      return { kind: "null" };
    case "bool":
      return { kind: "scalar", text: plainValue(value), type: "boolean" };
    case "number":
      return { kind: "scalar", text: plainValue(value), type: "number" };
    case "bytes": {
      const cell = value as EncodedCell;
      return { kind: "bytes", hex: cell.preview ?? "", size: cell.size ?? 0 };
    }
    case "truncated": {
      const text = plainValue(value);
      return { kind: "truncated", text, chars: charCount(text), size: (value as EncodedCell).size ?? 0 };
    }
    case "json": {
      const text = plainValue(value);
      const json = parseJson(text);
      return json ? { kind: "json", json, chars: charCount(text) } : textView(text);
    }
    case "empty":
    case "text":
      return textView(plainValue(value));
  }
}

/** `00000010  de ad be ef …  ..ascii..`: offset, 16 bytes in hex, then the bytes as ASCII ('.' if not printable). */
function hexDump(hex: string): string {
  const count = Math.floor(hex.length / 2);
  const lines: string[] = [];
  for (let offset = 0; offset < count; offset += BYTES_PER_LINE) {
    const pairs: string[] = [];
    let ascii = "";
    for (let i = offset; i < Math.min(offset + BYTES_PER_LINE, count); i += 1) {
      const pair = hex.slice(i * 2, i * 2 + 2);
      const code = parseInt(pair, 16);
      pairs.push(pair);
      ascii += code >= 0x20 && code < 0x7f ? String.fromCharCode(code) : ".";
    }
    const offsetText = offset.toString(16).padStart(8, "0");
    lines.push(`${offsetText}  ${pairs.join(" ").padEnd(BYTES_PER_LINE * 3 - 1)}  ${ascii}`);
  }
  return lines.join("\n");
}

/** Wrapping monospace text; the hex dump keeps its lines (HEX). */
const PRE = "p-3 font-mono text-[12px] break-words whitespace-pre-wrap";
const HEX = "p-3 font-mono text-[12px] whitespace-pre";

function Note({ children }: { children: ReactNode }) {
  return <p className="mx-3 mt-3 rounded-md border border-border bg-panel px-2 py-1.5 text-muted">{children}</p>;
}

function ValueBody({ view, onCopyPath }: { view: ValueView; onCopyPath: (path: string) => void }) {
  switch (view.kind) {
    case "null":
      return <p className="p-3 font-mono text-[12px] text-muted italic">NULL</p>;
    case "scalar":
    case "text":
      return (
        <pre className={PRE} data-selectable>
          {view.text}
        </pre>
      );
    case "truncated":
      return (
        <>
          <Note>
            Showing the first {view.chars.toLocaleString()} of {view.size.toLocaleString()} characters — export the
            result for the full value.
          </Note>
          <pre className={PRE} data-selectable>
            {view.text}
          </pre>
        </>
      );
    case "json":
      return <JsonTree value={view.json} onCopyPath={onCopyPath} />;
    case "bytes": {
      const shown = Math.floor(view.hex.length / 2);
      return (
        <>
          {shown < view.size && (
            <Note>
              Showing the first {shown.toLocaleString()} of {view.size.toLocaleString()} bytes — export the result for
              the full value.
            </Note>
          )}
          <pre className={HEX} data-selectable>
            {view.size === 0 ? "(empty)" : hexDump(view.hex)}
          </pre>
        </>
      );
    }
  }
}

/** The size line of the toolbar. */
function describeSize(view: ValueView, column: ColumnMeta): string {
  switch (view.kind) {
    case "null":
      return `NULL · ${column.type_name}`;
    case "scalar":
      return `${view.type} · ${column.type_name}`;
    case "text":
      return view.chars === 0 ? "Empty string" : plural(view.chars, "character");
    case "truncated":
      return `${plural(view.size, "character")} (truncated)`;
    case "json":
      return `JSON · ${plural(view.chars, "character")}`;
    case "bytes":
      return `${plural(view.size, "byte")}${view.size >= 1024 ? ` (${formatBytes(view.size)})` : ""}`;
  }
}

export interface ValueViewerProps {
  value: unknown;
  column: ColumnMeta;
  onClose: () => void;
}

/**
 * The value viewer drawer for one cell: text (wrapping), a JSON tree, a hex dump, or a scalar with its type.
 * "Copy value" copies what a single-cell ⌘C copies (`plainValue`).
 */
export function ValueViewer({ value, column, onClose }: ValueViewerProps) {
  const view = useMemo(() => viewOf(value, column), [value, column]);
  const copyPath = (path: string) => void copyPlainText(path, `Copied path ${path}`);
  return (
    <DrawerFrame
      label="Value viewer"
      title={
        <>
          <span className="truncate font-mono font-semibold" data-selectable>
            {column.name}
          </span>
          <span className="shrink-0 text-[11px] text-muted">{column.type_name}</span>
        </>
      }
      toolbar={
        <>
          <span className="text-muted">{describeSize(view, column)}</span>
          {/* The buttons wrap together, to the right. */}
          <span className="ml-auto flex gap-1">
            {view.kind === "json" && (
              <Button size="sm" onClick={() => void copyPlainText(formatJson(view.json.root), "Copied JSON")}>
                <Copy size={12} /> Copy JSON
              </Button>
            )}
            <Button size="sm" onClick={() => void copyValue(value)}>
              <Copy size={12} /> Copy value
            </Button>
          </span>
        </>
      }
      onClose={onClose}
    >
      <ValueBody view={view} onCopyPath={copyPath} />
    </DrawerFrame>
  );
}
