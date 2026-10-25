import { useState, type KeyboardEvent } from "react";
import { Checkbox, Input, Select } from "@/components/ui/Field";
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

/** An identity column's choice when appending, in full (its menu's words are short: the column is narrow). */
const IDENTITY_TITLES: Record<IdentityChoice, string> = {
  keep: "Keep the source's values",
  generate: "Let the target generate them",
};

/** What marks where the rows go among a table's statements: those before them above it, those after them below. */
const ROWS_HERE = "the rows are copied here";

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

/** A statement the copy adds; replace's DELETE in the danger colour. */
function StatementBox({ sql, danger }: { sql: string; danger: boolean }) {
  return (
    <pre
      className={cn(
        "max-h-40 overflow-auto rounded-md border px-2 py-1 font-mono text-[11px] whitespace-pre-wrap [scrollbar-gutter:stable] [overflow-wrap:anywhere]",
        danger ? "border-danger/40 bg-danger/10 font-medium text-danger" : "border-border text-muted",
      )}
      data-selectable
    >
      {sql}
    </pre>
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
  // A column put back keeps its box until the next preview answers: the preview on show still says it is left out
  // (so it is not "copied now"), and a box that vanished for the length of a read would look broken. The list belongs to
  // the preview it was made on; a new preview (a new `table`) starts it empty.
  const [putBack, setPutBack] = useState<{ on: TableTransfer; names: readonly string[] }>({ on: table, names: [] });
  const returning = putBack.on === table ? putBack.names : [];
  const leaveOut = (row: MappingRow, out: boolean) => {
    onChange(withLeftOut(form, key, row.source, out));
    if (!out) setPutBack({ on: table, names: [...returning, row.source] });
  };
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
                {/* Names are never broken inside a word: room for them, and a long one is cut (whole on hover) */}
                <td className="min-w-[7rem] px-2 py-1" data-selectable>
                  <span className="block max-w-[14rem] truncate font-mono" title={row.source}>
                    {row.source}
                  </span>
                  <span className="block text-[11px] text-muted">{row.sourceType}</span>
                </td>
                <td className="min-w-[7rem] px-2 py-1 font-mono" data-selectable>
                  {row.target === null ? (
                    <span className="text-muted">—</span>
                  ) : (
                    <span className="block max-w-[14rem] truncate" title={row.target}>
                      {row.target}
                    </span>
                  )}
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
                    // Short words that fit the column's 9rem (the long ones were cut: MP-4); the whole choice on hover
                    <Select
                      className="mt-1 text-[12px]"
                      value={row.identity}
                      aria-label={`Identity of ${row.target ?? row.source}`}
                      title={IDENTITY_TITLES[row.identity]}
                      onChange={(e) => onChange(withIdentity(form, key, row.source, e.target.value as IdentityChoice))}
                    >
                      <option value="keep" title={IDENTITY_TITLES.keep}>
                        Keep values
                      </option>
                      <option value="generate" title={IDENTITY_TITLES.generate}>
                        Generate new
                      </option>
                    </Select>
                  )}
                </td>
                <td className="min-w-[8rem] px-2 py-1 [overflow-wrap:anywhere]" data-selectable>
                  {row.problem ? (
                    <span className="text-danger">{row.problem}</span>
                  ) : row.skipped ? (
                    <span className="text-muted">{row.skipped}</span>
                  ) : row.conversion ? (
                    <span className="text-warning">⚠ {row.conversion}</span>
                  ) : null}
                </td>
                <td className="px-2 py-1">
                  {(row.canLeaveOut || returning.includes(row.source)) && (
                    <Checkbox
                      label=""
                      checked={!row.leftOut}
                      aria-label={`Copy ${row.source}`}
                      onChange={(e) => leaveOut(row, !e.target.checked)}
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
          {statements
            .filter((line) => line.phase === "before")
            .map((line, i) => (
              <StatementBox key={`before-${i}`} sql={line.sql} danger={line.danger} />
            ))}
          <p className="text-center text-[11px] text-muted">— {ROWS_HERE} —</p>
          {statements
            .filter((line) => line.phase === "after")
            .map((line, i) => (
              <StatementBox key={`after-${i}`} sql={line.sql} danger={line.danger} />
            ))}
        </div>
      )}
    </div>
  );
}
