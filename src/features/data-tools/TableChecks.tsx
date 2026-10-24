import { Button } from "@/components/ui/Button";
import { Checkbox } from "@/components/ui/Field";
import { cn } from "@/lib/cn";

/** A table a form lists: its name, and a word beside it when the tree has one (rows, size). */
export interface ListedTable {
  name: string;
  detail?: string | null;
}

/**
 * A schema's tables to check (the dump's and the copy's forms), every one checked the first time they arrive (the dialog
 * seeds that, §3.3.1); All, None and the count, and beside it what is wrong with the checks (none, or more than the
 * tool takes), said here and not only on its button's tooltip. The count turns red with a problem that is not "none
 * checked".
 */
export function TableChecks({
  tables,
  checked,
  problem,
  onChange,
}: {
  tables: readonly ListedTable[];
  checked: readonly string[];
  problem: string | null;
  onChange: (names: string[]) => void;
}) {
  const on = new Set(checked);
  // The tree's order, whatever the order of the clicks
  const toggle = (name: string, value: boolean) =>
    onChange(tables.map((table) => table.name).filter((n) => (n === name ? value : on.has(n))));
  const tooMany = problem !== null && checked.length > 0;
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-start gap-1 text-[12px]">
        <Button size="sm" variant="ghost" onClick={() => onChange(tables.map((table) => table.name))}>
          All
        </Button>
        <Button size="sm" variant="ghost" onClick={() => onChange([])}>
          None
        </Button>
        <span className="ml-auto min-w-0 pt-1 text-right">
          <span className={cn("tabular-nums", tooMany ? "text-danger" : "text-muted")}>
            {checked.length.toLocaleString()} of {tables.length.toLocaleString()} checked
          </span>
          {problem && (
            // none checked: what to do; too many: what the tool cannot take
            <span className={cn("block", tooMany ? "text-danger" : "text-muted")}>{problem}</span>
          )}
        </span>
      </div>
      <div className="flex max-h-48 flex-col gap-0.5 overflow-y-auto rounded-md border border-border px-2 py-1 [scrollbar-gutter:stable]">
        {tables.length === 0 && <p className="text-[12px] text-muted">No tables</p>}
        {tables.map((table) => (
          <div key={table.name} className="flex min-w-0 items-center gap-2">
            <Checkbox
              label={<span className="[overflow-wrap:anywhere]">{table.name}</span>}
              checked={on.has(table.name)}
              onChange={(e) => toggle(table.name, e.target.checked)}
            />
            {table.detail && <span className="ml-auto shrink-0 text-[11px] text-muted">{table.detail}</span>}
          </div>
        ))}
      </div>
    </div>
  );
}
