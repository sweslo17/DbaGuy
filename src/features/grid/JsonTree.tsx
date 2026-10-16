import { ChevronRight } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/cn";
import { plural } from "@/lib/format";
import { isContainer, jsonPath, type JsonContainer, type JsonScalar, type JsonValue, type ParsedJson } from "./json";

/** Documents with more values than this open with only the root expanded. */
const BIG_TREE = 2000;
/** An expanded container renders this many children at a time ("Show more" adds the next ones). */
const CHILD_PAGE = 200;
const INDENT_PX = 14;

const SCALAR_CLASS: Record<JsonScalar["kind"], string> = {
  string: "text-success",
  number: "text-accent",
  boolean: "text-accent",
  null: "italic text-muted",
};

function Scalar({ value }: { value: JsonScalar }) {
  return (
    <span className={cn("break-all whitespace-pre-wrap", SCALAR_CLASS[value.kind])}>
      {value.kind === "string" ? `"${value.text}"` : value.text}
    </span>
  );
}

function summary(value: JsonContainer): string {
  const count = value.children.length;
  return value.kind === "object" ? `{${plural(count, "key")}}` : `[${plural(count, "item")}]`;
}

interface JsonNodeProps {
  /** Key or index in the parent; undefined for the root. */
  name?: string | number;
  value: JsonValue;
  path: (string | number)[];
  depth: number;
  /** Containers shallower than this start expanded. */
  openDepth: number;
  onCopyPath: (path: string) => void;
}

/**
 * One value and, when it is an expanded container, its children (rendered only while expanded, a page at a time).
 * Clicking a key copies its JSONPath.
 */
function JsonNode({ name, value, path, depth, openDepth, onCopyPath }: JsonNodeProps) {
  const container = isContainer(value) && value.children.length > 0 ? value : null;
  const [open, setOpen] = useState(depth < openDepth);
  const [shown, setShown] = useState(CHILD_PAGE);
  const indent = { paddingLeft: depth * INDENT_PX };
  const key =
    name === undefined ? null : (
      <>
        <button
          type="button"
          className={cn("rounded-sm hover:bg-hover", typeof name === "number" ? "text-muted" : "text-fg")}
          title={`Copy path ${jsonPath(path)}`}
          onClick={() => onCopyPath(jsonPath(path))}
        >
          {name === "" ? '""' : name}
        </button>
        <span className="text-muted">: </span>
      </>
    );

  if (!container) {
    return (
      <div className="flex" style={indent}>
        <span className="w-4 shrink-0" />
        <span className="min-w-0">
          {key}
          {isContainer(value) ? <span className="text-muted">{summary(value)}</span> : <Scalar value={value} />}
        </span>
      </div>
    );
  }
  const children = container.children;
  return (
    <>
      <div className="flex" style={indent}>
        <button
          type="button"
          className="flex w-4 shrink-0 items-center text-muted"
          aria-expanded={open}
          aria-label={open ? "Collapse" : "Expand"}
          onClick={() => setOpen(!open)}
        >
          <ChevronRight size={12} className={cn("transition-transform", open && "rotate-90")} />
        </button>
        <span className="min-w-0">
          {key}
          <button type="button" className="text-muted" onClick={() => setOpen(!open)}>
            {summary(container)}
          </button>
        </span>
      </div>
      {open &&
        children
          .slice(0, shown)
          .map((entry, i) => (
            <JsonNode
              key={i}
              name={entry.key}
              value={entry.value}
              path={[...path, entry.key]}
              depth={depth + 1}
              openDepth={openDepth}
              onCopyPath={onCopyPath}
            />
          ))}
      {open && children.length > shown && (
        <div style={{ paddingLeft: (depth + 1) * INDENT_PX + 16 }}>
          <button
            type="button"
            className="font-sans text-accent hover:underline"
            onClick={() => setShown(shown + CHILD_PAGE)}
          >
            Show {Math.min(CHILD_PAGE, children.length - shown).toLocaleString()} more (
            {(children.length - shown).toLocaleString()} left)
          </button>
        </div>
      )}
    </>
  );
}

/**
 * A collapsible JSON tree. Containers start expanded down to depth 2 (only the root for documents of more than
 * BIG_TREE values); collapsed children are not rendered. Clicking a key calls `onCopyPath` with its JSONPath.
 */
export function JsonTree({ value, onCopyPath }: { value: ParsedJson; onCopyPath: (path: string) => void }) {
  return (
    <div className="p-2 font-mono text-[12px] leading-5" data-selectable>
      <JsonNode
        value={value.root}
        path={[]}
        depth={0}
        openDepth={value.nodes > BIG_TREE ? 1 : 2}
        onCopyPath={onCopyPath}
      />
    </div>
  );
}
