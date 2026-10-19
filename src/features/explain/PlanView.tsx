import { useVirtualizer } from "@tanstack/react-virtual";
import { Info, Lightbulb, Loader2, TriangleAlert } from "lucide-react";
import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { CopyButton } from "@/components/ui/CopyButton";
import type { IndexAdvice, PlanNode, PlanView as PlanViewData } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import {
  BADGE_LABELS,
  adviceGroups,
  badgeNotes,
  estimateRows,
  estimateText,
  planRows,
  statRows,
  statsText,
  type PlanBadge,
  type PlanRows,
} from "./planModel";

/** Asking for index advice on a plan or profile: what the view needs to show the button and its outcome. */
export interface AdviceControl {
  /** The server can give advice (not Community Edition); otherwise `reason` says why the button is disabled. */
  available: boolean;
  reason?: string;
  pending: boolean;
  /** The server's advice for the plan's statement, once asked for. */
  result: PlanViewData | null;
  /** The server's own text when it refused (e.g. 5001 on a collection holding binary documents). */
  error: string | null;
  request: () => void;
}

const ROW_HEIGHT = 24;
const ROW_OVERSCAN = 12;
/** The height the tree is taken to have until the pane is measured, so its first frame already has rows. */
const FIRST_FRAME_HEIGHT = 480;
/** An operator's indent per level, and the level where it stops growing (a deep tree stays inside the pane). */
const INDENT = 14;
const MAX_INDENT_LEVELS = 30;
const STATEMENT_TOOLTIP_CHARS = 2000;

const KIND_LABELS: Record<PlanViewData["kind"], string> = {
  plan: "Plan",
  profile: "Profile",
  advice: "Advice",
  unknown: "Unknown",
};

const BADGE_STYLES: Record<PlanBadge, string> = {
  primary_scan: "border-warning/50 text-warning",
  sequential_scan: "border-warning/50 text-warning",
  intersect_scan: "border-warning/50 text-warning",
  not_covered: "border-border text-muted",
  slowest: "border-accent/50 text-accent",
};

/** A line of text that stands in for the view (nothing to draw, an error, loading). */
export function PlanNotice({ children, tone = "muted" }: { children: ReactNode; tone?: "muted" | "danger" }) {
  return (
    <div
      role={tone === "danger" ? "alert" : undefined}
      data-selectable
      className={cn(
        "flex h-full items-center justify-center p-4 text-center text-[12px] whitespace-pre-wrap",
        tone === "danger" ? "text-danger" : "text-muted",
      )}
    >
      {children}
    </div>
  );
}

function Badge({ badge, note }: { badge: PlanBadge; note?: string }) {
  return (
    <span
      className={cn("shrink-0 rounded border px-1 text-[10px] leading-4", BADGE_STYLES[badge])}
      title={note ?? BADGE_LABELS[badge]}
    >
      {BADGE_LABELS[badge]}
    </span>
  );
}

const ROW_BUTTON =
  "flex h-full w-full items-center gap-2 pr-3 text-left text-[12px] outline-none focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:ring-inset";

/** One operator: its title, badges, the planner's estimates and, in a profile, its items and times. */
const NodeRow = memo(function NodeRow({
  node,
  depth,
  selected,
  notes,
  onSelect,
}: {
  node: PlanNode;
  depth: number;
  selected: boolean;
  notes: ReadonlyMap<string, string>;
  onSelect: (id: number) => void;
}) {
  const estimates = estimateText(node.estimates);
  const stats = statsText(node.stats);
  return (
    <button
      type="button"
      aria-current={selected || undefined}
      onClick={() => onSelect(node.id)}
      className={cn(ROW_BUTTON, selected ? "bg-selected" : "hover:bg-hover")}
      style={{ paddingLeft: 12 + Math.min(depth, MAX_INDENT_LEVELS) * INDENT }}
    >
      <span className="min-w-0 truncate" title={node.title}>
        {node.title}
      </span>
      {node.badges.map((badge) => (
        <Badge key={badge} badge={badge} note={notes.get(`${node.id}:${badge}`)} />
      ))}
      {(estimates || stats) && (
        <span className="ml-auto flex shrink-0 gap-3 pl-2 text-muted tabular-nums">
          {estimates && <span title="Estimated by the planner: cost · rows">{estimates}</span>}
          {stats && <span title="Items passed on · exec time + serv time">{stats}</span>}
        </span>
      )}
    </button>
  );
});

/** The heading of a subquery's tree. */
function SubqueryRow({ text, correlated }: { text: string; correlated: boolean }) {
  return (
    <div className="flex h-full items-center gap-2 border-t border-border px-3 text-[12px]">
      <span className="min-w-0 truncate" title={text.slice(0, STATEMENT_TOOLTIP_CHARS)}>
        <span className="text-muted">Subquery: </span>
        <span className="font-mono">{text}</span>
      </span>
      {correlated && (
        <span className="shrink-0 rounded border border-border px-1 text-[10px] leading-4 text-muted">correlated</span>
      )}
    </div>
  );
}

