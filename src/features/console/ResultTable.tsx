import { formatCell } from "@/lib/cells";
import { cn } from "@/lib/cn";
import type { ResultSetState } from "./execution";

export function ResultTable({ set }: { set: ResultSetState }) {
  return (
    <div className="overflow-auto rounded-md border border-border" data-selectable>
      <table className="border-collapse font-mono text-[12px]">
        <thead className="sticky top-0 bg-panel">
          <tr>
            <th className="border-b border-r border-border px-2 py-1 text-right text-muted">#</th>
            {set.columns.map((c, i) => (
              <th key={i} className="border-b border-r border-border px-2 py-1 text-left font-semibold" title={c.type_name}>
                {c.name}
                <span className="ml-1 font-normal text-muted">{c.type_name}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {set.rows.map((row, r) => (
            <tr key={r} className="hover:bg-hover">
              <td className="border-r border-border px-2 text-right text-muted">{r + 1}</td>
              {row.map((value, c) => {
                const cell = formatCell(value);
                return (
                  <td
                    key={c}
                    className={cn(
                      "max-w-[420px] truncate border-r border-border px-2",
                      set.columns[c]?.category === "number" && "text-right",
                      cell.kind === "null" && "italic text-muted",
                      cell.kind === "bytes" && "text-muted",
                    )}
                  >
                    {cell.text}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
