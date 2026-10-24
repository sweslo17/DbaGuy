import type { ColumnMapping, TableTransfer } from "@/lib/api/types";
import type { CopyForm, IdentityChoice } from "./copyForm";

/*
 * A copy's column mapping as the dialog's table shows it (spec ⑥ §3.6 MappingTable, §3.5.1): one row per source column
 * — where it goes, its type (a new table's may be typed over), how its values change on the way (⚠), why it is not
 * copied, and the problem that blocks the copy — and what each control changes in the form (a type, an identity's
 * choice, a column left out). Every word comes from the preview (`TableTransfer`); nothing here decides a mapping. Pure.
 */

export interface MappingRow {
  source: string;
  sourceType: string;
  /** Where it goes; null when it is not copied (`skipped` says why). */
  target: string | null;
  targetType: string | null;
  /** A new table's column: its type can be typed over (the form's override, shown in its field). */
  typeEditable: boolean;
  /** What the form asks for it (the field's text), when it overrides the mapped type. */
  typed: string | null;
  conversion: string | null;
  skipped: string | null;
  /** Appending into an identity column: keep the source's values or let the target make them; null otherwise. */
  identity: IdentityChoice | null;
  problem: string | null;
  /** The user left it out: it can be put back. */
  leftOut: boolean;
  /** It can be left out (it is copied now, or left out by the user). */
  canLeaveOut: boolean;
}

/** A key's own value: a table or column named `constructor` or `__proto__` is a name like any other (05b K2). */
const own = <T>(record: Readonly<Record<string, T>>, key: string): T | undefined =>
  Object.hasOwn(record, key) ? record[key] : undefined;

/** `record` with `key` set to `value` (undefined: without it), as own data properties only. */
function withKey<T>(record: Readonly<Record<string, T>>, key: string, value: T | undefined): Record<string, T> {
  const kept: [string, T][] = Object.entries(record).filter(([name]) => name !== key);
  return Object.fromEntries(value === undefined ? kept : [...kept, [key, value]]);
}

export function mappingRows(table: TableTransfer, form: CopyForm): MappingRow[] {
  const typed = own(form.overrides, table.source_key) ?? {};
  const out = new Set(own(form.leftOut, table.source_key) ?? []);
  const identities = own(form.identity, table.source_key) ?? {};
  return table.columns.map((column: ColumnMapping) => {
    // What the form asks for is shown at once, not when the next preview answers (a checkbox that sprang back for the
    // length of a read would look broken). The backend leaves out every column the form names, so the two agree; the
    // preview's own `skipped` text says it in words. (No match on that text: the schema carries no code for it.)
    const leftOut = out.has(column.source_name);
    return {
      source: column.source_name,
      sourceType: column.source_type,
      target: column.target_name ?? null,
      targetType: column.target_type ?? null,
      typeEditable: column.type_editable ?? false,
      typed: own(typed, column.source_name) ?? null,
      conversion: column.conversion ?? null,
      skipped: column.skipped ?? null,
      identity: column.identity ? (own(identities, column.source_name) ?? "keep") : null,
      problem: column.problem ?? null,
      leftOut,
      canLeaveOut: leftOut || (!column.skipped && column.target_name != null),
    };
  });
}

/** The form with a new column's type typed over (empty text: the mapped type again). */
export function withOverride(form: CopyForm, table: string, column: string, text: string): CopyForm {
  const columns = withKey(own(form.overrides, table) ?? {}, column, text.trim() ? text : undefined);
  return { ...form, overrides: withKey(form.overrides, table, Object.keys(columns).length ? columns : undefined) };
}

/** The form with an identity column's choice (keep is the default: it is not sent). */
export function withIdentity(form: CopyForm, table: string, column: string, choice: IdentityChoice): CopyForm {
  const columns = withKey(own(form.identity, table) ?? {}, column, choice === "keep" ? undefined : choice);
  return { ...form, identity: withKey(form.identity, table, Object.keys(columns).length ? columns : undefined) };
}

/** The form with a column left out of the copy, or put back. */
export function withLeftOut(form: CopyForm, table: string, column: string, out: boolean): CopyForm {
  const kept = (own(form.leftOut, table) ?? []).filter((name) => name !== column);
  const columns = out ? [...kept, column] : kept;
  return { ...form, leftOut: withKey(form.leftOut, table, columns.length ? columns : undefined) };
}

/** The statements a table adds, each with whether it destroys rows (replace's DELETE: danger, M11). */
export function statementLines(table: TableTransfer): { sql: string; danger: boolean }[] {
  return [...table.before, ...table.after].map((sql) => ({ sql, danger: /^\s*DELETE\b/i.test(sql) }));
}

/** Every problem of the preview, the copy's first: what blocks Copy, said once each. */
export function previewProblems(plan: { problems: { message: string }[]; tables: TableTransfer[] }): string[] {
  return [...new Set([...plan.problems, ...plan.tables.flatMap((table) => table.problems)].map((p) => p.message))];
}
