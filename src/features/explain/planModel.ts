import type { IndexAdvice, PlanFinding, PlanNode, PlanView } from "@/lib/api/types";
import { jsonCellText, type EncodedCell } from "@/lib/cells";
import type { ExecutionState } from "../session/execution";
import { isPlanRequest, withoutExplain } from "./explainText";

/*
 * What the plan view draws, as plain data (spec §3.10): the operator tree flattened into rows, the texts of a node's
 * numbers, the grouping of the index advice, and what a result set's one cell holds. Nothing here touches React, so
 * it can be checked without a DOM.
 */

export type PlanBadge = PlanNode["badges"][number];
type PlanStats = NonNullable<PlanNode["stats"]>;
type PlanEstimates = NonNullable<PlanNode["estimates"]>;

/** The badges' words, by badge. */
export const BADGE_LABELS: Record<PlanBadge, string> = {
  primary_scan: "Primary scan",
  sequential_scan: "Sequential scan",
  intersect_scan: "Intersect scan",
  not_covered: "Not covered",
  slowest: "Slowest",
};

/** What a plan view holds when the text of its cell is not a plan the backend could read: nothing to draw. */
export const UNKNOWN_VIEW: PlanView = {
  kind: "unknown",
  subqueries: [],
  findings: [],
  advice: [],
  phase_times: [],
};

/** One line of the tree: an operator at its depth, or the heading of a subquery's tree. */
export type PlanRow =
  | { kind: "node"; node: PlanNode; depth: number }
  | { kind: "subquery"; text: string; correlated: boolean };

export interface PlanRows {
  /** The main tree first, then each subquery's under its heading, each depth-first. */
  rows: PlanRow[];
  /** Every operator by its id (unique over the whole view, subqueries included: a finding names one). */
  nodes: Map<number, PlanNode>;
  /** The row each operator is on. */
  index: Map<number, number>;
}

function addTree(root: PlanNode, into: PlanRows): void {
  const walk = (node: PlanNode, depth: number) => {
    into.nodes.set(node.id, node);
    into.index.set(node.id, into.rows.length);
    into.rows.push({ kind: "node", node, depth });
    for (const child of node.children) walk(child, depth + 1);
  };
  walk(root, 0); // the backend never builds a tree deeper than 200 operators (a deeper plan comes back unknown)
}

/** The rows of a view's operator trees. */
export function planRows(view: PlanView): PlanRows {
  const result: PlanRows = { rows: [], nodes: new Map(), index: new Map() };
  if (view.root) addTree(view.root, result);
  for (const subquery of view.subqueries) {
    result.rows.push({ kind: "subquery", text: subquery.text, correlated: subquery.correlated });
    addTree(subquery.root, result);
  }
  return result;
}

/** The findings of each operator, by its id (an operator's details list its own). */
export function findingsByNode(findings: readonly PlanFinding[]): Map<number, PlanFinding[]> {
  const byNode = new Map<number, PlanFinding[]>();
  for (const finding of findings) {
    const own = byNode.get(finding.node_id);
    if (own) own.push(finding);
    else byNode.set(finding.node_id, [finding]);
  }
  return byNode;
}

/** What each finding says about its operator's badge: the tooltip of that badge (`${node id}:${badge}`). */
export function badgeNotes(findings: readonly PlanFinding[]): Map<string, string> {
  return new Map(findings.map((finding) => [`${finding.node_id}:${finding.badge}`, finding.text]));
}

/**
 * An estimate for people: whole numbers from a thousand up, two decimals above one, three above a hundredth, and the
 * planner's tiny numbers (`4.6e-18`) as an exponent.
 */
export function formatEstimate(value: number): string {
  if (!Number.isFinite(value)) return String(value);
  const size = Math.abs(value);
  if (size === 0) return "0";
  if (size >= 1000) return Math.round(value).toLocaleString("en-US");
  if (size >= 1) return value.toLocaleString("en-US", { maximumFractionDigits: 2 });
  if (size >= 0.01) return value.toLocaleString("en-US", { maximumFractionDigits: 3 });
  return value.toExponential(1);
}

