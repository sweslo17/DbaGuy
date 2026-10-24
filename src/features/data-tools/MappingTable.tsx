import { useState, type KeyboardEvent } from "react";
import { Checkbox, Input } from "@/components/ui/Field";
import type { TableTransfer } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { isImeKeyEvent } from "@/lib/ime";
import type { CopyForm, IdentityChoice } from "./copyForm";
import { mappingRows, statementLines, withIdentity, withLeftOut, withOverride, type MappingRow } from "./mappingRows";

/*
 * One table of a copy's preview (spec ⑥ §3.6 MappingTable): each source column → its target column and type (a new
 * table's typed over in place), what happens to its values on the way (⚠), why it is not copied, its problem; an
 * identity's keep or generate when appending; the statements the copy adds around the rows (a DELETE in danger, M11).
 * Every change goes to the form, and the dialog reads the preview again. The words come from `mappingRows` only.
 */

/** A header cell: sticky on its own (a sticky `thead` is not honoured everywhere), over the rows scrolling under it. */
const HEAD = "sticky top-0 bg-elevated px-2 py-1 font-normal";

/** A new column's type, typed over: sent once the field is left or Return is pressed (each keystroke would re-plan). */
function TypeField({ row, onType }: { row: MappingRow; onType: (text: string) => void }) {
  const shown = row.typed ?? row.targetType ?? "";
  const [text, setText] = useState(shown);
  const [was, setWas] = useState(shown);
  if (shown !== was) {
    // the preview answered with another type (or the form was reset): the field follows it
    setWas(shown);
    setText(shown);
  }
  const commit = () => {
    if (text !== shown) onType(text.trim() === (row.targetType ?? "") && row.typed === null ? "" : text);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (isImeKeyEvent(e)) return;
    if (e.key === "Enter") {
      e.preventDefault(); // Return commits the type and never copies from here
      commit();
    }
  };
  return (
    <Input
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={onKeyDown}
      aria-label={`Type of ${row.source}`}
      aria-invalid={row.problem !== null}
      className={cn("h-6 w-full font-mono text-[12px]", row.problem && "border-danger")}
      spellCheck={false}
    />
  );
}

export function MappingTable({
  table,
  form,
  onChange,
}: {
  table: TableTransfer;
  form: CopyForm;
  onChange: (form: CopyForm) => void;
}) {
  const rows = mappingRows(table, form);
  const key = table.source_key;
  const statements = statementLines(table);
  return (
    <div className="flex flex-col gap-2 text-[12px]">
      {/* A fixed height, its scrollbar's room reserved: a classic scrollbar never sits over the last column */}
      <div className="max-h-64 overflow-auto rounded-md border border-border [scrollbar-gutter:stable]">
        <table className="w-full border-collapse">
          <thead className="text-left text-[11px] text-muted">
            <tr>
              <th className={HEAD}>Source</th>
              <th className={HEAD}>Target</th>
              <th className={HEAD}>Type</th>
              <th className={HEAD}>On the way</th>
              <th className={HEAD}>Copy</th>
            </tr>
          </thead>
          <tbody>
            {/* By position: a query's columns may share a name (the preview names that as a problem), and the order is the source's */}
            {rows.map((row, index) => (
              <tr key={index} className="border-t border-border align-top">
                <td className="px-2 py-1" data-selectable>
                  <span className="font-mono [overflow-wrap:anywhere]">{row.source}</span>
                  <span className="block text-[11px] text-muted">{row.sourceType}</span>
                </td>
                <td className="px-2 py-1 font-mono [overflow-wrap:anywhere]" data-selectable>
                  {row.target ?? <span className="text-muted">—</span>}
                </td>
                <td className="min-w-[9rem] px-2 py-1">
                  {row.typeEditable && !row.skipped && !row.leftOut ? (
                    <TypeField row={row} onType={(text) => onChange(withOverride(form, key, row.source, text))} />
                  ) : (
                    <span className="font-mono text-muted" data-selectable>
                      {row.targetType ?? ""}
                    </span>
                  )}
                  {row.identity && (
                    <select
                      className="mt-1 h-6 rounded border border-border bg-elevated px-1 text-[12px]"
                      value={row.identity}
                      aria-label={`Identity of ${row.target ?? row.source}`}
                      onChange={(e) => onChange(withIdentity(form, key, row.source, e.target.value as IdentityChoice))}
                    >
                      <option value="keep">Keep the source's values</option>
                      <option value="generate">Let the target generate them</option>
                    </select>
                  )}
                </td>
                <td className="px-2 py-1 [overflow-wrap:anywhere]" data-selectable>
                  {row.problem ? (
                    <span className="text-danger">{row.problem}</span>
                  ) : row.skipped ? (
                    <span className="text-muted">{row.skipped}</span>
                  ) : row.conversion ? (
                    <span className="text-warning">⚠ {row.conversion}</span>
                  ) : null}
                </td>
                <td className="px-2 py-1">
                  {row.canLeaveOut && (
                    <Checkbox
                      label=""
                      checked={!row.leftOut}
                      aria-label={`Copy ${row.source}`}
                      onChange={(e) => onChange(withLeftOut(form, key, row.source, !e.target.checked))}
                    />
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {statements.length > 0 && (
        <div className="flex flex-col gap-1">
          {statements.map((line, i) => (
            <pre
              key={i}
              className={cn(
                "max-h-40 overflow-auto rounded-md border px-2 py-1 font-mono text-[11px] whitespace-pre-wrap [scrollbar-gutter:stable] [overflow-wrap:anywhere]",
                line.danger ? "border-danger/40 bg-danger/10 font-medium text-danger" : "border-border text-muted",
              )}
              data-selectable
            >
              {line.sql}
            </pre>
          ))}
        </div>
      )}
    </div>
  );
}
