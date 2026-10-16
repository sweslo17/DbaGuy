import { Download } from "lucide-react";
import { memo } from "react";
import { plural } from "@/lib/format";
import type { Aggregates } from "./aggregate";

interface StatusBarProps {
  rowCount: number;
  columnCount: number;
  hasMore: boolean;
  /** Only some of the rows were ever loaded. */
  partial: boolean;
  /** Figures of a selection of more than one cell, else null. */
  aggregates: Aggregates | null;
  /** Opens the export dialog (shown only when set). */
  onExport?: () => void;
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

/** Under the grid: the size of the result, the selected cell count with its numeric figures, and Export. */
export const StatusBar = memo(function StatusBar(props: StatusBarProps) {
  const { rowCount, columnCount, hasMore, partial, aggregates, onExport } = props;
  return (
    <div className="flex h-6 shrink-0 items-center gap-4 border-y border-border px-3 text-[12px] whitespace-nowrap text-muted">
      <span className="shrink-0">
        {plural(rowCount, "row")}
        {hasMore ? " loaded" : partial ? " (partial)" : ""} · {plural(columnCount, "column")}
      </span>
      <span className="ml-auto flex min-w-0 items-center gap-4">
        {aggregates && (
          <>
            <span className="shrink-0">{plural(aggregates.cells, "cell")}</span>
            <Figures aggregates={aggregates} />
          </>
        )}
        {onExport && (
          <button
            type="button"
            className="-mr-2 flex h-5 shrink-0 items-center gap-1 rounded px-1.5 hover:bg-hover hover:text-fg"
            title="Export the whole result to a file"
            onClick={onExport}
          >
            <Download size={12} />
            Export…
          </button>
        )}
      </span>
    </div>
  );
});