/** `cost 12.5 · 5 rows` (the planner's estimates, there only when it had statistics); null when it gave none. */
export function estimateText(estimates: PlanEstimates | null | undefined): string | null {
  if (!estimates) return null;
  const parts = [
    estimates.cost != null && `cost ${formatEstimate(estimates.cost)}`,
    estimates.cardinality != null && `${formatEstimate(estimates.cardinality)} rows`,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

/** `3 out · 11.4µs + 1.42ms`: the items an operator passed on, then its exec and serv times as the server wrote them. */
export function statsText(stats: PlanStats | null | undefined): string | null {
  if (!stats) return null;
  const times = [stats.exec_time, stats.serv_time].filter((time): time is string => !!time);
  const parts = [stats.items_out != null && `${stats.items_out.toLocaleString("en-US")} out`, times.join(" + ")].filter(
    Boolean,
  );
  return parts.length ? parts.join(" · ") : null;
}

/** A node's statistics as label / value pairs, in the profile's own order; the ones it did not report are left out. */
export function statRows(stats: PlanStats | null | undefined): [string, string][] {
  if (!stats) return [];
  const rows: [string, number | string | null | undefined][] = [
    ["Items in", stats.items_in],
    ["Items out", stats.items_out],
    ["Exec time", stats.exec_time],
    ["Serv time", stats.serv_time],
    ["Kern time", stats.kern_time],
  ];
  return rows.flatMap(([label, value]): [string, string][] =>
    value == null ? [] : [[label, typeof value === "number" ? value.toLocaleString("en-US") : value]],
  );
}

/** A node's estimates as label / value pairs. */
export function estimateRows(estimates: PlanEstimates | null | undefined): [string, string][] {
  if (!estimates) return [];
  const rows: [string, number | null | undefined][] = [
    ["Cost", estimates.cost],
    ["Cardinality", estimates.cardinality],
  ];
  return rows.flatMap(([label, value]): [string, string][] => (value == null ? [] : [[label, formatEstimate(value)]]));
}

/** The advice kinds in the order the panel lists them. */
const ADVICE_ORDER: IndexAdvice["kind"][] = ["current", "recommended", "covering"];
const ADVICE_TITLES: Record<IndexAdvice["kind"], string> = {
  current: "Current",
  recommended: "Recommended",
  covering: "Covering",
};

export interface AdviceGroup {
  kind: IndexAdvice["kind"];
  title: string;
  items: IndexAdvice[];
}

/** The advice by kind: current, recommended, covering; a kind without any is left out. */
export function adviceGroups(advice: readonly IndexAdvice[]): AdviceGroup[] {
  return ADVICE_ORDER.flatMap((kind) => {
    const items = advice.filter((entry) => entry.kind === kind);
    return items.length ? [{ kind, title: ADVICE_TITLES[kind], items }] : [];
  });
}

/** What the one cell of a plan set holds. */
export type PlanSource =
  | { kind: "text"; text: string }
  /** The plan was over the backend's size for a cell: only its start came. */
  | { kind: "truncated" }
  /** The set has no row (yet). */
  | { kind: "none" }
  /** A cell that is not text at all. */
  | { kind: "unreadable" };

/** The plan's JSON text in a plan set's first cell (the backend sends it whole, in one row: ruling B45). */
export function planSource(rows: readonly (readonly unknown[])[]): PlanSource {
  const row = rows[0];
  if (!row) return { kind: "none" };
  const cell = row[0];
  const text = jsonCellText(cell);
  if (text !== null) return { kind: "text", text };
  if (typeof cell === "string") return { kind: "text", text: cell };
  if (cell !== null && typeof cell === "object" && (cell as EncodedCell).$type === "text") return { kind: "truncated" };
  return { kind: "unreadable" };
}

/**
 * The statement to ask index advice for: the one the plan is of (EXPLAIN's `text`), else the one that ran, which is what
 * a profile (it carries no statement) is of. What ran may be an EXPLAIN (Explain sends one, or the user typed it): its
 * statement is what is advised, never `ADVISE EXPLAIN …`.
 */
export function adviceStatement(view: PlanView, executed: string): string {
  return view.statement_text?.trim() ? view.statement_text : withoutExplain(executed);
}

const PLAN_LABEL = "Plan";
const PROFILE_LABEL = "Profile";

/**
 * The tab of a plan set: `Profile` when the run asked for the execution profile and the set is that profile (it follows
 * the rows of the statement, or is all a statement without rows has), `Plan` for the plan of an EXPLAIN. A run with
 * profile of an EXPLAIN or ADVISE that gave one set only has the plan of the statement: no rows, no profile. `ordinal` is
 * the set's place among the execution's sets.
 */
export function planSetLabel(execution: Pick<ExecutionState, "profilePlan" | "sql">, ordinal: number): string {
  const profile = execution.profilePlan && (ordinal > 0 || !isPlanRequest(execution.sql));
  return profile ? PROFILE_LABEL : PLAN_LABEL;
}
