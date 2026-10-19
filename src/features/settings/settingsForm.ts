import { ApiError, errorMessage } from "@/lib/api/client";
import type { AppSettings } from "@/lib/api/types";

/*
 * The settings form's model: the numeric fields with the backend's ranges (`domain/settings.py`; the server's 422 stays
 * the authority), their validation, and the text of a refused save.
 */

type Section = "editor" | "run" | "safety" | "notifications" | "couchbase";
type NumberKey<T> = { [K in keyof T]-?: T[K] extends number ? K : never }[keyof T];

interface NumberFieldOf<S extends Section> {
  section: S;
  key: NumberKey<AppSettings[S]>;
  label: string;
  unit?: string;
  min: number;
  /** None: no upper bound. */
  max?: number;
  hint?: string;
}

export type NumberField = { [S in Section]: NumberFieldOf<S> }[Section];

export const NUMBER_FIELDS = {
  font_size: { section: "editor", key: "font_size", label: "Font size", unit: "px", min: 8, max: 32 },
  tab_size: { section: "editor", key: "tab_size", label: "Tab size", unit: "spaces", min: 1, max: 8 },
  page_size: {
    section: "run",
    key: "page_size",
    label: "Page size",
    unit: "rows",
    min: 1,
    max: 10_000,
    hint: "Rows fetched at a time; Load more fetches the next page",
  },
  statement_timeout_s: {
    section: "run",
    key: "statement_timeout_s",
    label: "Statement timeout",
    unit: "s",
    min: 0,
    max: 86_400,
    hint: "Cancels a statement that runs longer; 0 = none",
  },
  idle_transaction_warn_minutes: {
    section: "safety",
    key: "idle_transaction_warn_minutes",
    label: "Idle warning",
    unit: "min",
    min: 0,
    max: 1440,
    hint: "Warns about a transaction left open and idle this long; 0 = off",
  },
  idle_transaction_rollback_minutes: {
    section: "safety",
    key: "idle_transaction_rollback_minutes",
    label: "Idle rollback",
    unit: "min",
    min: 0,
    max: 1440,
    hint: "Rolls back a transaction left open and idle this long; 0 = off",
  },
  long_query_seconds: {
    section: "notifications",
    key: "long_query_seconds",
    label: "Long query",
    unit: "s",
    min: 0,
    hint: "Notifies when a run that took at least this long ends while DbaGuy is in the background; 0 = off",
  },
  sample_size: {
    section: "couchbase",
    key: "sample_size",
    label: "Sample size",
    unit: "documents",
    min: 10,
    max: 10_000,
    hint: "Documents read by Sample fields; INFER is never run for you",
  },
} as const satisfies Record<string, NumberField>;

export type NumberId = keyof typeof NUMBER_FIELDS;
const NUMBER_IDS = Object.keys(NUMBER_FIELDS) as NumberId[];

/** What the user typed in each numeric box. */
export type NumberTexts = Record<NumberId, string>;

const valueOf = (settings: AppSettings, field: NumberField): number =>
  (settings[field.section] as Record<string, number>)[field.key];

export function numberTexts(settings: AppSettings): NumberTexts {
  return Object.fromEntries(NUMBER_IDS.map((id) => [id, String(valueOf(settings, NUMBER_FIELDS[id]))])) as NumberTexts;
}

/** Why `text` is not a valid value for `field`, or null. */
export function numberProblem(field: NumberField, text: string): string | null {
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) return "Enter a whole number";
  const value = Number(trimmed);
  const max = "max" in field ? field.max : undefined;
  if (value < field.min || (max !== undefined && value > max)) {
    return max !== undefined ? `Between ${field.min} and ${max.toLocaleString()}` : `At least ${field.min}`;
  }
  return Number.isSafeInteger(value) ? null : "Too large";
}

/** The scripts folder as stored (empty: the default), or why it cannot be. The backend checks the folder itself. */
export function scriptsDirValue(text: string): { value: string | null; problem: string | null } {
  const trimmed = text.trim();
  if (!trimmed) return { value: null, problem: null };
  if (!trimmed.startsWith("/") && !trimmed.startsWith("~")) {
    return { value: trimmed, problem: "Enter an absolute path (or one starting with ~)" };
  }
  return { value: trimmed, problem: null };
}

/** `settings` with the typed numbers in place; call only once every number is valid. */
export function withNumbers(settings: AppSettings, texts: NumberTexts): AppSettings {
  return NUMBER_IDS.reduce<AppSettings>((next, id) => {
    const field = NUMBER_FIELDS[id];
    return { ...next, [field.section]: { ...next[field.section], [field.key]: Number(texts[id].trim()) } };
  }, settings);
}

export function hasNumberProblem(texts: NumberTexts): boolean {
  return NUMBER_IDS.some((id) => numberProblem(NUMBER_FIELDS[id], texts[id]) !== null);
}

interface ValidationItem {
  loc: (string | number)[];
  msg: string;
}

const isValidationItem = (item: unknown): item is ValidationItem =>
  typeof item === "object" &&
  item !== null &&
  Array.isArray((item as ValidationItem).loc) &&
  typeof (item as ValidationItem).msg === "string";

/** The field a 422 item is about, by its label when the form shows it ("Page size: …"). */
function itemText({ loc, msg }: ValidationItem): string {
  const [, section, key] = loc;
  const field = Object.values(NUMBER_FIELDS).find((f) => f.section === section && f.key === key);
  const where = field?.label ?? loc.slice(1).join(".");
  return where ? `${where}: ${msg}` : msg;
}

/** Why a save was refused: each field the backend rejected (422), or its message. */
export function saveErrorText(error: unknown): string {
  const { detail } = error instanceof ApiError ? error : { detail: undefined };
  if (Array.isArray(detail) && detail.length > 0 && detail.every(isValidationItem)) {
    return detail.map(itemText).join("\n");
  }
  return errorMessage(error);
}
