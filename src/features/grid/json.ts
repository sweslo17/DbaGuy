/**
 * JSON for the value viewer: a parser that keeps what `JSON.parse` loses (number literals beyond double precision,
 * key order, duplicate keys), a pretty-printer over its tree, and JSONPath for a node.
 */

export interface JsonEntry {
  /** Object key, or array index. */
  key: string | number;
  value: JsonValue;
}

export interface JsonContainer {
  kind: "object" | "array";
  children: JsonEntry[];
}

export interface JsonScalar {
  kind: "string" | "number" | "boolean" | "null";
  /** A string's value; for the others the literal as written (e.g. `12345678901234567890`, `1e3`, `true`). */
  text: string;
}

export type JsonValue = JsonContainer | JsonScalar;

export const isContainer = (value: JsonValue): value is JsonContainer =>
  value.kind === "object" || value.kind === "array";

export interface ParsedJson {
  root: JsonValue;
  /** Every value in the document, containers included. */
  nodes: number;
}

/** Deeper documents are shown as text (the parser and the tree recurse). */
const MAX_DEPTH = 500;
const NUMBER = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y;
const LITERALS = [
  ["true", "boolean"],
  ["false", "boolean"],
  ["null", "null"],
] as const;

class SyntaxFailure extends Error {}

class Parser {
  pos = 0;
  nodes = 0;
  constructor(private readonly text: string) {}

  fail(): never {
    throw new SyntaxFailure(`Invalid JSON at ${this.pos}`);
  }

  skipSpace(): void {
    const { text } = this;
    while (this.pos < text.length) {
      const code = text.charCodeAt(this.pos);
      if (code !== 0x20 && code !== 0x0a && code !== 0x0d && code !== 0x09) return;
      this.pos += 1;
    }
  }

  /** Consumes `ch` (after whitespace) when it is next. */
  eat(ch: string): boolean {
    this.skipSpace();
    if (this.text[this.pos] !== ch) return false;
    this.pos += 1;
    return true;
  }

  expect(ch: string): void {
    if (!this.eat(ch)) this.fail();
  }

  /** A string literal: scanned here, decoded (and its escapes checked) by `JSON.parse`. */
  string(): string {
    const { text } = this;
    const start = this.pos;
    if (text[start] !== '"') this.fail();
    for (let i = start + 1; i < text.length; i += 1) {
      const code = text.charCodeAt(i);
      if (code === 0x22) {
        this.pos = i + 1;
        try {
          return JSON.parse(text.slice(start, i + 1)) as string;
        } catch {
          this.fail();
        }
      }
      if (code === 0x5c) i += 1;
      else if (code < 0x20) break;
    }
    this.pos = text.length;
    return this.fail();
  }

  value(depth: number): JsonValue {
    if (depth > MAX_DEPTH) this.fail();
    this.skipSpace();
    this.nodes += 1;
    const { text } = this;
    const ch = text[this.pos];
    if (ch === "{" || ch === "[") return this.container(ch, depth);
    if (ch === '"') return { kind: "string", text: this.string() };
    NUMBER.lastIndex = this.pos;
    const number = NUMBER.exec(text);
    if (number) {
      this.pos += number[0].length;
      return { kind: "number", text: number[0] };
    }
    for (const [literal, kind] of LITERALS) {
      if (text.startsWith(literal, this.pos)) {
        this.pos += literal.length;
        return { kind, text: literal };
      }
    }
    return this.fail();
  }

  container(open: "{" | "[", depth: number): JsonValue {
    const close = open === "{" ? "}" : "]";
    this.pos += 1;
    const children: JsonEntry[] = [];
    if (!this.eat(close)) {
      do {
        let key: string | number = children.length;
        if (open === "{") {
          this.skipSpace();
          key = this.string();
          this.expect(":");
        }
        children.push({ key, value: this.value(depth + 1) });
      } while (this.eat(","));
      this.expect(close);
    }
    return { kind: open === "{" ? "object" : "array", children };
  }
}

/** The document in `text`, or null when it is not valid JSON (or nests deeper than MAX_DEPTH). */
export function parseJson(text: string): ParsedJson | null {
  const parser = new Parser(text);
  try {
    const root = parser.value(0);
    parser.skipSpace();
    if (parser.pos !== text.length) return null;
    return { root, nodes: parser.nodes };
  } catch (error) {
    if (error instanceof SyntaxFailure) return null;
    throw error;
  }
}

/** Pretty-printed like `JSON.stringify(value, null, 2)`, number literals kept as written. */
export function formatJson(value: JsonValue): string {
  const out: string[] = [];
  const write = (node: JsonValue, indent: string) => {
    if (!isContainer(node)) {
      out.push(node.kind === "string" ? JSON.stringify(node.text) : node.text);
      return;
    }
    const [open, close] = node.kind === "object" ? ["{", "}"] : ["[", "]"];
    if (node.children.length === 0) {
      out.push(open + close);
      return;
    }
    const inner = `${indent}  `;
    out.push(open);
    node.children.forEach((entry, i) => {
      out.push(i === 0 ? "\n" : ",\n", inner);
      if (node.kind === "object") out.push(JSON.stringify(entry.key), ": ");
      write(entry.value, inner);
    });
    out.push("\n", indent, close);
  };
  write(value, "");
  return out.join("");
}

const IDENT = /^[A-Za-z_$][A-Za-z0-9_$]*$/;

/** JSONPath of a node from its keys and indexes: `$.a[0]["b c"]`. */
export function jsonPath(segments: (string | number)[]): string {
  return (
    "$" +
    segments
      .map((s) => (typeof s === "number" ? `[${s}]` : IDENT.test(s) ? `.${s}` : `[${JSON.stringify(s)}]`))
      .join("")
  );
}
