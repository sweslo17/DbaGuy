import { memo } from "react";
import { plural } from "@/lib/format";
import type { Aggregates } from "./aggregate";

interface StatusBarProps {
  rowCount: number;
  columnCount: number;
  hasMore: boolean;
  /** Figures of a selection of more than one cell, else null. */
  aggregates: Aggregates | null;
}

/** Up to 15 significant digits: a double's precision, without float noise such as 0.30000000000000004. */
const formatNumber = (value: number) => value.toLocaleString(undefined, { maximumSignificantDigits: 15 });

function Figures({ aggregates: a }: { aggregates: Aggregates }) {
  const { sum, avg, min, max } = a;
  if (a.capped || sum === undefined || avg === undefined || min === undefined || max === undefined) return null;
  const mark = a.approximate ? "≈" : "";
  const figures: [string, number][] = [
    ["Sum", sum],
    ["Avg", avg],
    ["Min", min],
    ["Max", max],
  ];
  return (
    <span
      className="truncate"
      data-selectable
      title={a.approximate ? "Approximate: a value exceeds double precision" : undefined}
    >
      {figures.map(([label, value], i) => (
        <span key={label}>
          {i > 0 && " · "}
          {label} {mark}
          <span className="text-fg">{formatNumber(value)}</span>
        </span>
      ))}
    </span>
  );
}

/** Under the grid: the size of the result, and the selected cell count with its numeric figures. */
export const StatusBar = memo(function StatusBar({ rowCount, columnCount, hasMore, aggregates }: StatusBarProps) {
  return (
    <div className="flex h-6 shrink-0 items-center gap-4 border-y border-border px-3 text-[12px] whitespace-nowrap text-muted">
      <span className="shrink-0">
        {plural(rowCount, "row")}
        {hasMore ? " loaded" : ""} · {plural(columnCount, "column")}
      </span>
      {aggregates && (
        <span className="ml-auto flex min-w-0 items-center gap-4">
          <span className="shrink-0">{plural(aggregates.cells, "cell")}</span>
          <Figures aggregates={aggregates} />
        </span>
      )}
    </div>
  );
});