/**
 * The operator tree as indented rows, drawn only where it is in view (a plan may have thousands of operators). `reveal`
 * scrolls an operator into view when it changes (a finding was clicked).
 */
const TreeList = memo(function TreeList({
  model,
  notes,
  selectedId,
  reveal,
  onSelect,
}: {
  model: PlanRows;
  notes: ReadonlyMap<string, string>;
  selectedId: number | null;
  reveal: { id: number } | null;
  onSelect: (id: number) => void;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: model.rows.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: ROW_OVERSCAN,
    initialRect: { width: 0, height: FIRST_FRAME_HEIGHT },
  });
  useEffect(() => {
    const at = reveal ? model.index.get(reveal.id) : undefined;
    if (at !== undefined) virtualizer.scrollToIndex(at);
  }, [reveal, model, virtualizer]);
  return (
    <div ref={scroller} role="group" aria-label="Operators" className="min-h-0 min-w-0 flex-1 overflow-y-auto">
      <div className="relative" style={{ height: virtualizer.getTotalSize() }}>
        {virtualizer.getVirtualItems().map((item) => {
          const row = model.rows[item.index];
          return (
            <div
              key={item.key}
              className="absolute inset-x-0 top-0"
              style={{ height: ROW_HEIGHT, transform: `translateY(${item.start}px)` }}
            >
              {row.kind === "node" ? (
                <NodeRow
                  node={row.node}
                  depth={row.depth}
                  selected={row.node.id === selectedId}
                  notes={notes}
                  onSelect={onSelect}
                />
              ) : (
                <SubqueryRow text={row.text} correlated={row.correlated} />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
});

function DetailSection({ title, rows }: { title: string; rows: [string, string][] }) {
  if (rows.length === 0) return null;
  return (
    <section className="mt-3">
      <h4 className="mb-1 text-[11px] font-medium tracking-wide text-muted uppercase">{title}</h4>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
        {rows.map(([label, value], index) => (
          <Fragment key={index}>
            <dt className="text-muted">{label}</dt>
            <dd className="m-0 font-mono break-words whitespace-pre-wrap">{value}</dd>
          </Fragment>
        ))}
      </dl>
    </section>
  );
}

/** All the plan says of the selected operator: its details, estimates and statistics. */
function NodeDetails({ node }: { node: PlanNode | null }) {
  const details = node?.details.map((detail): [string, string] => [detail.label, detail.value]) ?? [];
  const estimates = estimateRows(node?.estimates);
  const stats = statRows(node?.stats);
  const empty = details.length + estimates.length + stats.length === 0;
  return (
    <aside
      aria-label="Operator details"
      data-selectable
      className="w-72 shrink-0 overflow-y-auto border-l border-border p-3 text-[12px]"
    >
      {node === null ? (
        <p className="text-muted">Select an operator to see its details</p>
      ) : (
        <>
          <h3 className="font-medium break-words">{node.title}</h3>
          {node.operator !== node.title && <p className="text-muted">{node.operator}</p>}
          {empty && <p className="mt-3 text-muted">This operator has no details</p>}
          <DetailSection title="Details" rows={details} />
          <DetailSection title="Estimates" rows={estimates} />
          <DetailSection title="Statistics" rows={stats} />
        </>
      )}
    </aside>
  );
}

/** The warnings and notes the analysis found; a click shows the operator they are about. */
function Findings({ findings, onSelect }: { findings: PlanViewData["findings"]; onSelect: (id: number) => void }) {
  if (findings.length === 0) return null;
  return (
    <ul className="flex flex-col px-1 pb-1">
      {findings.map((finding, index) => (
        <li key={index}>
          <button
            type="button"
            title="Show the operator"
            onClick={() => onSelect(finding.node_id)}
            className="flex w-full items-start gap-2 rounded px-2 py-1 text-left text-[12px] outline-none hover:bg-hover focus-visible:ring-2 focus-visible:ring-accent/50"
          >
            {finding.severity === "warning" ? (
              <TriangleAlert size={13} className="mt-px shrink-0 text-warning" aria-label="Warning" />
            ) : (
              <Info size={13} className="mt-px shrink-0 text-muted" aria-label="Note" />
            )}
            <span className="min-w-0 break-words">{finding.text}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

/** A statement to copy: in monospace, its Copy button beside it. */
function CopyBlock({ text, label }: { text: string; label?: string }) {
  return (
    <div className="flex items-start gap-2">
      <pre className="m-0 min-w-0 flex-1 font-mono text-[12px] break-all whitespace-pre-wrap">{text}</pre>
      <CopyButton text={text} label={label} />
    </div>
  );
}

function AdviceItem({ entry }: { entry: IndexAdvice }) {
  return (
    <div className="rounded-md border border-border bg-panel p-2">
      <CopyBlock text={entry.statement} />
      {entry.rule && <p className="mt-1 text-[11px] text-muted">{entry.rule}</p>}
      {entry.update_statistics && (
        <div className="mt-2 border-t border-border pt-2">
          <p className="mb-1 text-[11px] text-muted">Update statistics</p>
          <CopyBlock text={entry.update_statistics} />
        </div>
      )}
    </div>
  );
}

/** ADVISE's answer: the current, the recommended and the covering indexes, each statement to copy and run as it is. */
function AdviceSection({ view }: { view: PlanViewData }) {
  const groups = adviceGroups(view.advice);
  return (
    <section aria-label="Index advice" className="border-t border-border px-3 py-2">
      <h3 className="mb-1 text-[11px] font-medium tracking-wide text-muted uppercase">Index advice</h3>
      {view.advice_note && <p className="text-[12px] text-muted">{view.advice_note}</p>}
      {groups.length === 0 && !view.advice_note && <p className="text-[12px] text-muted">The server gave no advice</p>}
      {groups.map((group) => (
        <div key={group.kind} className="mt-2">
          <p className="mb-1 text-[12px] font-medium">{group.title}</p>
          <div className="flex flex-col gap-2">
            {group.items.map((entry, index) => (
              <AdviceItem key={index} entry={entry} />
            ))}
          </div>
        </div>
      ))}
    </section>
  );
}

/** What the plan is of: its kind, the planner's cost for the whole of it, the statement, a profile's phase times. */
function Header({ view, advice }: { view: PlanViewData; advice?: AdviceControl }) {
  const cost = estimateText(view.estimates);
  const statement = view.statement_text;
  const phases = view.phase_times.map((phase) => `${phase.label} ${phase.value}`).join(" · ");
  const canAdvise = advice && (view.kind === "plan" || view.kind === "profile");
  return (
    <div className="px-3 py-2">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-[12px]">
            <span className="rounded bg-hover px-1.5 text-[11px] font-medium">{KIND_LABELS[view.kind]}</span>
            {cost && (
              <span className="text-muted tabular-nums" title="Estimated by the planner for the whole plan">
                {cost}
              </span>
            )}
          </div>
          {statement && (
            <p
              data-selectable
              title={statement.slice(0, STATEMENT_TOOLTIP_CHARS)}
              className="mt-1 line-clamp-3 font-mono text-[12px] break-words whitespace-pre-wrap"
            >
              {statement}
            </p>
          )}
          {phases && (
            <p className="mt-1 truncate text-[11px] text-muted" title={phases}>
              {phases}
            </p>
          )}
        </div>
        {canAdvise && (
          <Button
            size="sm"
            className="shrink-0"
            disabled={!advice.available || advice.pending}
            title={advice.available ? "Ask the server which indexes this statement would use" : advice.reason}
            onClick={advice.request}
          >
            {advice.pending ? <Loader2 size={12} className="animate-spin" /> : <Lightbulb size={12} />} Get index advice
          </Button>
        )}
      </div>
      {canAdvise && advice.error && (
        <p role="alert" data-selectable className="mt-2 text-[12px] break-words whitespace-pre-wrap text-danger">
          {advice.error}
        </p>
      )}
    </div>
  );
}

function PlanBody({ view, advice }: { view: PlanViewData; advice?: AdviceControl }) {
  const model = useMemo(() => planRows(view), [view]);
  const notes = useMemo(() => badgeNotes(view.findings), [view]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [reveal, setReveal] = useState<{ id: number } | null>(null);
  const select = useCallback((id: number) => setSelectedId(id), []);
  const showFinding = useCallback((id: number) => {
    setSelectedId(id);
    setReveal({ id });
  }, []);
  const hasTree = model.rows.length > 0;
  const selected = selectedId === null ? null : (model.nodes.get(selectedId) ?? null);
  const advised = view.kind === "advice" ? view : (advice?.result ?? null);
  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* A tall top part (long advice) scrolls on its own: the tree keeps at least half of the pane. */}
      <div
        className={cn("overflow-y-auto", hasTree ? "max-h-[45%] shrink-0 border-b border-border" : "min-h-0 flex-1")}
      >
        <Header view={view} advice={advice} />
        <Findings findings={view.findings} onSelect={showFinding} />
        {advised && <AdviceSection view={advised} />}
      </div>
      {hasTree && (
        <div className="flex min-h-0 flex-1">
          <TreeList model={model} notes={notes} selectedId={selectedId} reveal={reveal} onSelect={select} />
          <NodeDetails node={selected} />
        </div>
      )}
    </div>
  );
}

/**
 * An EXPLAIN, ADVISE or profile answer (spec §3.10): what it is of, the findings, the operator tree with each
 * operator's badges and numbers, the subqueries' trees, and the selected operator's details; or the index advice. The
 * view is the backend's analysis (`POST /couchbase/plan`): nothing here reads the plan's JSON. `advice` adds the
 * "Get index advice" button of a plan or profile, and shows its outcome.
 */
export const PlanView = memo(function PlanView({ view, advice }: { view: PlanViewData; advice?: AdviceControl }) {
  if (view.kind === "unknown") {
    return <PlanNotice>Nothing to draw: switch to Grid to see the server's text</PlanNotice>;
  }
  return <PlanBody view={view} advice={advice} />;
});
